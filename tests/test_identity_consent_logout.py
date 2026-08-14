from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Response

from backend.api.routes_auth import logout
from backend.api.routes_user import CURRENT_TERMS_VERSION, CreateWallet, create_wallet
from backend.core import auth
from backend.db.database import Database
from backend.db.models import PG_SCHEMA_SQL, SCHEMA_SQL, TABLES


class WalletIdentityAndConsentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        self.request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.10"), headers={}
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    def body(self, **overrides):
        values = {
            "init_data": "signed-init-data",
            "terms_accepted": True,
            "terms_version": CURRENT_TERMS_VERSION,
        }
        values.update(overrides)
        return CreateWallet(**values)

    async def assert_rejected_before_signer(self, body, *, token="bot-token", status):
        response = Response()
        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", token), \
             patch("backend.api.routes_user.wallet.create_signer") as create_signer:
            with self.assertRaises(HTTPException) as ctx:
                await create_wallet(body, self.request, response, db=self.db)
        self.assertEqual(status, ctx.exception.status_code)
        create_signer.assert_not_called()

    async def test_missing_telegram_proof_is_rejected_before_signer_creation(self):
        await self.assert_rejected_before_signer(
            self.body(init_data=None), status=401)

    async def test_unconfigured_telegram_token_is_rejected_before_signer_creation(self):
        await self.assert_rejected_before_signer(
            self.body(), token="", status=503)

    async def test_invalid_or_expired_telegram_proof_is_rejected_before_signer_creation(self):
        with patch("backend.api.routes_user.auth.validate_init_data", return_value=None):
            await self.assert_rejected_before_signer(self.body(), status=401)

    async def test_missing_or_wrong_consent_is_rejected_before_signer_creation(self):
        with patch("backend.api.routes_user.auth.validate_init_data", return_value={"id": 42}):
            for body in (
                self.body(terms_accepted=False),
                self.body(terms_version="old-version"),
            ):
                await self.assert_rejected_before_signer(body, status=400)

    async def test_new_user_and_versioned_consent_are_persisted(self):
        response = Response()
        client = AsyncMock()
        client.wallet = "0xfunder"
        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), \
             patch("backend.api.routes_user.auth.validate_init_data", return_value={"id": 42, "username": "alice"}), \
             patch("backend.api.routes_user.wallet.create_signer", return_value={"address": "0xsigner", "private_key": "secret"}), \
             patch("backend.api.routes_user.wallet.make_clob_client", return_value=client), \
             patch("backend.api.routes_user.wallet.encrypt_private_key", return_value="encrypted"):
            result = await create_wallet(self.body(), self.request, response, db=self.db)

        self.assertEqual("0xfunder", result["address"])
        self.assertTrue(result["created"])
        self.assertEqual(1, await self.db.fetchval("SELECT COUNT(*) FROM users"))
        consent = await self.db.fetchone("SELECT * FROM user_consents WHERE user_id=?", ("0xfunder",))
        self.assertEqual(CURRENT_TERMS_VERSION, consent["terms_version"])
        self.assertEqual(42, consent["telegram_user_id"])
        self.assertTrue(consent["accepted_at"])

    async def test_new_user_and_consent_roll_back_together_if_consent_insert_fails(self):
        await self.db.execute(
            "CREATE TRIGGER reject_consent BEFORE INSERT ON user_consents "
            "BEGIN SELECT RAISE(ABORT, 'consent write failed'); END")
        response = Response()
        client = AsyncMock()
        client.wallet = "0xrollback"
        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), \
             patch("backend.api.routes_user.auth.validate_init_data", return_value={"id": 77}), \
             patch("backend.api.routes_user.wallet.create_signer", return_value={"address": "0xsigner", "private_key": "secret"}), \
             patch("backend.api.routes_user.wallet.make_clob_client", return_value=client), \
             patch("backend.api.routes_user.wallet.encrypt_private_key", return_value="encrypted"), \
             patch("backend.api.routes_user._create_rate_limited", return_value=False):
            with self.assertRaises(HTTPException) as ctx:
                await create_wallet(self.body(), self.request, response, db=self.db)
        self.assertEqual(503, ctx.exception.status_code)
        self.assertIn("operator reconciliation", ctx.exception.detail.lower())
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM users"))
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM user_consents"))
        claim = await self.db.fetchone(
            "SELECT state,last_error,signer_address,private_key_enc,lease_owner "
            "FROM wallet_creation_claims WHERE telegram_user_id=?", (77,))
        self.assertEqual("side_effect_started", claim["state"])
        self.assertEqual("persist_wallet: IntegrityError", claim["last_error"])
        self.assertEqual("0xsigner", claim["signer_address"])
        self.assertEqual("encrypted", claim["private_key_enc"])
        self.assertIsNotNone(claim["lease_owner"])

    async def test_existing_telegram_user_is_returned_without_generating_or_duplicating_wallet(self):
        await self.db.execute(
            "INSERT INTO users(id,signer_address,telegram_user_id,private_key_enc,created_at) VALUES(?,?,?,?,?)",
            ("0xexisting", "0xsigner", 42, "encrypted", "2026-01-01T00:00:00+00:00"),
        )
        response = Response()
        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), \
             patch("backend.api.routes_user.auth.validate_init_data", return_value={"id": 42}), \
             patch("backend.api.routes_user.wallet.create_signer") as create_signer:
            result = await create_wallet(self.body(), self.request, response, db=self.db)
        self.assertEqual("0xexisting", result["address"])
        self.assertFalse(result["created"])
        self.assertEqual(1, await self.db.fetchval("SELECT COUNT(*) FROM users"))
        self.assertEqual(1, await self.db.fetchval("SELECT COUNT(*) FROM user_consents"))
        create_signer.assert_not_called()


class ConsentSchemaTests(unittest.TestCase):
    def test_consent_table_is_covered_by_every_schema_source(self):
        self.assertIn("user_consents", TABLES)
        for sql in (SCHEMA_SQL, PG_SCHEMA_SQL):
            self.assertIn("CREATE TABLE IF NOT EXISTS user_consents", sql)
            self.assertIn("terms_version", sql)
            self.assertIn("accepted_at", sql)
        migration = open("supabase/migrations/0001_init.sql", encoding="utf-8").read()
        self.assertIn("CREATE TABLE IF NOT EXISTS user_consents", migration)


class LogoutRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_logout_revokes_only_the_cookie_identified_server_session_and_clears_cookie(self):
        raw = "raw-session-secret"
        db = AsyncMock()
        db.execute.return_value = 1
        request = SimpleNamespace(cookies={auth.SESSION_COOKIE: raw})
        response = Response()

        result = await logout(request=request, response=response, db=db)

        self.assertEqual({"ok": True}, result)
        sql, params = db.execute.await_args.args
        self.assertIn("api_token = NULL", sql)
        self.assertIn("WHERE api_token = ?", sql)
        self.assertEqual((auth.hash_session_token(raw),), params)
        self.assertIn("polytrade_session=", response.headers["set-cookie"])
        self.assertIn("Max-Age=0", response.headers["set-cookie"])


if __name__ == "__main__":
    unittest.main()
