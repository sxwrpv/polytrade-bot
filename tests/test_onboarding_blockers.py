from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException, Response

from backend.api.deps import get_current_user
from backend.api.routes_auth import LinkTelegram, link_telegram
from backend.api.routes_user import (
    CURRENT_FUNDING_ACK_VERSION,
    CURRENT_TERMS_VERSION,
    CreateWallet,
    FundingAcknowledgement,
    acknowledge_funding,
    create_wallet,
    deposit_address,
)
from backend.core import auth
from backend.db.database import Database
from backend.db.models import PG_SCHEMA_SQL, SCHEMA_SQL, TABLES


class SQLiteDBTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def insert_user(self, address: str, telegram_id=None):
        await self.db.execute(
            "INSERT INTO users(id,signer_address,telegram_user_id,private_key_enc,created_at) "
            "VALUES(?,?,?,?,?)",
            (address, address + "-signer", telegram_id, "encrypted", "2026-01-01T00:00:00+00:00"),
        )
        return await self.db.fetchone("SELECT * FROM users WHERE id=?", (address,))


class FundingAcknowledgementTests(SQLiteDBTest):
    async def test_deposit_addresses_fail_closed_until_versioned_ack_is_durable(self):
        user = await self.insert_user("0xfunded")
        pm = SimpleNamespace(create_bridge_address=AsyncMock(return_value={"address": {"evm": "0xbridge"}}))

        with self.assertRaises(HTTPException) as ctx:
            await deposit_address(user=user, db=self.db, pmc=pm)
        self.assertEqual(403, ctx.exception.status_code)
        pm.create_bridge_address.assert_not_awaited()

        result = await acknowledge_funding(
            FundingAcknowledgement(accepted=True, version=CURRENT_FUNDING_ACK_VERSION),
            user=user,
            db=self.db,
        )
        self.assertEqual({"accepted": True, "version": CURRENT_FUNDING_ACK_VERSION}, result)
        row = await self.db.fetchone(
            "SELECT * FROM funding_acknowledgements WHERE user_id=?", (user["id"],)
        )
        self.assertEqual(CURRENT_FUNDING_ACK_VERSION, row["version"])
        self.assertTrue(row["accepted_at"])

        addresses = await deposit_address(user=user, db=self.db, pmc=pm)
        self.assertEqual("0xbridge", addresses["addresses"][0]["address"])

    async def test_wrong_or_unchecked_funding_version_is_not_persisted(self):
        user = await self.insert_user("0xunchecked")
        for body in (
            FundingAcknowledgement(accepted=False, version=CURRENT_FUNDING_ACK_VERSION),
            FundingAcknowledgement(accepted=True, version="stale"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await acknowledge_funding(body, user=user, db=self.db)
            self.assertEqual(400, ctx.exception.status_code)
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM funding_acknowledgements"))


class LegacyTelegramLinkTests(SQLiteDBTest):
    async def test_authenticated_legacy_wallet_links_to_live_telegram_identity(self):
        user = await self.insert_user("0xlegacy")
        with patch("backend.api.routes_auth.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
            "backend.api.routes_auth.auth.validate_init_data", return_value={"id": 777, "username": "alice"}
        ):
            result = await link_telegram(LinkTelegram(init_data="signed"), user=user, db=self.db)

        self.assertEqual({"address": "0xlegacy", "linked": True, "telegram_user_id": 777}, result)
        linked = await self.db.fetchone("SELECT * FROM users WHERE id=?", ("0xlegacy",))
        self.assertEqual(777, linked["telegram_user_id"])
        self.assertEqual(1, await self.db.fetchval("SELECT COUNT(*) FROM users"))
        self.assertNotIn("private_key_enc", result)

    async def test_link_conflicts_when_telegram_identity_belongs_to_another_wallet(self):
        user = await self.insert_user("0xlegacy")
        await self.insert_user("0xother", telegram_id=777)
        with patch("backend.api.routes_auth.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
            "backend.api.routes_auth.auth.validate_init_data", return_value={"id": 777}
        ):
            with self.assertRaises(HTTPException) as ctx:
                await link_telegram(LinkTelegram(init_data="signed"), user=user, db=self.db)
        self.assertEqual(409, ctx.exception.status_code)
        current = await self.db.fetchone("SELECT telegram_user_id FROM users WHERE id=?", ("0xlegacy",))
        self.assertIsNone(current["telegram_user_id"])

    async def test_invalid_telegram_data_cannot_link(self):
        user = await self.insert_user("0xlegacy")
        with patch("backend.api.routes_auth.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
            "backend.api.routes_auth.auth.validate_init_data", return_value=None
        ):
            with self.assertRaises(HTTPException) as ctx:
                await link_telegram(LinkTelegram(init_data="bad"), user=user, db=self.db)
        self.assertEqual(401, ctx.exception.status_code)
        current = await self.db.fetchone("SELECT telegram_user_id FROM users WHERE id=?", ("0xlegacy",))
        self.assertIsNone(current["telegram_user_id"])

    async def test_no_authenticated_legacy_session_is_rejected(self):
        request = SimpleNamespace(cookies={}, app=SimpleNamespace(state=SimpleNamespace(db=self.db)))
        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(request)
        self.assertEqual(401, ctx.exception.status_code)


class WalletCreationClaimTests(SQLiteDBTest):
    def wallet_body(self):
        return CreateWallet(
            init_data="signed",
            terms_accepted=True,
            terms_version=CURRENT_TERMS_VERSION,
        )

    def request(self, *, cookie=None, host="203.0.113.50"):
        cookies = {} if cookie is None else {auth.SESSION_COOKIE: cookie}
        return SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(db=self.db)),
            client=SimpleNamespace(host=host), headers={}, cookies=cookies,
        )

    async def test_authenticated_legacy_session_cannot_create_a_second_wallet(self):
        legacy = await self.insert_user("0xlegacy-session")
        raw, stored, expires_at = auth.new_session()
        await self.db.execute(
            "UPDATE users SET api_token=?,api_token_expires_at=? WHERE id=?",
            (stored, expires_at, legacy["id"]),
        )
        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
            "backend.api.routes_user.auth.validate_init_data", return_value={"id": 6060}
        ), patch("backend.api.routes_user.wallet.create_signer") as create_signer:
            with self.assertRaises(HTTPException) as ctx:
                await create_wallet(self.wallet_body(), self.request(cookie=raw), Response(), db=self.db)
        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("link", ctx.exception.detail.lower())
        create_signer.assert_not_called()
        self.assertEqual(1, await self.db.fetchval("SELECT COUNT(*) FROM users"))

    async def test_invalid_session_cookie_does_not_disable_normal_wallet_creation(self):
        client = AsyncMock()
        client.wallet = "0xnew-funder"
        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
            "backend.api.routes_user.auth.validate_init_data", return_value={"id": 6061}
        ), patch(
            "backend.api.routes_user.wallet.create_signer",
            return_value={"address": "0xnew-signer", "private_key": "secret"},
        ), patch("backend.api.routes_user.wallet.make_clob_client", return_value=client), patch(
            "backend.api.routes_user.wallet.encrypt_private_key", return_value="encrypted"
        ), patch("backend.api.routes_user._create_rate_limited", return_value=False):
            result = await create_wallet(
                self.wallet_body(), self.request(cookie="invalid-cookie"), Response(), db=self.db
            )
        self.assertEqual("0xnew-funder", result["address"])

    async def test_link_winning_race_fences_create_before_wallet_side_effects(self):
        legacy = await self.insert_user("0xlegacy-race")
        link_db = Database(path=self.path, dsn="")
        create_db = Database(path=self.path, dsn="")
        await link_db.connect()
        await create_db.connect()
        bound_uncommitted = asyncio.Event()
        release_link = asyncio.Event()
        original_transaction = link_db.transaction

        @asynccontextmanager
        async def paused_transaction(*, write=False):
            async with original_transaction(write=write) as tx:
                original_execute = tx.execute

                async def execute(sql, params=()):
                    changed = await original_execute(sql, params)
                    if sql.startswith("UPDATE users SET telegram_user_id="):
                        bound_uncommitted.set()
                        await release_link.wait()
                    return changed

                tx.execute = execute
                yield tx

        link_db.transaction = paused_transaction
        signer = Mock(return_value={"address": "unused", "private_key": "unused"})
        unused_client = AsyncMock()
        unused_client.wallet = "unused-funder"
        request = self.request(host="203.0.113.61")
        try:
            with patch("backend.api.routes_auth.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
                "backend.api.routes_auth.auth.validate_init_data", return_value={"id": 7001}
            ), patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
                "backend.api.routes_user.auth.validate_init_data", return_value={"id": 7001}
            ), patch("backend.api.routes_user.wallet.create_signer", signer), patch(
                "backend.api.routes_user.wallet.make_clob_client", return_value=unused_client
            ), patch(
                "backend.api.routes_user._create_rate_limited", return_value=False
            ):
                linking = asyncio.create_task(
                    link_telegram(LinkTelegram(init_data="signed"), user=legacy, db=link_db)
                )
                await asyncio.wait_for(bound_uncommitted.wait(), timeout=2)
                creating = asyncio.create_task(
                    create_wallet(self.wallet_body(), request, Response(), db=create_db)
                )
                await asyncio.sleep(0.05)
                release_link.set()
                linked, created = await asyncio.gather(linking, creating)
        finally:
            release_link.set()
            await link_db.close()
            await create_db.close()
        self.assertEqual("0xlegacy-race", linked["address"])
        self.assertEqual("0xlegacy-race", created["address"])
        signer.assert_not_called()
        claim = await self.db.fetchone(
            "SELECT state FROM wallet_creation_claims WHERE telegram_user_id=?", (7001,)
        )
        self.assertEqual("complete", claim["state"])

    async def test_started_create_fences_link_without_binding_legacy_wallet(self):
        legacy = await self.insert_user("0xlegacy-loses-race")
        create_db = Database(path=self.path, dsn="")
        link_db = Database(path=self.path, dsn="")
        await create_db.connect()
        await link_db.connect()
        side_effect_started = asyncio.Event()
        release_create = asyncio.Event()
        client = AsyncMock()
        client.wallet = "0xcreated-race"

        async def make_client(_pk):
            side_effect_started.set()
            await release_create.wait()
            return client

        request = self.request(host="203.0.113.62")
        try:
            with patch("backend.api.routes_auth.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
                "backend.api.routes_auth.auth.validate_init_data", return_value={"id": 7002}
            ), patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
                "backend.api.routes_user.auth.validate_init_data", return_value={"id": 7002}
            ), patch(
                "backend.api.routes_user.wallet.create_signer",
                return_value={"address": "0xcreated-signer", "private_key": "secret"},
            ), patch("backend.api.routes_user.wallet.make_clob_client", side_effect=make_client), patch(
                "backend.api.routes_user.wallet.encrypt_private_key", return_value="encrypted"
            ), patch("backend.api.routes_user._create_rate_limited", return_value=False):
                creating = asyncio.create_task(
                    create_wallet(self.wallet_body(), request, Response(), db=create_db)
                )
                await asyncio.wait_for(side_effect_started.wait(), timeout=2)
                link_error = None
                try:
                    await link_telegram(LinkTelegram(init_data="signed"), user=legacy, db=link_db)
                except HTTPException as exc:
                    link_error = exc
                current = await link_db.fetchone(
                    "SELECT telegram_user_id FROM users WHERE id=?", (legacy["id"],)
                )
                release_create.set()
                create_error = None
                try:
                    created = await creating
                except HTTPException as exc:
                    create_error = exc
        finally:
            release_create.set()
            await create_db.close()
            await link_db.close()
        self.assertIsNotNone(link_error)
        self.assertEqual(409, link_error.status_code)
        self.assertIn("progress", link_error.detail.lower())
        self.assertIsNone(current["telegram_user_id"])
        self.assertIsNone(create_error)
        self.assertEqual("0xcreated-race", created["address"])

    async def test_independent_connections_serialize_before_irreversible_wallet_work(self):
        second_db = Database(path=self.path, dsn="")
        await second_db.connect()
        started = asyncio.Event()
        release = asyncio.Event()
        signer_calls = 0

        def create_signer():
            nonlocal signer_calls
            signer_calls += 1
            return {"address": "0xsigner", "private_key": "secret"}

        client = AsyncMock()
        client.wallet = "0xfunder"

        async def make_client(_pk):
            started.set()
            await release.wait()
            return client

        request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.50"), headers={})
        try:
            with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
                "backend.api.routes_user.auth.validate_init_data", return_value={"id": 4242}
            ), patch("backend.api.routes_user.wallet.create_signer", side_effect=create_signer), patch(
                "backend.api.routes_user.wallet.make_clob_client", side_effect=make_client
            ), patch("backend.api.routes_user.wallet.encrypt_private_key", return_value="encrypted"), patch(
                "backend.api.routes_user._create_rate_limited", return_value=False
            ):
                first = asyncio.create_task(
                    create_wallet(self.wallet_body(), request, Response(), db=self.db)
                )
                await asyncio.wait_for(started.wait(), timeout=2)
                with self.assertRaises(HTTPException) as ctx:
                    await create_wallet(self.wallet_body(), request, Response(), db=second_db)
                self.assertEqual(409, ctx.exception.status_code)
                release.set()
                result = await first
        finally:
            release.set()
            await second_db.close()

        self.assertEqual("0xfunder", result["address"])
        self.assertEqual(1, signer_calls)
        claim = await self.db.fetchone(
            "SELECT state FROM wallet_creation_claims WHERE telegram_user_id=?", (4242,)
        )
        self.assertEqual("complete", claim["state"])

    async def test_stale_pre_side_effect_claim_can_recover_but_started_claim_never_replays(self):
        await self.db.execute(
            "INSERT INTO wallet_creation_claims(telegram_user_id,claim_token,state,claimed_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (51, "dead", "claimed", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
        )
        recovered = await self.db.claim_wallet_creation(51, "new-token", stale_before="2021-01-01T00:00:00+00:00")
        self.assertTrue(recovered)

        await self.db.execute(
            "UPDATE wallet_creation_claims SET state='side_effect_started', claim_token='started' "
            "WHERE telegram_user_id=?", (51,)
        )
        replay = await self.db.claim_wallet_creation(51, "another", stale_before="2030-01-01T00:00:00+00:00")
        self.assertFalse(replay)

    async def test_deterministic_pre_side_effect_failure_releases_claim_for_immediate_retry(self):
        client = AsyncMock()
        client.wallet = "0xretry-funder"
        signer = Mock(side_effect=[
            RuntimeError("local entropy unavailable"),
            {"address": "0xretry-signer", "private_key": "secret"},
        ])
        patches = (
            patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"),
            patch("backend.api.routes_user.auth.validate_init_data", return_value={"id": 8101}),
            patch("backend.api.routes_user.wallet.create_signer", signer),
            patch("backend.api.routes_user.wallet.make_clob_client", return_value=client),
            patch("backend.api.routes_user.wallet.encrypt_private_key", return_value="encrypted"),
            patch("backend.api.routes_user._create_rate_limited", return_value=False),
        )
        with patches[0], patches[1], patches[2], patches[3] as make_client, patches[4], patches[5]:
            with self.assertRaises(HTTPException) as ctx:
                await create_wallet(self.wallet_body(), self.request(), Response(), db=self.db)
            self.assertEqual(503, ctx.exception.status_code)
            self.assertIn("retry", ctx.exception.detail.lower())
            make_client.assert_not_awaited()
            self.assertIsNone(await self.db.fetchone(
                "SELECT * FROM wallet_creation_claims WHERE telegram_user_id=?", (8101,)))

            result = await create_wallet(self.wallet_body(), self.request(), Response(), db=self.db)

        self.assertEqual("0xretry-funder", result["address"])
        self.assertTrue(result["created"])
        self.assertEqual(2, signer.call_count)

    async def test_sdk_early_read_failure_is_resumable_with_persisted_signer(self):
        client = AsyncMock()
        client.wallet = "0xretry-funder"
        signer = Mock(return_value={"address": "0xdurable-signer", "private_key": "durable-key"})
        make_client = AsyncMock(side_effect=[OSError("credential lookup failed"), client])
        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
            "backend.api.routes_user.auth.validate_init_data", return_value={"id": 8102}
        ), patch("backend.api.routes_user.wallet.create_signer", signer), patch(
            "backend.api.routes_user.wallet.make_clob_client", make_client
        ), patch(
            "backend.api.routes_user.wallet.encrypt_private_key", return_value="encrypted-durable-key"
        ), patch(
            "backend.api.routes_user.wallet.decrypt_private_key", return_value="durable-key"
        ), patch(
            "backend.api.routes_user.wallet.address_for_key", return_value="0xdurable-signer"
        ), patch("backend.api.routes_user._create_rate_limited", return_value=False):
            with self.assertRaises(HTTPException) as first:
                await create_wallet(self.wallet_body(), self.request(), Response(), db=self.db)
            self.assertEqual(503, first.exception.status_code)
            claim = await self.db.fetchone(
                "SELECT state,signer_address,private_key_enc,lease_owner FROM wallet_creation_claims "
                "WHERE telegram_user_id=?", (8102,))
            self.assertEqual("side_effect_started", claim["state"])
            self.assertEqual("0xdurable-signer", claim["signer_address"])
            self.assertEqual("encrypted-durable-key", claim["private_key_enc"])
            self.assertIsNone(claim["lease_owner"])

            result = await create_wallet(self.wallet_body(), self.request(), Response(), db=self.db)

        self.assertEqual("0xretry-funder", result["address"])
        self.assertEqual(1, signer.call_count)
        self.assertEqual(["durable-key", "durable-key"],
                         [call.args[0] for call in make_client.await_args_list])

    async def test_ambiguous_deployment_failure_resumes_only_exact_persisted_signer(self):
        dangerous = "relayer timed out private_key=SUPER-SECRET"
        client = AsyncMock()
        client.wallet = "0xambiguous-funder"
        make_client = AsyncMock(side_effect=[RuntimeError(dangerous), client])
        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
            "backend.api.routes_user.auth.validate_init_data", return_value={"id": 8102}
        ), patch(
            "backend.api.routes_user.wallet.create_signer",
            return_value={"address": "0xambiguous", "private_key": "SUPER-SECRET"},
        ) as signer, patch(
            "backend.api.routes_user.wallet.make_clob_client", make_client
        ), patch(
            "backend.api.routes_user.wallet.encrypt_private_key", return_value="encrypted"
        ), patch(
            "backend.api.routes_user.wallet.decrypt_private_key", return_value="SUPER-SECRET"
        ), patch(
            "backend.api.routes_user.wallet.address_for_key", return_value="0xambiguous"
        ), patch("backend.api.routes_user._create_rate_limited", return_value=False):
            with self.assertLogs("routes_user", level="ERROR") as logs, self.assertRaises(HTTPException) as ctx:
                await create_wallet(self.wallet_body(), self.request(host="203.0.113.80"), Response(), db=self.db)
            self.assertEqual(503, ctx.exception.status_code)
            self.assertIn("retry", ctx.exception.detail.lower())
            self.assertNotIn("SUPER-SECRET", ctx.exception.detail)
            self.assertNotIn(dangerous, "\n".join(logs.output))

            claim = await self.db.fetchone(
                "SELECT state,last_error,signer_address,private_key_enc FROM wallet_creation_claims "
                "WHERE telegram_user_id=?", (8102,))
            self.assertEqual("side_effect_started", claim["state"])
            self.assertTrue(claim["last_error"])
            self.assertIn("RuntimeError", claim["last_error"])
            self.assertNotIn("SUPER-SECRET", claim["last_error"])
            self.assertEqual("0xambiguous", claim["signer_address"])
            self.assertEqual("encrypted", claim["private_key_enc"])

            result = await create_wallet(
                self.wallet_body(), self.request(host="203.0.113.81"), Response(), db=self.db)
            self.assertEqual("0xambiguous-funder", result["address"])
        self.assertEqual(1, signer.call_count)
        self.assertEqual(["SUPER-SECRET", "SUPER-SECRET"],
                         [call.args[0] for call in make_client.await_args_list])
        completed = await self.db.fetchone(
            "SELECT state,private_key_enc,lease_owner FROM wallet_creation_claims "
            "WHERE telegram_user_id=?", (8102,))
        self.assertEqual("complete", completed["state"])
        self.assertIsNone(completed["private_key_enc"])
        self.assertIsNone(completed["lease_owner"])
        user = await self.db.fetchone("SELECT * FROM users WHERE telegram_user_id=?", (8102,))
        consent = await self.db.fetchone("SELECT * FROM user_consents WHERE user_id=?", (user["id"],))
        self.assertEqual("encrypted", user["private_key_enc"])
        self.assertEqual(CURRENT_TERMS_VERSION, consent["terms_version"])

    async def test_independent_connections_allow_only_one_resuming_sdk_call(self):
        await self.db.execute(
            "INSERT INTO wallet_creation_claims(telegram_user_id,claim_token,state,claimed_at,updated_at,"
            "signer_address,private_key_enc) VALUES(?,?,?,?,?,?,?)",
            (8103, "identity-token", "side_effect_started", "2020-01-01T00:00:00+00:00",
             "2020-01-01T00:00:00+00:00", "0xresume-signer", "resume-ciphertext"),
        )
        second_db = Database(path=self.path, dsn="")
        await second_db.connect()
        entered = asyncio.Event()
        release = asyncio.Event()
        client = AsyncMock()
        client.wallet = "0xresume-funder"

        async def make_client(private_key):
            self.assertEqual("resume-key", private_key)
            entered.set()
            await release.wait()
            return client

        signer = Mock()
        try:
            with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
                "backend.api.routes_user.auth.validate_init_data", return_value={"id": 8103}
            ), patch("backend.api.routes_user.wallet.create_signer", signer), patch(
                "backend.api.routes_user.wallet.make_clob_client", side_effect=make_client
            ) as sdk, patch(
                "backend.api.routes_user.wallet.decrypt_private_key", return_value="resume-key"
            ), patch(
                "backend.api.routes_user.wallet.address_for_key", return_value="0xresume-signer"
            ), patch("backend.api.routes_user._create_rate_limited", return_value=False):
                first = asyncio.create_task(create_wallet(
                    self.wallet_body(), self.request(host="203.0.113.82"), Response(), db=self.db))
                await asyncio.wait_for(entered.wait(), timeout=2)
                with self.assertRaises(HTTPException) as blocked:
                    await create_wallet(
                        self.wallet_body(), self.request(host="203.0.113.83"), Response(), db=second_db)
                self.assertEqual(409, blocked.exception.status_code)
                self.assertEqual(1, sdk.await_count)
                release.set()
                await first
        finally:
            release.set()
            await second_db.close()
        signer.assert_not_called()

    async def test_expired_started_owner_cannot_be_taken_over_while_sdk_is_active(self):
        second_db = Database(path=self.path, dsn="")
        await second_db.connect()
        entered = asyncio.Event()
        release = asyncio.Event()
        first = None

        async def make_client(_private_key):
            entered.set()
            await release.wait()
            client = AsyncMock()
            client.wallet = "0xexpiry-funder"
            return client

        try:
            with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
                "backend.api.routes_user.auth.validate_init_data", return_value={"id": 8104}
            ), patch(
                "backend.api.routes_user.wallet.create_signer",
                return_value={"address": "0xexpiry-signer", "private_key": "expiry-key"},
            ), patch("backend.api.routes_user.wallet.make_clob_client", side_effect=make_client), patch(
                "backend.api.routes_user.wallet.encrypt_private_key", return_value="expiry-ciphertext"
            ), patch("backend.api.routes_user._create_rate_limited", return_value=False):
                first = asyncio.create_task(create_wallet(
                    self.wallet_body(), self.request(host="203.0.113.84"), Response(), db=self.db))
                await asyncio.wait_for(entered.wait(), timeout=2)
                await second_db.execute(
                    "UPDATE wallet_creation_claims SET lease_expires_at=? WHERE telegram_user_id=?",
                    ("2020-01-01T00:00:00+00:00", 8104),
                )
                with self.assertRaises(HTTPException) as blocked:
                    await create_wallet(
                        self.wallet_body(), self.request(host="203.0.113.85"), Response(), db=second_db)
                self.assertEqual(409, blocked.exception.status_code)
                release.set()
                await first
        finally:
            release.set()
            if first is not None:
                await asyncio.gather(first, return_exceptions=True)
            await second_db.close()

    async def test_explicit_sdk_failure_release_allows_exactly_one_same_signer_resume(self):
        second_db = Database(path=self.path, dsn="")
        await second_db.connect()
        entered = asyncio.Event()
        release = asyncio.Event()
        resumed_client = AsyncMock()
        resumed_client.wallet = "0xreleased-funder"

        attempts = 0

        async def sdk_call(_private_key):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("returned failure")
            entered.set()
            await release.wait()
            return resumed_client

        make_client = AsyncMock(side_effect=sdk_call)
        signer = Mock(return_value={"address": "0xreleased-signer", "private_key": "released-key"})
        try:
            with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
                "backend.api.routes_user.auth.validate_init_data", return_value={"id": 8105}
            ), patch("backend.api.routes_user.wallet.create_signer", signer), patch(
                "backend.api.routes_user.wallet.make_clob_client", make_client
            ), patch(
                "backend.api.routes_user.wallet.encrypt_private_key", return_value="released-ciphertext"
            ), patch(
                "backend.api.routes_user.wallet.decrypt_private_key", return_value="released-key"
            ), patch(
                "backend.api.routes_user.wallet.address_for_key", return_value="0xreleased-signer"
            ), patch("backend.api.routes_user._create_rate_limited", return_value=False):
                with self.assertRaises(HTTPException):
                    await create_wallet(self.wallet_body(), self.request(), Response(), db=self.db)
                claim = await self.db.fetchone(
                    "SELECT lease_owner,last_error FROM wallet_creation_claims WHERE telegram_user_id=?",
                    (8105,),
                )
                self.assertIsNone(claim["lease_owner"])
                self.assertEqual("make_clob_client: OSError", claim["last_error"])
                winner = asyncio.create_task(create_wallet(
                    self.wallet_body(), self.request(host="203.0.113.86"), Response(), db=self.db))
                await asyncio.wait_for(entered.wait(), timeout=2)
                with self.assertRaises(HTTPException) as blocked:
                    await create_wallet(
                        self.wallet_body(), self.request(host="203.0.113.87"), Response(), db=second_db)
                self.assertEqual(409, blocked.exception.status_code)
                release.set()
                await winner
        finally:
            release.set()
            await second_db.close()
        self.assertEqual(1, signer.call_count)
        self.assertEqual(2, make_client.await_count)

    async def test_cancellation_during_sdk_call_retains_owner_and_has_no_renewal_task(self):
        entered = asyncio.Event()

        async def make_client(_private_key):
            entered.set()
            await asyncio.Event().wait()

        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
            "backend.api.routes_user.auth.validate_init_data", return_value={"id": 8106}
        ), patch(
            "backend.api.routes_user.wallet.create_signer",
            return_value={"address": "0xcancel-signer", "private_key": "cancel-key"},
        ), patch("backend.api.routes_user.wallet.make_clob_client", side_effect=make_client), patch(
            "backend.api.routes_user.wallet.encrypt_private_key", return_value="cancel-ciphertext"
        ), patch("backend.api.routes_user._create_rate_limited", return_value=False):
            task = asyncio.create_task(create_wallet(
                self.wallet_body(), self.request(host="203.0.113.88"), Response(), db=self.db))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            claim = await self.db.fetchone(
                "SELECT state,lease_owner FROM wallet_creation_claims WHERE telegram_user_id=?", (8106,))
            self.assertEqual("side_effect_started", claim["state"])
            self.assertIsNotNone(claim["lease_owner"])
            self.assertFalse(any(
                "renew_lease" in getattr(pending.get_coro(), "__qualname__", "")
                for pending in asyncio.all_tasks() if pending is not asyncio.current_task()
            ))
            with self.assertRaises(HTTPException) as blocked:
                await create_wallet(self.wallet_body(), self.request(), Response(), db=self.db)
            self.assertEqual(409, blocked.exception.status_code)

    async def test_cancellation_after_client_creation_attempts_shielded_cleanup(self):
        entered = asyncio.Event()
        client = AsyncMock()
        client.wallet = "0xcancel-cleanup-funder"

        async def wait_ready(_client):
            entered.set()
            await asyncio.Event().wait()

        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), patch(
            "backend.api.routes_user.auth.validate_init_data", return_value={"id": 8107}
        ), patch(
            "backend.api.routes_user.wallet.create_signer",
            return_value={"address": "0xcleanup-signer", "private_key": "cleanup-key"},
        ), patch("backend.api.routes_user.wallet.make_clob_client", return_value=client), patch(
            "backend.api.routes_user.wallet.wait_wallet_ready", side_effect=wait_ready
        ), patch(
            "backend.api.routes_user.wallet.encrypt_private_key", return_value="cleanup-ciphertext"
        ), patch("backend.api.routes_user._create_rate_limited", return_value=False):
            task = asyncio.create_task(create_wallet(
                self.wallet_body(), self.request(host="203.0.113.89"), Response(), db=self.db))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        client.close.assert_awaited_once()
        claim = await self.db.fetchone(
            "SELECT state,lease_owner FROM wallet_creation_claims WHERE telegram_user_id=?", (8107,))
        self.assertEqual("side_effect_started", claim["state"])
        self.assertIsNotNone(claim["lease_owner"])


