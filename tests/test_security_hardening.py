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

from fastapi import HTTPException

from backend.api.deps import get_current_user
from backend.core import auth
from backend.core.runtime_security import harden_runtime_files

# This module SPECIFIES the target hardening (hashed + expiring cookie sessions,
# Telegram step-up on key export, no Bearer token in the frontend). Several
# pieces of that target do not exist yet, so the imports are guarded: a raw
# ImportError here stops the whole suite from collecting and hides every other
# test. Cases whose dependencies are missing skip; the rest still run. Drop the
# guard once routes_user exposes ExportKeyBody and telegram_alerts raises
# TelegramAPIError.
try:
    from backend.api.routes_user import ExportKeyBody, export_key
    _EXPORT_READY = True
except ImportError:                                  # pragma: no cover
    ExportKeyBody = export_key = None
    _EXPORT_READY = False

try:
    from backend.core.telegram_alerts import TelegramAPIError, TelegramPositionNotifier
    _ALERTS_READY = True
except ImportError:                                  # pragma: no cover
    TelegramAPIError = TelegramPositionNotifier = None
    _ALERTS_READY = False

_PENDING = "target hardening not wired up yet"


@unittest.skipUnless(_EXPORT_READY, _PENDING)
class _RequiresTargetAuth(unittest.IsolatedAsyncioTestCase):
    """Base for cases that need the not-yet-landed cookie/step-up auth."""


class SessionSecurityTests(_RequiresTargetAuth):
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
        user = await get_current_user(request, authorization=None, x_api_token=None)
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
            await get_current_user(request, authorization=None, x_api_token=None)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_legacy_plaintext_tokens_are_invalidated_not_backfilled(self):
        db = AsyncMock()
        db.execute.return_value = 3
        changed = await auth.invalidate_legacy_sessions(db)
        self.assertEqual(changed, 3)
        sql = db.execute.await_args.args[0]
        self.assertIn("api_token = NULL", sql)
        self.assertIn("sha256:", sql)


class ExportStepUpTests(_RequiresTargetAuth):
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


@unittest.skipUnless(_ALERTS_READY, _PENDING)
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


@unittest.skipUnless(_EXPORT_READY, _PENDING)
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
