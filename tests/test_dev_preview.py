from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import Response

from backend.api.routes_auth import dev_login
from backend.core import auth
from backend.core.dev_preview import DEV_PREVIEW_USER, ensure_dev_preview_user
from backend.db.database import Database


class DevPreviewAuthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        await self.db.connect()
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.tmp.name)

    async def test_seed_creates_one_local_wallet(self):
        with patch("backend.core.dev_preview.DEV_PREVIEW", True), patch(
            "backend.core.dev_preview.ENCRYPTION_SECRET", "test-secret"
        ):
            first = await ensure_dev_preview_user(self.db)
            second = await ensure_dev_preview_user(self.db)
        self.assertEqual(DEV_PREVIEW_USER, first)
        self.assertEqual(first, second)
        row = await self.db.fetchone("SELECT id, signer_address FROM users WHERE id=?", (first,))
        self.assertIsNotNone(row)
        self.assertNotEqual(row["id"], row["signer_address"])

    async def test_dev_login_disabled_by_default(self):
        with patch("backend.api.routes_auth.DEV_PREVIEW", False):
            with self.assertRaises(Exception) as ctx:
                await dev_login(Response(), db=self.db)
        self.assertEqual(404, ctx.exception.status_code)

    async def test_dev_login_issues_session_cookie(self):
        with patch("backend.api.routes_auth.DEV_PREVIEW", True), patch(
            "backend.core.dev_preview.DEV_PREVIEW", True
        ), patch("backend.core.dev_preview.ENCRYPTION_SECRET", "test-secret"):
            response = Response()
            result = await dev_login(response, db=self.db)
        self.assertEqual(DEV_PREVIEW_USER, result["address"])
        self.assertIn(auth.SESSION_COOKIE, response.headers.get("set-cookie", ""))
        user = await self.db.fetchone("SELECT api_token FROM users WHERE id=?", (DEV_PREVIEW_USER,))
        self.assertTrue(user["api_token"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