class BlockerSchemaTests(unittest.TestCase):
    def test_new_invariants_exist_in_every_schema_source_and_additive_migration(self):
        self.assertIn("funding_acknowledgements", TABLES)
        self.assertIn("wallet_creation_claims", TABLES)
        for sql in (SCHEMA_SQL, PG_SCHEMA_SQL):
            self.assertIn("CREATE TABLE IF NOT EXISTS funding_acknowledgements", sql)
            self.assertIn("CREATE TABLE IF NOT EXISTS wallet_creation_claims", sql)
            self.assertIn("telegram_user_id", sql)
            self.assertIn("claim_token", sql)
        with open("supabase/migrations/0007_onboarding_invariants.sql", encoding="utf-8") as fh:
            migration = fh.read()
        self.assertIn("funding_acknowledgements", migration)
        self.assertIn("wallet_creation_claims", migration)
        with open("supabase/migrations/0008_resumable_wallet_creation.sql", encoding="utf-8") as fh:
            resumable = fh.read().lower()
        for column in ("signer_address", "private_key_enc", "lease_owner", "lease_expires_at"):
            self.assertIn(f"add column if not exists {column}", resumable)
        self.assertNotRegex(resumable, r"\b(drop table|truncate|delete from)\b")

    def test_0007_is_complete_idempotent_user_consents_upgrade_with_lockdown(self):
        with open("supabase/migrations/0007_onboarding_invariants.sql", encoding="utf-8") as fh:
            migration = fh.read()
        normalized = " ".join(migration.lower().split())
        self.assertIn("create table if not exists public.user_consents", normalized)
        self.assertIn("user_id text not null references public.users(id)", normalized)
        self.assertIn("primary key(user_id, terms_version)", normalized)
        self.assertRegex(normalized, r"create (?:unique )?index if not exists \w+ on public\.user_consents")
        self.assertIn("alter table public.user_consents enable row level security", normalized)
        self.assertIn("revoke all on table public.user_consents from anon, authenticated", normalized)
        self.assertIn("drop policy if exists no_api_access on public.user_consents", normalized)
        self.assertRegex(
            normalized,
            r"create policy no_api_access on public\.user_consents .* using \(false\) with check \(false\)",
        )
        self.assertNotRegex(normalized, r"\b(drop table|truncate|delete from)\b")


if __name__ == "__main__":
    unittest.main()
