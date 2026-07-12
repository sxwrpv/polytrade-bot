from __future__ import annotations

import asyncio
import math
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend.api.routes_positions import (
    CloseBody,
    CloseExternalBody,
    _notify_position,
    close_external_position,
    close_position,
    open_positions,
)
from backend.core.copy_engine import Action, CopyEngine
from backend.core.execution import OrderResult
from backend.db.database import Database, now_iso


class FakeOriginDB:
    async def fetchall(self, sql, params=()):
        if "FROM copy_positions" in sql:
            return []
        if "FROM copy_open_claims" in sql:
            return []
        raise AssertionError(f"unexpected fetchall: {sql}")

    async def fetchone(self, sql, params=()):
        if "token_id = ?" in sql and "trader_address" in sql:
            return {"trader_address": "0xleader", "opened_at": "2026-07-09T20:00:00Z"}
        raise AssertionError(f"unexpected fetchone: {sql}")


class FakePositionsPM:
    async def get_positions(self, user_id, size_threshold=0):
        return [SimpleNamespace(
            asset="bot-token", size=12.0, cur_price=0.42, cash_pnl=1.25,
            title="Bot-opened market", event_slug="event", slug="market",
            outcome="YES", avg_price=0.35, initial_value=4.2,
            redeemable=False, condition_id="condition",
        )]


class PositionOriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_unmanaged_live_holding_with_bot_history_is_conservatively_attributed(self):
        rows = await open_positions(
            user={"id": "user"}, db=FakeOriginDB(), pmc=FakePositionsPM())

        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["external"])
        self.assertEqual("bot_history", rows[0]["origin"])
        self.assertEqual("0xleader", rows[0]["trader_address"])


class CloseSlippageContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_position_notifier_receives_event(self):
        notifier = AsyncMock()
        request = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(position_notifier=notifier)))

        await _notify_position(request, {"event": "closed", "position_id": "p"})

        notifier.assert_awaited_once_with({"event": "closed", "position_id": "p"})

    async def test_external_close_passes_selected_slippage_to_execution(self):
        body = CloseExternalBody(token_id="bot-token", acceptable_slippage_pct=7.5)
        tx = SimpleNamespace(fetchone=AsyncMock(return_value=None), execute=AsyncMock())

        @asynccontextmanager
        async def transaction(**kwargs):
            yield tx

        db = SimpleNamespace(
            is_pg=False,
            transaction=transaction,
            execute=AsyncMock(),
        )
        pm = FakePositionsPM()
        result = OrderResult(ok=False, reason="test stop")

        with (
            patch("backend.api.routes_positions.get_user_client", AsyncMock(return_value=object())),
            patch("backend.api.routes_positions.execution.place_market_order", AsyncMock(return_value=result)) as place,
        ):
            response = await close_external_position(
                body, SimpleNamespace(), user={"id": "user"}, db=db, pmc=pm)

        self.assertFalse(response["ok"])
        self.assertEqual(7.5, place.await_args.kwargs["max_slippage_pct"])

    async def test_managed_close_passes_selected_slippage_to_execution(self):
        row = {
            "id": "position", "token_id": "bot-token", "shares": 12.0,
            "entry_price": 0.35, "notional_usd": 4.2,
        }
        db = SimpleNamespace(
            fetchone=AsyncMock(return_value=row),
            claim_managed_sell=AsyncMock(return_value=True),
            try_transition=AsyncMock(return_value=True),
            execute=AsyncMock(),
        )
        result = OrderResult(ok=False, reason="test stop")

        with (
            patch("backend.api.routes_positions.get_user_client", AsyncMock(return_value=object())),
            patch("backend.api.routes_positions.execution.place_market_order", AsyncMock(return_value=result)) as place,
        ):
            response = await close_position(
                "position", SimpleNamespace(), CloseBody(acceptable_slippage_pct=4.5),
                user={"id": "user"}, db=db, pmc=FakePositionsPM())

        self.assertFalse(response["ok"])
        self.assertEqual(4.5, place.await_args.kwargs["max_slippage_pct"])

    def test_close_slippage_rejects_values_outside_zero_to_ten(self):
        from pydantic import ValidationError

        CloseExternalBody(token_id="token", acceptable_slippage_pct=0)
        CloseExternalBody(token_id="token", acceptable_slippage_pct=10)
        CloseBody(acceptable_slippage_pct=0)
        CloseBody(acceptable_slippage_pct=10)
        for value in (-0.1, 10.1, math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                CloseExternalBody(token_id="token", acceptable_slippage_pct=value)
            with self.subTest(value=value), self.assertRaises(ValidationError):
                CloseBody(acceptable_slippage_pct=value)

    def test_configured_close_slippage_default_cannot_bypass_validation(self):
        from backend.config import validate_slippage_pct

        self.assertEqual(2.5, validate_slippage_pct(2.5, "TEST"))
        for value in (-0.1, 10.1, math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_slippage_pct(value, "TEST")


class CloseClaimSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.db.execute(
            "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
            ("user", "encrypted", now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def _insert_position(self, *, status="open", trader="0xleader", pid="position"):
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_slug,market_title,outcome,shares,entry_price,notional_usd,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, "user", trader, "condition", "bot-token", "market", "Market", "YES",
             12.0, 0.35, 4.2, status, now_iso()))

    async def test_managed_ambiguous_sell_remains_fenced(self):
        await self._insert_position()
        uncertain = OrderResult(ok=False, reason="timeout", submission_uncertain=True)
        with (
            patch("backend.api.routes_positions.get_user_client", AsyncMock(return_value=object())),
            patch("backend.api.routes_positions.execution.place_market_order", AsyncMock(return_value=uncertain)),
        ):
            response = await close_position(
                "position", SimpleNamespace(), CloseBody(), user={"id": "user"},
                db=self.db, pmc=FakePositionsPM())
        self.assertFalse(response["ok"])
        self.assertEqual("closing", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id=?", ("position",)))
        visible = await open_positions(user={"id": "user"}, db=self.db, pmc=FakePositionsPM())
        self.assertEqual("closing", visible[0]["status"])
        self.assertFalse(visible[0].get("external", False))

    async def test_managed_pre_submission_exception_restores_open(self):
        await self._insert_position()
        with patch("backend.api.routes_positions.get_user_client", AsyncMock(side_effect=RuntimeError("key"))):
            with self.assertRaises(RuntimeError):
                await close_position(
                    "position", SimpleNamespace(), CloseBody(), user={"id": "user"},
                    db=self.db, pmc=FakePositionsPM())
        self.assertEqual("open", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id=?", ("position",)))

    async def test_external_close_claim_is_race_safe_and_keeps_origin_unknown(self):
        await self._insert_position(status="closed", pid="history")
        calls = 0

        async def place(*args, **kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return OrderResult(ok=False, reason="timeout", submission_uncertain=True)

        async def attempt():
            return await close_external_position(
                CloseExternalBody(token_id="bot-token"), SimpleNamespace(),
                user={"id": "user"}, db=self.db, pmc=FakePositionsPM())

        with (
            patch("backend.api.routes_positions.get_user_client", AsyncMock(return_value=object())),
            patch("backend.api.routes_positions.execution.place_market_order", side_effect=place),
        ):
            results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)

        self.assertEqual(1, calls)
        self.assertEqual(1, sum(isinstance(x, Exception) for x in results))
        claim = await self.db.fetchone(
            "SELECT status,trader_address FROM copy_positions WHERE id != ?", ("history",))
        self.assertEqual("closing", claim["status"])
        self.assertEqual("manual", claim["trader_address"])

    async def test_external_close_rejects_active_buy_claim_across_connections(self):
        second = Database(path=self.path, dsn="")
        await second.connect()
        await self.db.execute(
            "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,state,"
            "reserved_usd,risk_revision,claimed_at,updated_at) VALUES(?,?,?,?,?,'reserved',?,?,?,?)",
            ("user", "bot-token", "0xleader", "buy-claim", "open", 4.2, 0,
             now_iso(), now_iso()))
        try:
            with self.assertRaises(HTTPException) as raised:
                await close_external_position(
                    CloseExternalBody(token_id="bot-token"), SimpleNamespace(),
                    user={"id": "user"}, db=second, pmc=FakePositionsPM())
            self.assertEqual(409, raised.exception.status_code)
            self.assertEqual(0, await self.db.fetchval(
                "SELECT COUNT(*) FROM copy_positions WHERE status='closing'"))
        finally:
            await second.close()

    async def test_external_close_racing_buy_reservation_creates_one_fence(self):
        await self.db.execute(
            "INSERT INTO followed_traders(id,user_id,trader_address,copy_ratio_pct,max_position_usd,"
            "paused,is_active,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("follow", "user", "0xleader", 1.0, 50.0, 0, 1, now_iso()))
        second = Database(path=self.path, dsn="")
        await second.connect()
        leader = SimpleNamespace(
            proxy_wallet="0xleader", asset="bot-token", condition_id="condition",
            size=100.0, avg_price=0.42, cur_price=0.42, current_value=42.0,
            redeemable=False, outcome="YES", slug="market", title="Market")
        action = Action(
            kind="open", token_id="bot-token", condition_id="condition", outcome="YES",
            side="BUY", amount=4.2, notional_usd=4.2, reference_price=0.42,
            trader_shares=100.0, position=leader)
        engine = CopyEngine(second, SimpleNamespace())
        uncertain = OrderResult(ok=False, reason="timeout", submission_uncertain=True)
        try:
            with (
                patch("backend.api.routes_positions.get_user_client", AsyncMock(return_value=object())),
                patch("backend.api.routes_positions.execution.place_market_order", AsyncMock(return_value=uncertain)),
            ):
                results = await asyncio.gather(
                    close_external_position(
                        CloseExternalBody(token_id="bot-token"), SimpleNamespace(),
                        user={"id": "user"}, db=self.db, pmc=FakePositionsPM()),
                    engine._prepare_buy("user", action), return_exceptions=True)
            fences = int(await self.db.fetchval(
                "SELECT COUNT(*) FROM copy_positions WHERE token_id='bot-token' "
                "AND status IN ('open','closing','reconciliation_required')"))
            fences += int(await self.db.fetchval(
                "SELECT COUNT(*) FROM copy_open_claims WHERE token_id='bot-token' "
                "AND state IN ('reserved','submitting','uncertain')"))
            self.assertEqual(1, fences, results)
        finally:
            await second.close()

    async def test_open_positions_shows_pending_uncertain_buy_without_live_shares(self):
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,state,"
            "reserved_usd,risk_revision,claimed_at,updated_at,last_error) "
            "VALUES(?,?,?,?,?,'uncertain',?,?,?,?,?)",
            ("user", "pending-token", "0xleader", "claim-token", "open", 7.25, 0,
             ts, ts, "submission timed out"))
        rows = await open_positions(
            user={"id": "user"}, db=self.db,
            pmc=SimpleNamespace(get_positions=AsyncMock(return_value=[])))
        self.assertEqual(1, len(rows))
        pending = rows[0]
        self.assertEqual("pending-token", pending["token_id"])
        self.assertEqual("reconciliation_required", pending["status"])
        self.assertTrue(pending["reconciliation_required"])
        self.assertEqual("claim-token", pending["claim_id"])
        self.assertEqual("uncertain", pending["claim_state"])
        self.assertEqual("submission timed out", pending["claim_error"])
        self.assertEqual(7.25, pending["reserved_usd"])
        self.assertEqual(0, pending["shares"])

    async def test_open_positions_merges_live_shares_into_buy_claim_not_external(self):
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,state,"
            "reserved_usd,risk_revision,claimed_at,updated_at) "
            "VALUES(?,?,?,?,?,'submitting',?,?,?,?)",
            ("user", "bot-token", "0xleader", "claim-live", "resize", 4.2, 0, ts, ts))
        rows = await open_positions(user={"id": "user"}, db=self.db, pmc=FakePositionsPM())
        self.assertEqual(1, len(rows))
        merged = rows[0]
        self.assertEqual("reconciliation_required", merged["status"])
        self.assertEqual("submitting", merged["claim_state"])
        self.assertEqual(12.0, merged["shares"])
        self.assertFalse(merged.get("external", False))

    async def test_managed_close_rejects_active_buy_claim(self):
        await self._insert_position()
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,state,"
            "reserved_usd,risk_revision,claimed_at,updated_at) "
            "VALUES(?,?,?,?,?,'reserved',?,?,?,?)",
            ("user", "bot-token", "0xleader", "resize-buy", "resize", 2.0, 0, ts, ts))
        place = AsyncMock(return_value=OrderResult(ok=True, filled_shares=12, avg_price=.4))
        with (
            patch("backend.api.routes_positions.get_user_client", AsyncMock(return_value=object())),
            patch("backend.api.routes_positions.execution.place_market_order", place),
        ):
            with self.assertRaises(HTTPException) as raised:
                await close_position("position", SimpleNamespace(), CloseBody(),
                                     user={"id": "user"}, db=self.db, pmc=FakePositionsPM())
        self.assertEqual(409, raised.exception.status_code)
        place.assert_not_awaited()
        self.assertEqual("open", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id='position'"))

    async def test_managed_close_rolls_back_position_when_event_insert_fails(self):
        await self._insert_position()
        await self.db.execute(
            "CREATE TRIGGER reject_events BEFORE INSERT ON trade_events "
            "BEGIN SELECT RAISE(ABORT, 'event failure'); END")
        result = OrderResult(ok=True, filled_shares=12, avg_price=.45)
        with (
            patch("backend.api.routes_positions.get_user_client", AsyncMock(return_value=object())),
            patch("backend.api.routes_positions.execution.place_market_order", AsyncMock(return_value=result)),
        ):
            with self.assertRaises(Exception):
                await close_position("position", SimpleNamespace(), CloseBody(),
                                     user={"id": "user"}, db=self.db, pmc=FakePositionsPM())
        self.assertEqual("closing", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id='position'"))
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM trade_events"))

    async def test_external_close_rolls_back_position_when_event_insert_fails(self):
        await self.db.execute(
            "CREATE TRIGGER reject_events BEFORE INSERT ON trade_events "
            "BEGIN SELECT RAISE(ABORT, 'event failure'); END")
        result = OrderResult(ok=True, filled_shares=12, avg_price=.45)
        with (
            patch("backend.api.routes_positions.get_user_client", AsyncMock(return_value=object())),
            patch("backend.api.routes_positions.execution.place_market_order", AsyncMock(return_value=result)),
        ):
            with self.assertRaises(Exception):
                await close_external_position(
                    CloseExternalBody(token_id="bot-token"), SimpleNamespace(),
                    user={"id": "user"}, db=self.db, pmc=FakePositionsPM())
        self.assertEqual("closing", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE token_id='bot-token'"))
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM trade_events"))


class LegacyUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_preserves_duplicates_and_prefers_closing_fence(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(path=path, dsn="")
        await db.connect()
        try:
            await db.init()
            await db.execute(
                "INSERT INTO users(id,private_key_enc,created_at) VALUES(?,?,?)",
                ("legacy-user", "encrypted", now_iso()))
            await db.execute("DROP INDEX uq_active_position_per_token")
            base = ("legacy-user", "0xleader", "condition", "legacy-token", "market",
                    "Market", "YES", 10.0, 0.5, 5.0)
            await db.execute(
                "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,market_slug,"
                "market_title,outcome,shares,entry_price,notional_usd,status,opened_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("new-open", *base, "open", "2026-07-10T12:00:00+00:00"))
            await db.execute(
                "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,market_slug,"
                "market_title,outcome,shares,entry_price,notional_usd,status,opened_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("old-closing", *base, "closing", "2026-07-09T12:00:00+00:00"))

            await db.init()
            await db.init()

            rows = await db.fetchall(
                "SELECT id,status FROM copy_positions WHERE token_id='legacy-token' ORDER BY id")
            self.assertEqual(2, len(rows))
            self.assertEqual("closing",
                             next(r["status"] for r in rows if r["id"] == "old-closing"))
            self.assertEqual("reconciliation_required",
                             next(r["status"] for r in rows if r["id"] == "new-open"))
            visible = await open_positions(
                user={"id": "legacy-user"}, db=db,
                pmc=SimpleNamespace(get_positions=AsyncMock(return_value=[])))
            reconciled = next(r for r in visible if r["id"] == "new-open")
            self.assertTrue(reconciled["reconciliation_required"])
        finally:
            await db.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
