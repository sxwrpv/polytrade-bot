from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace

import httpx

from backend.core import copy_engine as ce
from backend.core.copy_engine import (
    CAPITAL_SHARE, POSITION_SHARE, Action, CopyEngine, cash_share, plan_actions,
)
from backend.core.execution import OrderResult
from backend.core.leader_cash import LeaderCashError, LeaderCashReader
from backend.core.withdraw import PUSD, USDC, USDC_E
from backend.db.database import Database, now_iso


USER = "0x" + "1" * 40
TRADER = "0x" + "2" * 40
TOKEN = "token-1"


def fake_pm(wallet_positions=()):
    """Engine pm stub. The pre-submission size gate reads the user's REAL
    wallet via get_all_positions; an empty wallet means full headroom."""
    async def get_all_positions(wallet, *, size_threshold=0.0, page_size=500,
                                max_pages=6):
        return list(wallet_positions), True

    return SimpleNamespace(get_all_positions=get_all_positions)


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

        engine = CopyEngine(self.db, fake_pm(), place_order=place,
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
        engine = CopyEngine(self.db, fake_pm(), position_notifier=notify)

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

        engine = CopyEngine(self.db, fake_pm(), place_order=place)
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

        first = CopyEngine(self.db, fake_pm(), place_order=place)
        second = CopyEngine(self.db, fake_pm(), place_order=place)

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

        engine = CopyEngine(self.db, fake_pm(), place_order=place)
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
            first = CopyEngine(self.db, fake_pm(), place_order=place)
            second = CopyEngine(second_db, fake_pm(), place_order=place)
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
        engine = CopyEngine(self.db, fake_pm(), place_order=None)
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

        engine = CopyEngine(self.db, fake_pm(), place_order=place)
        await engine._execute(USER, object(), open_action())
        claim = await self.db.fetchone(
            "SELECT state FROM copy_open_claims WHERE user_id=? AND token_id=?", (USER, TOKEN))
        self.assertEqual("uncertain", claim["state"])

    async def test_unclassified_buy_exception_retains_claim(self):
        async def place(*args, **kwargs):
            raise RuntimeError("preflight failed")

        engine = CopyEngine(self.db, fake_pm(), place_order=place)
        with self.assertRaisesRegex(RuntimeError, "preflight failed"):
            await engine._execute(USER, object(), open_action())

        self.assertEqual("uncertain", await self.db.fetchval(
            "SELECT state FROM copy_open_claims WHERE user_id=? AND token_id=?",
            (USER, TOKEN)))

    async def test_master_user_pause_blocks_buy(self):
        calls = 0

        async def place(*args, **kwargs):
            nonlocal calls
            calls += 1
            return OrderResult(ok=True, filled_shares=40, avg_price=0.5)

        engine = CopyEngine(self.db, fake_pm(), place_order=place)
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

        engine = CopyEngine(self.db, fake_pm(), place_order=place)
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

        engine = CopyEngine(self.db, fake_pm(), place_order=place)
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
                CopyEngine(self.db, fake_pm(), place_order=place)._execute(
                    USER, object(), resize),
                CopyEngine(second_db, fake_pm(), place_order=place)._execute(
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

    async def test_buy_sizing_leaves_out_pusd_that_withdrawals_still_hold(self):
        # A withdrawal holds its pUSD in the wallet until its transfer
        # confirms; a copy BUY must not be sized from that money. Confirmed
        # and failed withdrawals hold nothing (the balance already shows it).
        for n, (status, units) in enumerate((("submitted", 12_500_000),
                                             ("confirmed", 7_000_000),
                                             ("failed", 3_000_000))):
            await self.db.execute(
                "INSERT INTO withdrawals(id,user_id,idempotency_key,amount_units,destination,"
                "to_chain_id,to_token,status,requested_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"w{n}", USER, f"key-{n:05d}", units, "0x" + "3" * 40, 137, "usdc", status,
                 now_iso(), now_iso()))

        async def collateral(client):
            return 50.0

        engine = CopyEngine(self.db, fake_pm(), collateral_fn=collateral)
        user = await self.db.fetchone("SELECT * FROM users WHERE id=?", (USER,))
        available, _ = await engine._spendable_collateral(user, object())
        self.assertAlmostEqual(available, 37.5)
        raw, _ = await engine._read_collateral(user, object())
        self.assertEqual(raw, 50.0)

    async def test_a_buy_carries_the_cash_it_was_sized_from(self):
        # 2026-10-22: five copies sized to the whole of a $6.32 balance were
        # refused, because the exchange wants room for its fee estimate on
        # top. The order carries the spendable cash as max_spend, and the SDK
        # lowers the spend until both fit (execution.place_market_order).
        orders = []

        async def place(client, pm, token, side, amount, **kwargs):
            orders.append((side, amount, kwargs.get("max_spend")))
            return OrderResult(ok=True, filled_shares=amount / 0.5, avg_price=0.5)

        async def get_positions(wallet, **kwargs):
            return [position(value=2000.0)] if wallet == TRADER else []

        async def get_all_positions(wallet, **kwargs):
            return [], True

        async def collateral(client):
            return 6.316234

        async def client_factory(user):
            return object()

        engine = CopyEngine(
            self.db, SimpleNamespace(get_positions=get_positions,
                                     get_all_positions=get_all_positions),
            place_order=place, client_factory=client_factory, collateral_fn=collateral)
        follow = await self.db.fetchone("SELECT * FROM followed_traders WHERE id='follow-1'")
        await engine._handle_leader_trade(follow, leader_buy())
        self.assertEqual([("BUY", 6.316234, 6.316234)], orders)


# ---------------------------------------------------------------------------
# Capital share: the copy takes the share of your free cash that the leader's
# bet took of theirs (owner's call 2026-10-22, free cash as the base).
# ---------------------------------------------------------------------------
FOLLOW = {"max_position_usd": 50.0}


def capital_plan(positions, rows=(), available=150.0, **kwargs):
    options = dict(sizing_mode=CAPITAL_SHARE, multiplier=1.0, leader_cash=8000.0,
                   recent_buys={TOKEN}, ignore_below=2.0)
    options.update(kwargs)
    return plan_actions(list(positions), list(rows), FOLLOW, available, **options)


def leader_holding(token: str, value: float, price: float = 0.5):
    p = position(price=price, value=value)
    p.asset = token
    return p


def copy_row(*, shares=60.0, trader_shares=4000.0, notional=30.0, token=TOKEN):
    return {"token_id": token, "id": "pos-1", "shares": shares,
            "trader_shares": trader_shares, "notional_usd": notional, "entry_price": 0.5}


class CapitalShareSizingTests(unittest.TestCase):
    def test_the_share_is_read_against_the_cash_held_before_the_bet(self):
        self.assertAlmostEqual(0.20, cash_share(2000, 8000))      # 2,000 of 10,000
        self.assertAlmostEqual(1.00, cash_share(500, 0))          # all of it
        self.assertAlmostEqual(0.05, cash_share(2000, 38000))     # the owner's 2K of 40K
        for bet, cash in ((2000, None), (0, 1000), (-5, 1000), (10, -1),
                          (float("nan"), 10), (10, float("inf")), (None, 10)):
            with self.subTest(bet=bet, cash=cash):
                self.assertIsNone(cash_share(bet, cash))

    def test_an_open_takes_the_same_share_of_your_free_cash(self):
        [action] = capital_plan([position(value=2000)])
        self.assertEqual("open", action.kind)
        self.assertAlmostEqual(30.0, action.amount)               # 20% of $150
        self.assertEqual(CAPITAL_SHARE, action.sizing_mode)
        self.assertAlmostEqual(0.2, action.capital_weight)
        self.assertAlmostEqual(150.0, action.follower_cash)

    def test_the_multiplier_scales_and_the_usual_caps_still_apply(self):
        [doubled] = capital_plan([position(value=2000)], multiplier=2.0)
        self.assertAlmostEqual(50.0, doubled.amount, msg="max per position")
        [all_in] = capital_plan([position(value=2000)], multiplier=10.0, max_per_trade=500.0)
        self.assertAlmostEqual(150.0, all_in.amount, msg="capped at your free cash")
        other = copy_row(token="token-other", notional=140.0)
        opens = [a for a in capital_plan([position(value=2000), leader_holding("token-other", 70)],
                                         [other], max_total_exposure=150.0) if a.kind == "open"]
        self.assertAlmostEqual(10.0, opens[0].amount, msg="exposure cap to this leader")

    def test_a_copy_under_the_minimum_is_skipped(self):
        # $500 of $100,000 is 0.5%: $0.75 of $150, under the $2 minimum.
        self.assertEqual([], capital_plan([position(value=500)], leader_cash=99500.0))

    def test_only_a_recent_leader_buy_is_opened(self):
        self.assertEqual([], capital_plan([position(value=2000)], recent_buys=set()))
        self.assertEqual([], capital_plan([position(value=2000)], recent_buys=None))

    def test_no_leader_cash_reading_plans_no_buy_and_keeps_the_anchor(self):
        self.assertEqual([], capital_plan([position(value=2000)], leader_cash=None))
        grown = leader_holding(TOKEN, 3000)                        # 6,000 shares vs 4,000
        self.assertEqual([], capital_plan([grown], [copy_row()], leader_cash=None))

    def test_blocked_opens_stay_blocked(self):
        self.assertEqual([], capital_plan([position(value=2000)], block_opens=True))
        actions = capital_plan([leader_holding(TOKEN, 3000)], [copy_row()], block_opens=True)
        self.assertEqual(["reanchor"], [a.kind for a in actions])

    def test_a_second_open_is_sized_from_the_cash_the_first_left(self):
        first, second = capital_plan(
            [leader_holding("token-a", 2000), leader_holding("token-b", 2000)],
            recent_buys={"token-a", "token-b"})
        self.assertAlmostEqual(30.0, first.amount)                 # 20% of 150
        self.assertAlmostEqual(24.0, second.amount)                # 20% of the 120 left

    def test_an_increase_takes_the_share_the_leader_added(self):
        # 4,000 -> 6,000 shares at 0.50: $1,000 added with $9,000 left = 10%.
        [action] = capital_plan([leader_holding(TOKEN, 3000)], [copy_row()],
                                leader_cash=9000.0, available=100.0)
        self.assertEqual(("resize", "increase"), (action.kind, action.subkind))
        self.assertAlmostEqual(10.0, action.amount)                # 10% of $100
        self.assertEqual(CAPITAL_SHARE, action.sizing_mode)
        self.assertEqual(6000.0, action.trader_shares)

    def test_an_increase_past_max_per_position_reanchors_as_before(self):
        actions = capital_plan([leader_holding(TOKEN, 3000)], [copy_row(notional=50.0)],
                               leader_cash=9000.0)
        self.assertEqual(["reanchor"], [a.kind for a in actions])

    def test_exits_keep_the_proportional_share_rule(self):
        trimmed = [leader_holding(TOKEN, 1200)]                    # 4,000 -> 2,400 shares
        for mode in (CAPITAL_SHARE, POSITION_SHARE):
            with self.subTest(mode=mode):
                [sell] = capital_plan(trimmed, [copy_row()], sizing_mode=mode, leader_cash=None)
                self.assertEqual(("resize", "decrease"), (sell.kind, sell.subkind))
                self.assertAlmostEqual(24.0, sell.amount)          # 40% of 60 shares
                [close] = capital_plan([], [copy_row()], sizing_mode=mode, leader_cash=None)
                self.assertEqual(("close", 60.0), (close.kind, close.amount))

    def test_position_share_ignores_the_capital_inputs(self):
        [action] = capital_plan([position(value=2000)], sizing_mode=POSITION_SHARE,
                                recent_buys=set())
        self.assertAlmostEqual(20.0, action.amount)                # 1% of their $2,000
        self.assertEqual(POSITION_SHARE, action.sizing_mode)
        self.assertIsNone(action.capital_weight)

    def test_follow_risk_reads_the_mode_and_the_multiplier(self):
        default = CopyEngine._follow_risk({})
        self.assertEqual((POSITION_SHARE, 1.0), (default["sizing_mode"], default["multiplier"]))
        chosen = CopyEngine._follow_risk({"sizing_mode": "capital_share", "capital_multiplier": 4})
        self.assertEqual((CAPITAL_SHARE, 4.0), (chosen["sizing_mode"], chosen["multiplier"]))
        self.assertEqual(POSITION_SHARE, CopyEngine._follow_risk({"sizing_mode": "equity"})["sizing_mode"])
        for junk in (-2, float("nan")):
            with self.subTest(junk=junk):
                self.assertEqual(0.0, CopyEngine._follow_risk({"capital_multiplier": junk})["multiplier"])


class SpendLimitPlanTests(unittest.TestCase):
    """Each planned BUY carries the cash still free when it was sized, the
    max_spend its order goes out with (Action.spend_limit)."""

    def test_each_open_carries_the_cash_left_for_it(self):
        holdings = [leader_holding("token-a", 2000.0), leader_holding("token-b", 2000.0)]
        first, second = plan_actions(holdings, [], FOLLOW, 30.0, ratio_pct=1.0)
        self.assertEqual((20.0, 30.0), (first.amount, first.spend_limit))
        self.assertEqual((10.0, 10.0), (second.amount, second.spend_limit))

    def test_an_increase_carries_the_cash_left_for_it(self):
        [increase] = plan_actions([leader_holding(TOKEN, 4000.0)], [copy_row()], FOLLOW, 12.0,
                                  ratio_pct=1.0)
        self.assertEqual(("resize", "increase"), (increase.kind, increase.subkind))
        self.assertEqual((12.0, 12.0), (increase.amount, increase.spend_limit))

    def test_the_exposure_cap_leaves_the_cash_limit_alone(self):
        # A copy held to $15 by MAX EXPOSURE still has all $100 of cash behind
        # its fee: only the cash bounds max_spend.
        [open_] = plan_actions([leader_holding("token-a", 2000.0)], [], FOLLOW, 100.0,
                               ratio_pct=1.0, max_total_exposure=15.0)
        self.assertEqual((15.0, 100.0), (open_.amount, open_.spend_limit))

    def test_a_sell_carries_no_spend_limit(self):
        [close] = plan_actions([], [copy_row()], FOLLOW, 12.0, ratio_pct=1.0)
        self.assertEqual(("close", None), (close.kind, close.spend_limit))


class FakeLeaderCash:
    def __init__(self, usd: float = 8000.0, error: Exception | None = None):
        self.usd, self.error, self.calls = usd, error, []

    async def cash_usd(self, wallet, *, after=None):
        self.calls.append((wallet, after))
        if self.error is not None:
            raise self.error
        return self.usd


def leader_buy(*, size=4000.0, price=0.5, usd=2000.0, ts=None):
    return SimpleNamespace(
        timestamp=int(ts if ts is not None else time.time()), asset=TOKEN, side="BUY",
        size=size, price=price, usd_size=usd, condition_id="condition-1", outcome="YES",
        slug="market", title="Market", tx_hash="0xbuy")


class CapitalShareEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.db.execute(
            "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
            (USER, "encrypted", now_iso()))
        await self.db.execute(
            "INSERT INTO followed_traders(id, user_id, trader_address, copy_ratio_pct, "
            "max_position_usd, sizing_mode, capital_multiplier, paused, is_active, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("follow-1", USER, TRADER, 1.0, 50.0, CAPITAL_SHARE, 1.0, 0, 1, now_iso()))
        self.orders: list[tuple[str, float]] = []
        self.clients_built = 0

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def follow(self) -> dict:
        return await self.db.fetchone("SELECT * FROM followed_traders WHERE id='follow-1'")

    def engine(self, *, leader_value=2000.0, cash=None, collateral=150.0):
        async def get_positions(wallet, **kwargs):
            return [position(value=leader_value)] if wallet == TRADER else []

        async def get_all_positions(wallet, **kwargs):
            return ([position(value=leader_value)] if wallet == TRADER else []), True

        async def place(client, pm, token, side, amount, **kwargs):
            self.orders.append((side, float(amount)))
            return OrderResult(ok=True, filled_shares=amount / 0.5, avg_price=0.5)

        async def client_factory(user):
            self.clients_built += 1
            return object()

        async def collateral_fn(client):
            return collateral

        return CopyEngine(
            self.db, SimpleNamespace(get_positions=get_positions,
                                     get_all_positions=get_all_positions),
            place_order=place, client_factory=client_factory, collateral_fn=collateral_fn,
            leader_cash=cash)

    async def test_fast_path_copies_the_leaders_cash_share(self):
        cash = FakeLeaderCash(8000.0)
        engine = self.engine(cash=cash)
        trade = leader_buy()

        await engine._handle_leader_trade(await self.follow(), trade)

        self.assertEqual([("BUY", 30.0)], self.orders)            # 20% of $150
        self.assertEqual([(TRADER, float(trade.timestamp))], cash.calls,
                         "read once, and only from after the trade")
        self.assertEqual({TOKEN}, engine._recent_buys_of(TRADER))
        row = await self.db.fetchone("SELECT notional_usd FROM copy_positions WHERE token_id=?",
                                     (TOKEN,))
        self.assertAlmostEqual(30.0, row["notional_usd"])

    async def test_fast_path_skips_and_logs_when_leader_cash_is_unknown(self):
        engine = self.engine(cash=FakeLeaderCash(error=LeaderCashError("eth_call: HTTP 503")))

        with self.assertLogs("copy_engine", level="INFO") as logs:
            await engine._handle_leader_trade(await self.follow(), leader_buy())

        self.assertEqual([], self.orders)
        self.assertEqual(0, self.clients_built, "skipped before any account read")
        text = "\n".join(logs.output)
        self.assertIn("leader cash unavailable", text)
        self.assertIn("reason=leader_cash_unknown", text)

    async def test_without_a_reader_capital_share_copies_nothing(self):
        engine = self.engine(cash=None)
        with self.assertLogs("copy_engine", level="WARNING"):
            await engine._handle_leader_trade(await self.follow(), leader_buy())
        self.assertEqual([], self.orders)

    async def test_a_top_up_of_a_market_held_before_copying_counts_only_the_trade(self):
        engine = self.engine(leader_value=20000.0, cash=FakeLeaderCash(900.0))
        engine._no_backfill[(USER, TRADER)] = {TOKEN}

        await engine._handle_leader_trade(await self.follow(), leader_buy(size=200, usd=100.0))

        # $100 with $900 left is 10%, so $15 of $150. The whole $20,000
        # position would have read as 96% and gone to the $50 cap.
        self.assertEqual([("BUY", 15.0)], self.orders)

    async def test_a_reservation_rederives_the_share_with_the_current_multiplier(self):
        engine = self.engine()
        planned = replace(open_action(amount=50.0), trader_address=TRADER,
                          sizing_mode=CAPITAL_SHARE, capital_weight=0.2, follower_cash=150.0)
        await self.db.execute("UPDATE followed_traders SET capital_multiplier=1 WHERE id='follow-1'")

        reserved, _ = await engine._prepare_buy(USER, planned)

        self.assertAlmostEqual(30.0, reserved.amount)             # 20% of 150 at 1x

    async def test_a_buy_sized_under_the_other_mode_is_not_reserved(self):
        engine = self.engine()
        position_sized = replace(open_action(amount=10.0), trader_address=TRADER)
        self.assertIsNone(await engine._prepare_buy(USER, position_sized))
        await self.db.execute(
            "UPDATE followed_traders SET sizing_mode='position_share' WHERE id='follow-1'")
        capital_sized = replace(position_sized, sizing_mode=CAPITAL_SHARE,
                                capital_weight=0.2, follower_cash=150.0)
        self.assertIsNone(await engine._prepare_buy(USER, capital_sized))
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM copy_open_claims"))

    async def test_the_reconciler_opens_a_recent_leader_buy_with_its_cash_share(self):
        cash = FakeLeaderCash(8000.0)
        engine = self.engine(cash=cash)
        engine._no_backfill[(USER, TRADER)] = set()      # following since before the buy
        engine._note_leader_buy(TRADER, TOKEN, time.time())

        await engine._sync_user(USER, [await self.follow()])

        self.assertEqual([("BUY", 30.0)], self.orders)
        self.assertEqual(1, len(cash.calls))

    async def test_the_reconciler_leaves_an_unflagged_position_alone(self):
        cash = FakeLeaderCash(8000.0)
        engine = self.engine(cash=cash)
        engine._no_backfill[(USER, TRADER)] = set()

        await engine._sync_user(USER, [await self.follow()])

        self.assertEqual([], self.orders)
        self.assertEqual([], cash.calls, "a quiet pass costs the RPC nothing")

    async def test_a_leader_buy_is_remembered_only_while_it_can_be_copied(self):
        engine = self.engine()
        engine._note_leader_buy(TRADER, "stale", time.time() - ce.MAX_LEADER_TRADE_AGE_SECONDS - 1)
        engine._note_leader_buy(TRADER, TOKEN, time.time())
        self.assertEqual({TOKEN}, engine._recent_buys_of(TRADER))
        engine._recent_leader_buys[TRADER][TOKEN] = time.time() - 1
        self.assertEqual(frozenset(), engine._recent_buys_of(TRADER))


def rpc(balances=None, *, chain="0x89", code="0x6080", calls=None, error=None):
    """A Polygon JSON-RPC node over httpx.MockTransport."""
    balances = {k.lower(): v for k, v in (balances or {}).items()}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body["method"])
        if error is not None:
            raise error
        if body["method"] == "eth_chainId":
            result = chain
        elif body["method"] == "eth_getCode":
            result = code
        else:
            token = body["params"][0]["to"].lower()
            result = "0x" + format(balances.get(token, 0), "064x")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    return httpx.MockTransport(handler)


class LeaderCashReaderTests(unittest.IsolatedAsyncioTestCase):
    URL = "https://polygon.example/v2/SECRETKEY"

    def reader(self, transport, clock=None):
        return LeaderCashReader(self.URL, transport=transport,
                                clock=clock or (lambda: self.now))

    async def asyncSetUp(self):
        self.now = 1_000.0

    async def test_free_cash_is_pusd_plus_both_usdc_tokens(self):
        reader = self.reader(rpc({PUSD: 7_000_000_000, USDC_E: 500_000_000, USDC: 250_500_000}))
        self.assertAlmostEqual(7750.5, await reader.cash_usd(TRADER))
        await reader.aclose()

    async def test_a_node_on_another_chain_or_without_pusd_is_refused(self):
        for transport in (rpc(chain="0x1"), rpc(code="0x")):
            reader = self.reader(transport)
            with self.assertRaises(LeaderCashError):
                await reader.cash_usd(TRADER)
            await reader.aclose()

    async def test_one_reading_serves_a_leader_for_thirty_seconds(self):
        calls: list[str] = []
        reader = self.reader(rpc({PUSD: 1_000_000}, calls=calls))
        self.assertEqual(1.0, await reader.cash_usd(TRADER))
        reads = calls.count("eth_call")
        self.now += 29
        self.assertEqual(1.0, await reader.cash_usd(TRADER))
        self.assertEqual(reads, calls.count("eth_call"))
        self.now += 2
        await reader.cash_usd(TRADER)
        self.assertEqual(2 * reads, calls.count("eth_call"), "expired after 30s")
        await reader.aclose()

    async def test_a_reading_from_before_the_trade_is_not_reused_for_it(self):
        calls: list[str] = []
        reader = self.reader(rpc({PUSD: 1_000_000}, calls=calls))
        await reader.cash_usd(TRADER)                      # read at t=1000
        reads = calls.count("eth_call")
        await reader.cash_usd(TRADER, after=self.now - 10)
        self.assertEqual(reads, calls.count("eth_call"), "taken after that trade")
        await reader.cash_usd(TRADER, after=self.now - 1)  # the trade is newer than the read
        self.assertEqual(2 * reads, calls.count("eth_call"))
        await reader.aclose()

    async def test_followers_of_one_trade_share_one_read(self):
        calls: list[str] = []
        reader = self.reader(rpc({PUSD: 1_000_000}, calls=calls))
        results = await asyncio.gather(*(reader.cash_usd(TRADER) for _ in range(5)))
        self.assertEqual([1.0] * 5, results)
        self.assertEqual(3, calls.count("eth_call"), "pUSD, USDC.e and USDC, once")
        await reader.aclose()

    async def test_a_failed_read_is_remembered_briefly_and_never_shows_the_url(self):
        calls: list[str] = []
        reader = self.reader(rpc(calls=calls, error=httpx.ConnectError(f"cannot reach {self.URL}")))
        with self.assertRaises(LeaderCashError) as first:
            await reader.cash_usd(TRADER)
        self.assertNotIn("SECRETKEY", str(first.exception))
        attempts = len(calls)
        with self.assertRaises(LeaderCashError):
            await reader.cash_usd(TRADER)
        self.assertEqual(attempts, len(calls), "a dead RPC is not hit again at once")
        self.now += 6
        with self.assertRaises(LeaderCashError):
            await reader.cash_usd(TRADER)
        self.assertGreater(len(calls), attempts)
        await reader.aclose()

    async def test_a_malformed_wallet_is_refused_without_a_call(self):
        calls: list[str] = []
        reader = self.reader(rpc(calls=calls))
        with self.assertRaises(LeaderCashError):
            await reader.cash_usd("0x1234")
        self.assertEqual([], calls)
        await reader.aclose()


if __name__ == "__main__":
    unittest.main()
