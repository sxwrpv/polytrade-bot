from __future__ import annotations

import logging
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from backend.api.deps import get_current_user
from backend.api.routes_user import ExportKeyBody, export_key
from backend.core import auth
from backend.core.runtime_security import harden_runtime_files
from backend.core.telegram_alerts import TelegramAPIError, TelegramPositionNotifier

# This module specifies the hardening target: hashed + expiring cookie
# sessions, Telegram step-up on key export, redacted Telegram errors, and no
# Bearer token in the frontend. All of it has landed, so nothing here is behind
# a skip guard any more: a regression must fail the suite, not quietly skip.


class SessionSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_is_hashed_expiring_and_cookie_authenticated(self):
        raw, stored, expires_at = auth.new_session()
        self.assertNotEqual(raw, stored)
        self.assertTrue(stored.startswith("sha256:"))
        self.assertGreater(auth.parse_session_expiry(expires_at), time.time())

        db = AsyncMock()
        db.fetchone.return_value = {
            "id": "wallet",
            "api_token": stored,
            "api_token_expires_at": expires_at,
        }
        request = SimpleNamespace(
            cookies={auth.SESSION_COOKIE: raw},
            app=SimpleNamespace(state=SimpleNamespace(db=db)),
        )
        user = await get_current_user(request)
        self.assertEqual(user["id"], "wallet")
        query_token = db.fetchone.await_args.args[1][0]
        self.assertEqual(query_token, stored)
        self.assertNotEqual(query_token, raw)

    async def test_expired_session_is_rejected(self):
        raw, stored, _ = auth.new_session()
        db = AsyncMock()
        db.fetchone.return_value = {
            "id": "wallet",
            "api_token": stored,
            "api_token_expires_at": "2000-01-01T00:00:00+00:00",
        }
        request = SimpleNamespace(
            cookies={auth.SESSION_COOKIE: raw},
            app=SimpleNamespace(state=SimpleNamespace(db=db)),
        )
        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(request)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_legacy_plaintext_tokens_are_invalidated_not_backfilled(self):
        db = AsyncMock()
        db.execute.return_value = 3
        changed = await auth.invalidate_legacy_sessions(db)
        self.assertEqual(changed, 3)
        sql = db.execute.await_args.args[0]
        self.assertIn("api_token = NULL", sql)
        self.assertIn("sha256:", sql)


class ExportStepUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_requires_fresh_matching_telegram_identity(self):
        user = {
            "telegram_user_id": 123,
            "private_key_enc": "ciphertext",
        }
        with patch("backend.api.routes_user.auth.validate_init_data", return_value={"id": 999}), \
             patch("backend.api.routes_user.wallet.decrypt_private_key") as decrypt:
            with self.assertRaises(HTTPException) as ctx:
                await export_key(ExportKeyBody(init_data="signed"), user=user)
        self.assertEqual(ctx.exception.status_code, 403)
        decrypt.assert_not_called()

    async def test_export_uses_five_minute_step_up_window(self):
        user = {
            "telegram_user_id": 123,
            "private_key_enc": "ciphertext",
        }
        with patch("backend.api.routes_user.auth.validate_init_data", return_value={"id": 123}) as validate, \
             patch("backend.api.routes_user.wallet.decrypt_private_key", return_value="0xsecret"):
            result = await export_key(ExportKeyBody(init_data="signed"), user=user)
        self.assertEqual(result, {"private_key": "0xsecret"})
        self.assertEqual(validate.call_args.kwargs["max_age"], 300)


class TelegramRedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_notifier_error_never_contains_bot_token_or_request_url(self):
        token = "123456:super-secret-token"
        db = AsyncMock()
        db.fetchone.return_value = {"telegram_user_id": 12345}
        response = SimpleNamespace(status_code=500)
        http = AsyncMock()
        http.post.return_value = response
        notifier = TelegramPositionNotifier(db, token, http=http)

        with self.assertRaises(TelegramAPIError) as ctx:
            await notifier({"event": "opened", "user_id": "wallet", "market_title": "m"})
        text = str(ctx.exception)
        self.assertNotIn(token, text)
        self.assertNotIn("api.telegram.org", text)
        self.assertIn("500", text)

    async def test_logged_traceback_never_contains_bot_token(self):
        """What the engine actually writes: log.exception prints the full
        traceback, chained causes included, for both an HTTP error status and
        a transport failure from a real httpx client."""
        token = "123456:super-secret-token"
        db = AsyncMock()
        db.fetchone.return_value = {"telegram_user_id": 12345}

        def unauthorized(request):
            return httpx.Response(401, request=request)

        def unreachable(request):
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        for handler in (unauthorized, unreachable):
            with self.subTest(handler=handler.__name__):
                http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                notifier = TelegramPositionNotifier(db, token, http=http)
                logger = logging.getLogger("test.telegram_redaction")
                with self.assertLogs(logger, level="ERROR") as logs:
                    try:
                        await notifier({"event": "opened", "user_id": "wallet"})
                    except TelegramAPIError:
                        logger.exception("position alert failed")
                await http.aclose()
                logged = "\n".join(logs.output)
                self.assertIn("TelegramAPIError", logged)
                self.assertNotIn(token, logged)


class RuntimePermissionTests(unittest.TestCase):
    def test_runtime_files_are_hardened_to_owner_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "logs").mkdir()
            for rel in (".env", "copybot.db", "copybot.db-wal", "logs/server.log"):
                path = root / rel
                path.touch()
                os.chmod(path, 0o644)

            harden_runtime_files(root, db_path="copybot.db")

            for rel in (".env", "copybot.db", "copybot.db-wal", "logs/server.log"):
                mode = stat.S_IMODE((root / rel).stat().st_mode)
                self.assertEqual(mode, 0o600, rel)
            self.assertEqual(stat.S_IMODE((root / "logs").stat().st_mode), 0o700)


class FrontendStorageTests(unittest.TestCase):
    def test_frontend_never_stores_or_sends_bearer_token(self):
        source = (Path(__file__).parents[1] / "frontend/src/api.js").read_text()
        self.assertNotIn("s?.token", source)
        self.assertNotIn("Authorization", source)
        self.assertNotIn("api_token", source)
        self.assertIn("credentials: 'same-origin'", source)

    def test_export_sends_telegram_step_up_proof(self):
        source = (Path(__file__).parents[1] / "frontend/src/api.js").read_text()
        self.assertIn("exportKey: (initData)", source)
        self.assertIn("init_data: initData", source)


if __name__ == "__main__":
    unittest.main()
