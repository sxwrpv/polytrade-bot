from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from backend.core.copy_engine import Action, CopyEngine
from backend.core.execution import OrderResult
from backend.db.database import Database, now_iso


USER = "0x" + "1" * 40
TRADER = "0x" + "2" * 40
TOKEN = "token-1"


def position(*, price: float = 0.50, value: float = 1000.0):
    return SimpleNamespace(
        proxy_wallet=TRADER,
        asset=TOKEN,
        condition_id="condition-1",
        size=value / price,
        avg_price=price,
        cur_price=price,
        current_value=value,
        redeemable=False,
        outcome="YES",
        slug="market",
        title="Market",
    )


def open_action(*, amount: float = 20.0, price: float = 0.50,
                token: str = TOKEN) -> Action:
    p = position(price=price)
    p.asset = token
    p.condition_id = f"condition-{token}"
    return Action(
        kind="open",
        token_id=token,
        condition_id=p.condition_id,
        outcome="YES",
        side="BUY",
        amount=amount,
        notional_usd=amount,
        reference_price=price,
        trader_shares=p.size,
        position=p,
    )


def close_action(row: dict) -> Action:
    return Action(kind="close", token_id=TOKEN, side="SELL", amount=row["shares"], row=row)


class CopySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.db.execute(
            "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
            (USER, "encrypted", now_iso()),
        )
        await self.db.execute(
            "INSERT INTO followed_traders(id, user_id, trader_address, copy_ratio_pct, "
            "max_position_usd, paused, is_active, created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("follow-1", USER, TRADER, 1.0, 50.0, 0, 1, now_iso()),
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def test_open_fill_sends_position_alert_after_persistence(self):
        alerts = []

        async def notify(event):
            alerts.append(event)

        async def place(*args, **kwargs):
            return OrderResult(ok=True, filled_shares=20, avg_price=0.5)

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place,
                            position_notifier=notify)
        spent = await engine._execute(USER, object(), open_action(amount=10))

        self.assertEqual(10.0, spent)
        self.assertEqual(1, len(alerts))
        self.assertEqual("opened", alerts[0]["event"])
        self.assertEqual("Market", alerts[0]["market_title"])
        self.assertEqual(10.0, alerts[0]["notional_usd"])

    async def test_closed_position_sends_pnl_alert_after_persistence(self):
        alerts = []

        async def notify(event):
            alerts.append(event)

        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,entry_price,notional_usd,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'closing',?)",
            ("position", USER, TRADER, "condition", TOKEN, "Market", "YES",
             10.0, 0.4, 4.0, now_iso()))
        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='position'")
        engine = CopyEngine(self.db, SimpleNamespace(), position_notifier=notify)

        await engine._close_row(USER, row, 0.6, 10.0)

        self.assertEqual(1, len(alerts))
        self.assertEqual("closed", alerts[0]["event"])
        self.assertAlmostEqual(2.0, alerts[0]["realized_pnl"])
        self.assertEqual(0.6, alerts[0]["exit_price"])

    async def test_execute_rechecks_pause_immediately_before_buy(self):
        calls = 0

        async def place(*args, **kwargs):
            nonlocal calls
            calls += 1
            return OrderResult(ok=True, filled_shares=40, avg_price=0.5)

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place)
        # Simulate an action planned from an old enabled snapshot, then the user
        # receives a successful pause response before execution reaches the CLOB.
        await self.db.execute(
            "UPDATE followed_traders SET paused=1 WHERE user_id=? AND trader_address=?",
            (USER, TRADER),
        )

        spent = await engine._execute(USER, object(), open_action())

        self.assertEqual(0.0, spent)
        self.assertEqual(0, calls)
        self.assertEqual(
            0,
            await self.db.fetchval("SELECT COUNT(*) FROM copy_positions"),
        )

    async def test_concurrent_open_paths_submit_only_one_exchange_order(self):
        calls = 0

        async def place(*args, **kwargs):
            nonlocal calls
            calls += 1
            # Force both engines into the old read/order/write race.
            await asyncio.sleep(0.05)
            return OrderResult(ok=True, filled_shares=40, avg_price=0.5)

        first = CopyEngine(self.db, SimpleNamespace(), place_order=place)
        second = CopyEngine(self.db, SimpleNamespace(), place_order=place)

        await asyncio.gather(
            first._execute(USER, object(), open_action()),
            second._execute(USER, object(), open_action()),
        )

        self.assertEqual(1, calls, "dedupe must happen before the real order")
        self.assertEqual(
            1,
            await self.db.fetchval(
                "SELECT COUNT(*) FROM copy_positions WHERE user_id=? AND token_id=? AND status='open'",
                (USER, TOKEN),
            ),
        )

    async def test_execute_applies_latest_max_trade_limit(self):
        submitted_amounts: list[float] = []

        async def place(client, pm, token, side, amount, **kwargs):
            submitted_amounts.append(amount)
            return OrderResult(ok=True, filled_shares=amount / 0.5, avg_price=0.5)

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place)
        # The action was planned at $20, then the user lowered the cap to $8.
        await self.db.execute(
            "UPDATE followed_traders SET max_position_usd=8 WHERE user_id=? AND trader_address=?",
            (USER, TRADER),
        )

        await engine._execute(USER, object(), open_action(amount=20))

        self.assertEqual([8.0], submitted_amounts)


    async def test_distinct_tokens_share_aggregate_exposure_across_connections(self):
        await self.db.execute("UPDATE users SET max_total_exposure_usd=10 WHERE id=?", (USER,))
        await self.db.execute(
            "UPDATE followed_traders SET max_position_usd=8 WHERE user_id=? AND trader_address=?",
            (USER, TRADER))
        second_db = Database(path=self.path, dsn="")
        await second_db.connect()
        submitted: list[float] = []

        async def place(client, pm, token, side, amount, **kwargs):
            submitted.append(float(amount))
            await asyncio.sleep(0.05)
            return OrderResult(ok=True, filled_shares=amount / 0.5, avg_price=0.5)

        try:
            first = CopyEngine(self.db, SimpleNamespace(), place_order=place)
            second = CopyEngine(second_db, SimpleNamespace(), place_order=place)
            await asyncio.gather(
                first._execute(USER, object(), open_action(amount=8, token="token-a")),
                second._execute(USER, object(), open_action(amount=8, token="token-b")),
            )
            self.assertLessEqual(sum(submitted), 10.0)
            total = await self.db.fetchval(
                "SELECT COALESCE(SUM(notional_usd),0) FROM copy_positions WHERE user_id=? AND status='open'",
                (USER,))
            self.assertLessEqual(float(total), 10.0)
        finally:
            await second_db.close()

    async def test_risk_revision_fences_reserved_buy_before_submission(self):
        engine = CopyEngine(self.db, SimpleNamespace(), place_order=None)
        prepared = await engine._prepare_buy(USER, open_action())
        self.assertIsNotNone(prepared)
        reserved, _ = prepared
        async with self.db.transaction(write=True) as tx:
            await tx.execute("UPDATE users SET paused=1,risk_revision=risk_revision+1 WHERE id=?", (USER,))
        self.assertFalse(await engine._mark_claim_submitting(USER, reserved))
        await engine._release_buy_claim(USER, reserved.token_id, reserved.claim_id)

    async def test_ambiguous_submission_retains_uncertain_claim(self):
        async def place(*args, **kwargs):
            return OrderResult(ok=False, reason="api_error: timeout", submission_uncertain=True)

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place)
        await engine._execute(USER, object(), open_action())
        claim = await self.db.fetchone(
            "SELECT state FROM copy_open_claims WHERE user_id=? AND token_id=?", (USER, TOKEN))
        self.assertEqual("uncertain", claim["state"])

    async def test_buy_exception_before_submission_releases_claim(self):
        async def place(*args, **kwargs):
            raise RuntimeError("preflight failed")

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place)
        with self.assertRaisesRegex(RuntimeError, "preflight failed"):
            await engine._execute(USER, object(), open_action())

        self.assertEqual(0, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_open_claims WHERE user_id=? AND token_id=?",
            (USER, TOKEN)))

    async def test_master_user_pause_blocks_buy(self):
        calls = 0

        async def place(*args, **kwargs):
            nonlocal calls
            calls += 1
            return OrderResult(ok=True, filled_shares=40, avg_price=0.5)

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place)
        await self.db.execute("UPDATE users SET paused=1 WHERE id=?", (USER,))

        await engine._execute(USER, object(), open_action())

        self.assertEqual(0, calls)

    async def test_ambiguous_sell_remains_closing_and_cannot_be_retried(self):
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,market_slug,"
            "market_title,outcome,shares,entry_price,notional_usd,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?)",
            ("position", USER, TRADER, "condition", TOKEN, "market", "Market", "YES",
             10.0, 0.5, 5.0, now_iso()))
        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='position'")
        calls = 0

        async def place(*args, **kwargs):
            nonlocal calls
            calls += 1
            return OrderResult(ok=False, reason="timeout", submission_uncertain=True)

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place)
        await engine._execute(USER, object(), close_action(row))
        await engine._execute(USER, object(), close_action(row))

        self.assertEqual(1, calls)
        self.assertEqual("closing", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id='position'"))

    async def test_sell_exception_before_submission_restores_open(self):
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,market_slug,"
            "market_title,outcome,shares,entry_price,notional_usd,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?)",
            ("position", USER, TRADER, "condition", TOKEN, "market", "Market", "YES",
             10.0, 0.5, 5.0, now_iso()))
        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='position'")

        async def place(*args, **kwargs):
            raise RuntimeError("preflight")

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place)
        with self.assertRaises(RuntimeError):
            await engine._execute(USER, object(), close_action(row))
        self.assertEqual("open", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id='position'"))

    def test_db_slippage_bypass_is_rejected_before_ordering(self):
        follow = {"max_position_usd": 15.0, "max_slippage_pct": float("nan")}
        with self.assertRaises(ValueError):
            CopyEngine._follow_risk(follow)

    async def test_fast_leader_sell_ambiguity_remains_fenced(self):
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,market_slug,"
            "market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
            ("position", USER, TRADER, "condition", TOKEN, "market", "Market", "YES",
             10.0, 10.0, 0.5, 5.0, now_iso()))
        follow = await self.db.fetchone(
            "SELECT * FROM followed_traders WHERE user_id=? AND trader_address=?", (USER, TRADER))
        calls = 0

        async def place(*args, **kwargs):
            nonlocal calls
            calls += 1
            return OrderResult(ok=False, reason="timeout", submission_uncertain=True)

        async def client_factory(user):
            return object()

        engine = CopyEngine(
            self.db, SimpleNamespace(), place_order=place, client_factory=client_factory)
        trade = SimpleNamespace(
            timestamp=1, asset=TOKEN, side="SELL", size=10.0, price=0.5)
        await engine._handle_leader_trade(follow, trade)
        await engine._handle_leader_trade(follow, trade)

        self.assertEqual(1, calls)
        self.assertEqual("closing", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id='position'"))

    async def _insert_open_position(self):
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,market_slug,"
            "market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
            ("position", USER, TRADER, "condition", TOKEN, "market", "Market", "YES",
             10.0, 10.0, 0.5, 5.0, now_iso()))
        return await self.db.fetchone("SELECT * FROM copy_positions WHERE id='position'")

    async def test_resize_buy_and_managed_sell_cannot_cross_submit_across_connections(self):
        row = await self._insert_open_position()
        second_db = Database(path=self.path, dsn="")
        await second_db.connect()
        sides: list[str] = []

        async def place(client, pm, token, side, amount, **kwargs):
            sides.append(side)
            await asyncio.sleep(.05)
            return OrderResult(ok=False, reason="test stop")

        leader = position(value=10)
        resize = Action(
            kind="resize", subkind="increase", token_id=TOKEN, side="BUY", amount=2,
            notional_usd=2, reference_price=.5, trader_shares=12, row=row,
            position=leader, trader_address=TRADER)
        sell = close_action(row)
        try:
            await asyncio.gather(
                CopyEngine(self.db, SimpleNamespace(), place_order=place)._execute(
                    USER, object(), resize),
                CopyEngine(second_db, SimpleNamespace(), place_order=place)._execute(
                    USER, object(), sell),
            )
            self.assertEqual(1, len(sides), sides)
        finally:
            await second_db.close()

    async def test_engine_full_close_rolls_back_position_when_event_fails(self):
        await self._insert_open_position()
        await self.db.execute("UPDATE copy_positions SET status='closing' WHERE id='position'")
        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='position'")
        await self.db.execute(
            "CREATE TRIGGER reject_events BEFORE INSERT ON trade_events "
            "BEGIN SELECT RAISE(ABORT, 'event failure'); END")
        engine = CopyEngine(self.db, SimpleNamespace())
        with self.assertRaises(Exception):
            await engine._close_row(USER, row, .6, 10)
        self.assertEqual("closing", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id='position'"))
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM trade_events"))

    async def test_engine_resize_sell_rolls_back_position_when_event_fails(self):
        row = await self._insert_open_position()
        await self.db.execute("UPDATE copy_positions SET status='closing' WHERE id='position'")
        await self.db.execute(
            "CREATE TRIGGER reject_events BEFORE INSERT ON trade_events "
            "BEGIN SELECT RAISE(ABORT, 'event failure'); END")
        action = Action(kind="resize", subkind="decrease", token_id=TOKEN, side="SELL",
                        amount=2, trader_shares=8, row=row)
        result = OrderResult(ok=True, filled_shares=2, avg_price=.6)
        engine = CopyEngine(self.db, SimpleNamespace())
        with self.assertRaises(Exception):
            await engine._record_resize(USER, action, result)
        persisted = await self.db.fetchone(
            "SELECT shares,status FROM copy_positions WHERE id='position'")
        self.assertEqual(10, persisted["shares"])
        self.assertEqual("closing", persisted["status"])
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM trade_events"))

    async def test_fast_partial_sell_rolls_back_position_when_event_fails(self):
        await self._insert_open_position()
        await self.db.execute(
            "CREATE TRIGGER reject_events BEFORE INSERT ON trade_events "
            "BEGIN SELECT RAISE(ABORT, 'event failure'); END")

        async def client_factory(user):
            return object()

        async def place(*args, **kwargs):
            return OrderResult(ok=True, filled_shares=2, avg_price=.6)

        follow = await self.db.fetchone(
            "SELECT * FROM followed_traders WHERE user_id=? AND trader_address=?",
            (USER, TRADER))
        trade = SimpleNamespace(
            timestamp=1, asset=TOKEN, side="SELL", size=2.0, price=.6)
        engine = CopyEngine(
            self.db, SimpleNamespace(), place_order=place, client_factory=client_factory)
        with self.assertRaises(Exception):
            await engine._handle_leader_trade(follow, trade)
        persisted = await self.db.fetchone(
            "SELECT shares,status FROM copy_positions WHERE id='position'")
        self.assertEqual(10, persisted["shares"])
        self.assertEqual("closing", persisted["status"])
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM trade_events"))


if __name__ == "__main__":
    unittest.main()
