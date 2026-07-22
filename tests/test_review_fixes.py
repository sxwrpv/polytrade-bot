"""Regression tests for the 2026-07-12 review fixes.

Covers: definitive-rejection classification (no more frozen uncertain claims),
leader-position pagination + truncation-safe close diffing, automated
uncertain-claim and stuck-closing reconciliation, per-wallet fast-exit
slippage, unfollow keeping open copies managed, event-based PnL consistency,
and the tunnel-aware rate-limit client IP.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from types import SimpleNamespace

from polymarket.errors import RequestRejectedError, TransportError

from backend.core import pnl
from backend.core.copy_engine import Action, CopyEngine, plan_actions
from backend.core.execution import OrderResult, place_market_order
from backend.core.polymarket import Level, OrderBook, PolymarketClient, Position, Trade
from backend.db.database import Database, now_iso

USER = "0x" + "1" * 40
TRADER = "0x" + "2" * 40
TOKEN = "token-1"


def iso_ago(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds)).isoformat()


def wallet_position(token=TOKEN, *, size=20.0, avg=0.5, cur=0.5,
                    redeemable=False) -> Position:
    return Position(
        proxy_wallet=USER, asset=token, condition_id=f"condition-{token}",
        size=size, avg_price=avg, cur_price=cur, initial_value=size * avg,
        current_value=size * cur, cash_pnl=(cur - avg) * size, percent_pnl=0.0,
        realized_pnl=0.0, redeemable=redeemable, mergeable=False,
        negative_risk=False, outcome="Yes", outcome_index=0, opposite_asset="",
        title="Market", slug="market", icon="", event_slug="event", end_date="")


class FakeBookPM:
    async def get_geoblock(self):
        return {"blocked": False}

    async def get_orderbook(self, token_id):
        return OrderBook(
            token_id=token_id, condition_id="condition",
            bids=(Level(0.49, 1000),), asks=(Level(0.5, 1000),),
            tick_size=0.01, min_order_size=1, neg_risk=False,
            last_trade_price=0.5)


class FailingClient:
    def __init__(self, error):
        self.error = error

    async def place_market_order(self, **kwargs):
        raise self.error


class RejectionClassificationTests(unittest.IsolatedAsyncioTestCase):
    """A definitive exchange rejection must NOT freeze a reconciliation claim."""

    async def test_clob_4xx_rejection_is_clean_failure(self):
        # the two rejections seen frozen live on 2026-07-11
        for message in ("not enough balance / allowance",
                        "order couldn't be fully filled. FOK orders are fully "
                        "filled or killed"):
            client = FailingClient(RequestRejectedError(message, status=400))
            res = await place_market_order(
                client, FakeBookPM(), TOKEN, "BUY", 10.0, reference_price=0.5)
            self.assertFalse(res.ok)
            self.assertFalse(res.submission_uncertain, message)

    async def test_5xx_and_transport_failures_stay_uncertain(self):
        for error in (RequestRejectedError("upstream exploded", status=502),
                      TransportError("connection reset mid-request")):
            client = FailingClient(error)
            res = await place_market_order(
                client, FakeBookPM(), TOKEN, "BUY", 10.0, reference_price=0.5)
            self.assertFalse(res.ok)
            self.assertTrue(res.submission_uncertain, str(error))


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    class PagedClient(PolymarketClient):
        def __init__(self, total):
            self.total = total
            self.calls = []

        async def get_positions(self, wallet, *, size_threshold=1.0, limit=500,
                                offset=0, sort_by="CURRENT"):
            self.calls.append((limit, offset))
            remaining = max(0, self.total - offset)
            return [wallet_position(f"tok-{offset + i}")
                    for i in range(min(limit, remaining))]

    async def test_get_all_positions_pages_past_the_per_call_cap(self):
        pm = self.PagedClient(total=1234)
        positions, complete = await pm.get_all_positions(USER)
        self.assertTrue(complete)
        self.assertEqual(1234, len(positions))
        self.assertEqual(1234, len({p.asset for p in positions}))

    async def test_truncated_fetch_is_flagged_incomplete(self):
        pm = self.PagedClient(total=10_000)
        positions, complete = await pm.get_all_positions(USER, max_pages=2)
        self.assertFalse(complete)
        self.assertEqual(1000, len(positions))

    def test_plan_actions_never_closes_on_a_truncated_position_list(self):
        row = {"token_id": TOKEN, "shares": 10.0, "notional_usd": 5.0,
               "trader_shares": 40.0, "entry_price": 0.5}
        follow = {"max_position_usd": 50.0}
        closes = [a for a in plan_actions(
            [], [row], follow, 100.0, positions_complete=False)
            if a.kind == "close"]
        self.assertEqual([], closes)
        # the complete list still closes as before
        closes = [a for a in plan_actions(
            [], [row], follow, 100.0, positions_complete=True)
            if a.kind == "close"]
        self.assertEqual(1, len(closes))


class EngineDbTestCase(unittest.IsolatedAsyncioTestCase):
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
            "max_position_usd, paused, is_active, created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("follow-1", USER, TRADER, 1.0, 50.0, 0, 1, now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def insert_claim(self, *, action="open", state="uncertain",
                           age_seconds=3600.0, token=TOKEN) -> str:
        ts = iso_ago(age_seconds)
        await self.db.execute(
            "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,"
            "action,state,reserved_usd,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (USER, token, TRADER, "claim-1", action, state, 8.0, ts, ts))
        return "claim-1"

    async def insert_position(self, *, status="open", shares=20.0,
                              closing_at=None, token=TOKEN) -> str:
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,"
            "opened_at,closing_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("position", USER, TRADER, f"condition-{token}", token, "Market", "YES",
             shares, 40.0, 0.5, shares * 0.5, status, now_iso(), closing_at))
        return "position"

    def engine(self, wallet_positions, **kwargs) -> CopyEngine:
        async def get_positions(wallet, *, size_threshold=1.0, limit=500,
                                offset=0, sort_by="CURRENT"):
            return list(wallet_positions)

        pm = SimpleNamespace(get_positions=get_positions, **kwargs)
        return CopyEngine(self.db, pm)


class UncertainClaimReconciliationTests(EngineDbTestCase):
    async def test_unfilled_uncertain_open_claim_is_released(self):
        await self.insert_claim()
        engine = self.engine(wallet_positions=[])

        await engine._reconcile_uncertain_claims()

        self.assertEqual(0, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_open_claims"))
        self.assertEqual(0, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_positions"))

    async def test_filled_uncertain_open_claim_is_adopted_from_wallet(self):
        await self.insert_claim()
        engine = self.engine(
            wallet_positions=[wallet_position(size=16.0, avg=0.5)])

        await engine._reconcile_uncertain_claims()

        self.assertEqual(0, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_open_claims"))
        row = await self.db.fetchone(
            "SELECT * FROM copy_positions WHERE user_id=? AND token_id=?",
            (USER, TOKEN))
        self.assertIsNotNone(row)
        self.assertEqual("open", row["status"])
        self.assertEqual(16.0, row["shares"])
        self.assertEqual(0.5, row["entry_price"])
        self.assertEqual(8.0, row["notional_usd"])
        self.assertEqual(TRADER, row["trader_address"])
        self.assertEqual(1, await self.db.fetchval(
            "SELECT COUNT(*) FROM trade_events WHERE event_type='open'"))

    async def test_fresh_uncertain_claim_is_left_for_the_indexer(self):
        await self.insert_claim(age_seconds=10.0)
        engine = self.engine(wallet_positions=[])

        await engine._reconcile_uncertain_claims()

        self.assertEqual(1, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_open_claims WHERE state='uncertain'"))


class StuckClosingRecoveryTests(EngineDbTestCase):
    async def test_shares_still_held_reopens_the_row(self):
        await self.insert_position(status="closing", closing_at=iso_ago(3600))
        engine = self.engine(wallet_positions=[wallet_position(size=20.0)])

        await engine._recover_stuck_closings()

        self.assertEqual("open", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id='position'"))

    async def test_shares_gone_finalizes_from_fill_history(self):
        await self.insert_position(status="closing", closing_at=iso_ago(3600))

        async def get_trade_history(wallet, limit=100, offset=0):
            return [Trade(
                proxy_wallet=USER, timestamp=int(dt.datetime.now(
                    dt.timezone.utc).timestamp()), condition_id=f"condition-{TOKEN}",
                side="SELL", asset=TOKEN, outcome="Yes", outcome_index=0,
                price=0.6, size=20.0, usd_size=12.0, title="Market",
                slug="market", tx_hash="0xabc")]

        async def get_resolved_prices(condition_id):
            return {}

        engine = self.engine(wallet_positions=[],
                             get_trade_history=get_trade_history,
                             get_resolved_prices=get_resolved_prices)

        await engine._recover_stuck_closings()

        row = await self.db.fetchone(
            "SELECT * FROM copy_positions WHERE id='position'")
        self.assertEqual("closed", row["status"])
        self.assertEqual(0.6, row["exit_price"])
        self.assertAlmostEqual((0.6 - 0.5) * 20.0, row["realized_pnl"])

    async def test_recent_closing_row_is_never_touched(self):
        # claim_managed_sell stamps closing_at; a fresh fence is in-flight work
        await self.insert_position(status="open")
        self.assertTrue(await self.db.claim_managed_sell(USER, TOKEN, "position"))
        engine = self.engine(wallet_positions=[wallet_position(size=20.0)])

        await engine._recover_stuck_closings()

        self.assertEqual("closing", await self.db.fetchval(
            "SELECT status FROM copy_positions WHERE id='position'"))


class FastExitSlippageTests(EngineDbTestCase):
    async def test_fast_exit_uses_the_wallets_slippage_setting(self):
        await self.db.execute(
            "UPDATE followed_traders SET max_slippage_pct=7.0 WHERE id='follow-1'")
        await self.insert_position(status="open")
        captured = {}

        async def place(client, pm, token, side, amount, **kwargs):
            captured.update(kwargs, side=side)
            return OrderResult(ok=True, side=side, filled_shares=amount,
                               avg_price=0.6)

        async def client_factory(user):
            return object()

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place,
                            client_factory=client_factory)
        follow = await self.db.fetchone("SELECT * FROM followed_traders")
        trade = SimpleNamespace(asset=TOKEN, side="SELL", size=40.0, price=0.6,
                                timestamp=int(dt.datetime.now(
                                    dt.timezone.utc).timestamp()))

        await engine._handle_leader_trade(follow, trade)

        self.assertEqual("SELL", captured["side"])
        self.assertEqual(7.0, captured["max_slippage_pct"])


class UnfollowKeepsManagingTests(EngineDbTestCase):
    async def test_reconcile_tick_still_manages_unfollowed_open_copies(self):
        await self.insert_position(status="open")
        await self.db.execute(
            "UPDATE followed_traders SET is_active=0 WHERE id='follow-1'")
        synced = []

        async def sync(user_id, follows):
            synced.append((user_id, [f["trader_address"] for f in follows]))

        engine = self.engine(wallet_positions=[])
        engine._sync_user = sync

        await engine._reconcile_tick()

        self.assertEqual([(USER, [TRADER])], synced)

    async def test_sync_user_blocks_opens_but_plans_closes_for_inactive_follow(self):
        await self.insert_position(status="open")
        await self.db.execute(
            "UPDATE followed_traders SET is_active=0 WHERE id='follow-1'")
        executed = []

        async def execute(user_id, client, action, slippage=None):
            executed.append(action)
            return 0.0

        async def get_all_positions(wallet, **kwargs):
            return [], True     # leader exited everything

        async def client_factory(user):
            return object()

        async def collateral(client):
            return 100.0

        pm = SimpleNamespace(get_all_positions=get_all_positions)
        engine = CopyEngine(self.db, pm, client_factory=client_factory,
                            collateral_fn=collateral)
        engine._execute = execute
        follow = await self.db.fetchone("SELECT * FROM followed_traders")

        await engine._sync_user(USER, [follow])

        self.assertEqual(["close"], [a.kind for a in executed])


class VerifiedSizeGateTests(EngineDbTestCase):
    """Pre-submission MAX/TRADE gate against the wallet's REAL holding: DB
    bookkeeping drift must never let a position grow past the cap."""

    def buy_engine(self, wallet_positions, placed, *, raise_on_read=False):
        async def get_all_positions(wallet, *, size_threshold=0.0,
                                    page_size=500, max_pages=6):
            if raise_on_read:
                raise RuntimeError("data-api down")
            return list(wallet_positions), True

        async def place(client, pm, token, side, amount, **kwargs):
            placed.append(amount)
            return OrderResult(ok=True, side=side, filled_shares=amount / 0.5,
                               avg_price=0.5)

        pm = SimpleNamespace(get_all_positions=get_all_positions)
        return CopyEngine(self.db, pm, place_order=place)

    def open_action(self, amount=8.0) -> Action:
        p = SimpleNamespace(
            proxy_wallet=TRADER, asset=TOKEN, condition_id="condition-1",
            size=2000.0, avg_price=0.5, cur_price=0.5, current_value=1000.0,
            redeemable=False, outcome="YES", slug="market", title="Market")
        return Action(kind="open", token_id=TOKEN, condition_id="condition-1",
                      outcome="YES", side="BUY", amount=amount,
                      notional_usd=amount, reference_price=0.5,
                      trader_shares=2000.0, position=p, trader_address=TRADER)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.db.execute(
            "UPDATE followed_traders SET max_position_usd=8.0 WHERE id='follow-1'")

    async def test_buy_clamped_to_real_wallet_headroom(self):
        # the wallet already holds $5 of this token that the DB knows nothing
        # about — only $3 of the $8 cap is really left
        placed: list[float] = []
        engine = self.buy_engine(
            [wallet_position(size=10.0, avg=0.5)], placed)

        spent = await engine._execute(USER, object(), self.open_action(8.0))

        self.assertEqual([3.0], placed)
        self.assertEqual(3.0, spent)
        self.assertEqual(3.0, await self.db.fetchval(
            "SELECT notional_usd FROM copy_positions WHERE token_id=?", (TOKEN,)))

    async def test_buy_skipped_when_wallet_already_at_cap(self):
        placed: list[float] = []
        engine = self.buy_engine(
            [wallet_position(size=16.0, avg=0.5)], placed)   # $8 already held

        spent = await engine._execute(USER, object(), self.open_action(8.0))

        self.assertEqual([], placed)
        self.assertEqual(0.0, spent)
        self.assertEqual(0, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_positions"))
        self.assertEqual(0, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_open_claims"))   # claim released

    async def test_buy_fails_closed_when_wallet_cannot_be_read(self):
        placed: list[float] = []
        engine = self.buy_engine([], placed, raise_on_read=True)

        spent = await engine._execute(USER, object(), self.open_action(8.0))

        self.assertEqual([], placed)
        self.assertEqual(0.0, spent)
        self.assertEqual(0, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_open_claims"))   # claim released

    async def test_resize_headroom_uses_wallet_truth_over_row(self):
        # DB row says $4 spent, but the wallet really holds $7 — a $4 top-up
        # must clamp to $1, and $1 >= MIN_NOTIONAL still executes
        await self.insert_position(status="open", shares=8.0)
        await self.db.execute(
            "UPDATE copy_positions SET notional_usd=4.0 WHERE id='position'")
        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='position'")
        placed: list[float] = []
        engine = self.buy_engine(
            [wallet_position(size=14.0, avg=0.5)], placed)   # $7 real cost
        p = SimpleNamespace(proxy_wallet=TRADER, asset=TOKEN,
                            condition_id=f"condition-{TOKEN}", size=4000.0,
                            avg_price=0.5, cur_price=0.5, current_value=2000.0,
                            redeemable=False, outcome="YES", slug="market",
                            title="Market")
        action = Action(kind="resize", subkind="increase", token_id=TOKEN,
                        side="BUY", amount=4.0, notional_usd=4.0,
                        reference_price=0.5, trader_shares=4000.0, row=row,
                        position=p, trader_address=TRADER)

        spent = await engine._execute(USER, object(), action)

        self.assertEqual([1.0], placed)
        self.assertEqual(1.0, spent)


class PnlConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.db.execute(
            "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
            (USER, "encrypted", now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def add_position(self, pid, trader, status, realized, events):
        closed_at = now_iso() if status in ("closed", "resolved") else None
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,"
            "token_id,market_title,outcome,shares,entry_price,notional_usd,status,"
            "realized_pnl,opened_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, USER, trader, "condition", f"token-{pid}", "Market", "YES",
             10.0, 0.5, 5.0, status, realized, now_iso(), closed_at))
        for i, (event_type, event_pnl) in enumerate(events):
            await self.db.execute(
                "INSERT INTO trade_events(id,user_id,position_id,event_type,pnl,ts) "
                "VALUES(?,?,?,?,?,?)",
                (f"{pid}-e{i}", USER, pid, event_type, event_pnl, now_iso()))

    async def test_headline_breakdown_and_windows_agree_with_partials(self):
        # closed with a partial: events 2.0 + 3.0 (row column only holds 3.0)
        await self.add_position("p1", TRADER, "closed", 3.0,
                                [("partial", 2.0), ("close", 3.0)])
        # still open but with banked partial-exit pnl
        await self.add_position("p2", TRADER, "open", None, [("partial", 1.5)])
        # legacy closed row with no events at all
        await self.add_position("p3", "0xother", "closed", 4.0, [])

        stats = await pnl.get_pnl_stats(USER, self.db)
        by_wallet = await pnl.get_pnl_by_wallet(USER, self.db)

        self.assertEqual(10.5, stats["realized_pnl"])       # 2+3+1.5+4
        self.assertEqual(10.5, stats["pnl_30d"])            # windows agree now
        self.assertEqual(10.5, round(sum(
            w["realized_pnl"] for w in by_wallet), 2))      # breakdown agrees
        per_trader = {w["trader_address"]: w for w in by_wallet}
        self.assertEqual(6.5, per_trader[TRADER]["realized_pnl"])
        self.assertEqual(1, per_trader[TRADER]["closed_trades"])  # p2 not "closed"
        self.assertEqual(4.0, per_trader["0xother"]["realized_pnl"])
        # completed-position stats stay closed-only
        self.assertEqual(2, stats["total_trades"])
        self.assertEqual(5.0, stats["best_trade"])


class ClientIpTests(unittest.TestCase):
    def test_forwarded_for_is_honored_only_from_loopback(self):
        from backend.api.routes_user import _client_ip

        tunnel = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"),
            headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
        self.assertEqual("203.0.113.7", _client_ip(tunnel))

        direct = SimpleNamespace(
            client=SimpleNamespace(host="198.51.100.9"),
            headers={"x-forwarded-for": "203.0.113.7"})
        self.assertEqual("198.51.100.9", _client_ip(direct))


if __name__ == "__main__":
    unittest.main()
