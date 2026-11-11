"""Review 2026-11-10: the engine's reconcile half and the close-external route.

LOG1  A standing uncertain claim logged its "retained" line on every pass
      (72,275 of 79,509 lines in 14 hours). Each (claim, reason) now logs on
      first sight and then once per UNCERTAIN_CLAIM_LOG_INTERVAL_SECONDS.
LOG3  The CRITICAL "quarantined" line goes through the same limiter.
LOG2  A claim at least UNCERTAIN_CLAIM_SLOW_AGE_SECONDS old that was weighed
      and retained is weighed again UNCERTAIN_CLAIM_SLOW_RECHECK_SECONDS
      later, and a user's positions read is skipped while every due claim of
      that user waits.
F6    The external close inserted its 'closing' row without closing_at, so
      stuck-closing recovery took the fresh row for a stuck one while the SELL
      was still in flight.
F15   Stuck-closing recovery spells its share-dust tolerance SHARE_DUST.
D13a  /positions/closed selected two columns named realized_pnl; SQLite put
      the stored one in the row and asyncpg the ledger total.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.api.routes_positions import (
    CloseExternalBody,
    close_external_position,
    closed_positions,
)
from backend.core import copy_engine as ce
from backend.core.copy_engine import CopyEngine
from backend.core.ctf import ChainBalance, ChainHead
from backend.core.execution import OrderResult
from backend.core.polymarket import Position, Trade
from backend.db.database import Database, now_iso

USER = "0x" + "1" * 40
OTHER = "0x" + "3" * 40
TRADER = "0x" + "2" * 40
TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
TOKEN_B = "52114319501245915516055106046884209969926127482827954674443846427813813222426"
COND = "condition-1"
INTERVAL = ce.UNCERTAIN_CLAIM_LOG_INTERVAL_SECONDS
RECHECK = ce.UNCERTAIN_CLAIM_SLOW_RECHECK_SECONDS
YOUNG = 300.0                                    # due, and weighed on every pass
OLD = ce.UNCERTAIN_CLAIM_SLOW_AGE_SECONDS + 600  # due, and slow once retained


def iso_ago(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat()


def holding(size: float, avg: float = 0.5, *, token: str = TOKEN, user: str = USER) -> Position:
    return Position(
        proxy_wallet=user, asset=token, condition_id=COND, size=size,
        avg_price=avg, cur_price=avg, initial_value=size * avg,
        current_value=size * avg, cash_pnl=0.0, percent_pnl=0.0,
        realized_pnl=0.0, redeemable=False, mergeable=False,
        negative_risk=False, outcome="Yes", outcome_index=0,
        opposite_asset="", title="Market", slug="market", icon="",
        event_slug="event", end_date="")


class Wallet:
    """Stands in for the data-api: holdings a test can change between passes,
    and a record of every full positions read."""

    def __init__(self, positions=(), *, complete: bool = True):
        self.positions = list(positions)
        self.complete = complete
        self.fail = False
        self.reads: list[str] = []
        self.trades: list[Trade] = []

    async def get_all_positions(self, wallet_address, **kwargs):
        self.reads.append(wallet_address)
        if self.fail:
            raise RuntimeError("data-api down")
        return [p for p in self.positions if p.proxy_wallet == wallet_address], self.complete

    async def get_positions(self, wallet_address, **kwargs):
        return [p for p in self.positions if p.proxy_wallet == wallet_address]

    async def get_trade_history(self, wallet_address, limit=100, offset=0):
        return list(self.trades)

    async def get_resolved_prices(self, condition_id):
        return {}


class Chain:
    """Stands in for CtfReader: one head block and one balance."""

    def __init__(self, shares: float = 0.0):
        self.shares = shares
        self.reads = 0

    async def head(self) -> ChainHead:
        return ChainHead(number=1000, timestamp=int(time.time()))

    async def balance(self, owner, token_id, head) -> ChainBalance:
        self.reads += 1
        return ChainBalance(shares=self.shares, head=head)


def elapse(engine: CopyEngine, seconds: float) -> None:
    """Let `seconds` pass for the engine's in-memory limiter and recheck state."""
    for state in ("_claim_logged_at", "_claim_next_check"):
        stamps = engine.__dict__.get(state, {})
        for key in stamps:
            stamps[key] -= seconds


def lines(logs, word: str) -> list[logging.LogRecord]:
    return [r for r in logs.records if word in r.getMessage()]


class EngineDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        for user in (USER, OTHER):
            await self.db.execute(
                "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
                (user, "encrypted", now_iso()))
        await self.db.execute(
            "INSERT INTO followed_traders(id,user_id,trader_address,copy_ratio_pct,"
            "max_position_usd,paused,is_active,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("follow-1", USER, TRADER, 1.0, 50.0, 0, 1, now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def claim(self, *, action="open", age=YOUNG, reserved=8.0, token=TOKEN,
                    user=USER, claim_id="claim-1") -> None:
        await self.db.execute(
            "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,"
            "state,reserved_usd,claimed_at,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user, token, TRADER, claim_id, action, "uncertain", reserved,
             iso_ago(age + 5), iso_ago(age), "order_rejected: FOK order killed"))

    async def position(self, *, status="open", shares=20.0, opened_age=7200.0,
                       closing_age=None, pid="pos-1", trader=TRADER) -> None:
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,"
            "opened_at,closing_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, USER, trader, COND, TOKEN, "Market", "YES", shares, 100.0, 0.5,
             shares * 0.5, status, iso_ago(opened_age),
             iso_ago(closing_age) if closing_age is not None else None))

    async def row(self, pid: str = "pos-1") -> dict | None:
        return await self.db.fetchone("SELECT * FROM copy_positions WHERE id=?", (pid,))

    async def claims(self) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM copy_open_claims"))


# ---------------------------------------------------------------------------
# LOG1: the "retained" line
# ---------------------------------------------------------------------------
class RetainedLogLimiterTests(EngineDb):
    async def test_a_standing_claim_logs_its_retained_line_once(self):
        await self.claim()
        wallet = Wallet()
        engine = CopyEngine(self.db, wallet)
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            for _ in range(3):
                await engine._reconcile_uncertain_claims()
        retained = lines(logs, "retained")
        self.assertEqual(1, len(retained))
        self.assertEqual(logging.WARNING, retained[0].levelno)
        self.assertEqual(
            f"uncertain OPEN claim retained (no authoritative fill status): {USER[:10]} {TOKEN}",
            retained[0].getMessage())
        self.assertEqual(3, len(wallet.reads))        # still weighed on every pass
        self.assertEqual(1, await self.claims())

    async def test_the_line_returns_once_the_interval_has_passed(self):
        await self.claim()
        engine = CopyEngine(self.db, Wallet())
        with self.assertLogs("copy_engine", level="WARNING"):
            await engine._reconcile_uncertain_claims()
        elapse(engine, INTERVAL - 5)
        with self.assertNoLogs("copy_engine", level="WARNING"):
            await engine._reconcile_uncertain_claims()
        elapse(engine, 10)
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await engine._reconcile_uncertain_claims()
            await engine._reconcile_uncertain_claims()
        self.assertEqual(1, len(lines(logs, "retained")))

    async def test_each_reason_is_limited_on_its_own(self):
        await self.claim()
        wallet = Wallet(complete=False)
        engine = CopyEngine(self.db, wallet)
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await engine._reconcile_uncertain_claims()
            wallet.complete = True
            await engine._reconcile_uncertain_claims()
            wallet.complete = False
            await engine._reconcile_uncertain_claims()
            wallet.complete = True
            await engine._reconcile_uncertain_claims()
        self.assertEqual(
            [f"uncertain claim retained: wallet scan incomplete for {USER[:10]} {TOKEN}",
             f"uncertain OPEN claim retained (no authoritative fill status): {USER[:10]} {TOKEN}"],
            [r.getMessage() for r in logs.records])

    async def test_each_claim_is_limited_on_its_own(self):
        await self.claim()
        await self.claim(token=TOKEN_B, claim_id="claim-2")
        engine = CopyEngine(self.db, Wallet())
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await engine._reconcile_uncertain_claims()
            await engine._reconcile_uncertain_claims()
        self.assertEqual({TOKEN, TOKEN_B},
                         {r.getMessage().split()[-1] for r in lines(logs, "retained")})
        self.assertEqual(2, len(logs.records))

    async def test_a_resize_claim_is_limited_too(self):
        await self.claim(action="resize")
        await self.position(shares=20.0)
        engine = CopyEngine(self.db, Wallet([holding(20.0)]))
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            for _ in range(3):
                await engine._reconcile_uncertain_claims()
        self.assertEqual(
            [f"uncertain RESIZE claim retained (no authoritative non-fill): {USER[:10]} {TOKEN}"],
            [r.getMessage() for r in logs.records])

    async def test_on_chain_reasons_share_one_line_per_interval(self):
        await self.claim(age=OLD)
        chain = Chain(shares=16.0)                      # the indexer shows none of it
        engine = CopyEngine(self.db, Wallet(), ctf=chain)
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await engine._reconcile_uncertain_claims()
            elapse(engine, RECHECK + 1)
            chain.shares = 17.0                         # the reason text moves with it
            await engine._reconcile_uncertain_claims()
        self.assertEqual(2, chain.reads)
        self.assertEqual(1, len(lines(logs, "retained")))
        self.assertIn("chain holds 16.0000 shares, indexer 0.0000", logs.output[0])

    async def test_the_state_goes_when_the_claim_does(self):
        await self.claim(age=OLD)
        engine = CopyEngine(self.db, Wallet())
        with self.assertLogs("copy_engine", level="WARNING"):
            await engine._reconcile_uncertain_claims()
        self.assertEqual([("claim-1", "no-fill-status")], list(engine._claim_logged_at))
        self.assertEqual(["claim-1"], list(engine._claim_next_check))
        await self.db.execute("DELETE FROM copy_open_claims")       # the operator cleared it
        await engine._reconcile_uncertain_claims()
        self.assertEqual({}, engine._claim_logged_at)
        self.assertEqual({}, engine._claim_next_check)
        # A later claim for the same token is a new claim and logs at once.
        await self.claim(age=OLD, claim_id="claim-2")
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await engine._reconcile_uncertain_claims()
        self.assertEqual(1, len(lines(logs, "retained")))

    async def test_an_adopted_claim_leaves_no_state(self):
        await self.claim()
        wallet = Wallet()
        engine = CopyEngine(self.db, wallet)
        with self.assertLogs("copy_engine", level="WARNING"):
            await engine._reconcile_uncertain_claims()
        wallet.positions = [holding(16.0)]
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await engine._reconcile_uncertain_claims()
            await engine._reconcile_uncertain_claims()
        self.assertEqual(1, len(lines(logs, "adopted")))
        self.assertEqual(0, await self.claims())
        self.assertEqual({}, engine._claim_logged_at)
        self.assertEqual({}, engine._claim_next_check)


class EngineWithoutInitTests(unittest.IsolatedAsyncioTestCase):
    """Many tests build the engine with CopyEngine.__new__: the limiter and
    recheck state has to appear on first use."""

    def claim(self, age: float) -> dict:
        return {"claim_id": "c1", "user_id": "0xu", "token_id": "tok", "action": "open",
                "reserved_usd": 15.0, "updated_at": iso_ago(age)}

    def engine(self, claim: dict):
        engine = CopyEngine.__new__(CopyEngine)
        reads = []

        async def fetchall(sql, params=()):
            return [claim]

        async def fetchone(sql, params=()):
            return None

        async def get_all_positions(user_id, size_threshold=1.0):
            reads.append(user_id)
            return [], True

        engine.db = SimpleNamespace(fetchall=fetchall, fetchone=fetchone)
        engine.pm = SimpleNamespace(get_all_positions=get_all_positions)
        return engine, reads

    async def test_settling_directly_limits_the_line(self):
        engine, _ = self.engine(self.claim(YOUNG))
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await engine._settle_uncertain_claim("0xu", self.claim(YOUNG), None)
            await engine._settle_uncertain_claim("0xu", self.claim(YOUNG), None)
        self.assertEqual(1, len(logs.records))

    async def test_a_reconcile_pass_limits_the_line_and_the_read(self):
        engine, reads = self.engine(self.claim(OLD))
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            for _ in range(3):
                await engine._reconcile_uncertain_claims()
        self.assertEqual(1, len(logs.records))
        self.assertEqual(["0xu"], reads)


# ---------------------------------------------------------------------------
# LOG3: the "quarantined" line
# ---------------------------------------------------------------------------
class QuarantineLogLimiterTests(EngineDb):
    async def test_a_quarantined_buy_logs_critical_once_per_interval(self):
        await self.claim()                                          # reserved $8
        engine = CopyEngine(self.db, Wallet([holding(40.0)]))       # $20 in the wallet
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            for _ in range(3):
                await engine._reconcile_uncertain_claims()
        self.assertEqual(1, len(logs.records))
        self.assertEqual(logging.CRITICAL, logs.records[0].levelno)
        self.assertEqual(
            f"uncertain BUY quarantined: observed $20.00 exceeds $8.00 claim for {USER[:10]} {TOKEN}",
            logs.records[0].getMessage())
        # The branch itself is as it was: the fence stays and nothing is booked.
        self.assertEqual(1, await self.claims())
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM copy_positions"))
        elapse(engine, INTERVAL + 1)
        with self.assertLogs("copy_engine", level="CRITICAL") as logs:
            await engine._reconcile_uncertain_claims()
        self.assertEqual(1, len(logs.records))

    async def test_a_quarantined_resize_logs_critical_once_per_interval(self):
        await self.claim(action="resize")
        await self.position(shares=20.0)
        engine = CopyEngine(self.db, Wallet([holding(60.0)]))       # 40 extra shares, $20
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            for _ in range(3):
                await engine._reconcile_uncertain_claims()
        self.assertEqual(1, len(logs.records))
        self.assertEqual(logging.CRITICAL, logs.records[0].levelno)
        self.assertEqual(
            f"uncertain RESIZE quarantined: observed $20.00 exceeds $8.00 claim for {USER[:10]} {TOKEN}",
            logs.records[0].getMessage())
        self.assertEqual(1, await self.claims())
        self.assertEqual(20.0, (await self.row())["shares"])


# ---------------------------------------------------------------------------
# LOG2: the slow recheck of old claims
# ---------------------------------------------------------------------------
class SlowRecheckTests(EngineDb):
    async def passes(self, engine: CopyEngine, count: int = 1) -> None:
        for _ in range(count):
            await engine._reconcile_uncertain_claims()

    async def test_an_old_retained_claim_is_weighed_once_per_recheck_interval(self):
        await self.claim(age=OLD)
        wallet = Wallet()
        engine = CopyEngine(self.db, wallet)
        await self.passes(engine, 3)
        self.assertEqual([USER], wallet.reads)
        elapse(engine, RECHECK - 5)
        await self.passes(engine)
        self.assertEqual([USER], wallet.reads)
        elapse(engine, 10)
        await self.passes(engine, 3)
        self.assertEqual([USER, USER], wallet.reads)

    async def test_a_claim_short_of_the_slow_age_is_weighed_on_every_pass(self):
        for age in (YOUNG, ce.UNCERTAIN_CLAIM_SLOW_AGE_SECONDS - 60):
            with self.subTest(age=age):
                await self.db.execute("DELETE FROM copy_open_claims")
                await self.claim(age=age)
                wallet = Wallet()
                engine = CopyEngine(self.db, wallet)
                await self.passes(engine, 3)
                self.assertEqual([USER] * 3, wallet.reads)
                self.assertEqual({}, engine._claim_next_check)

    async def test_a_waiting_claim_settles_the_same_way_at_its_next_weighing(self):
        await self.claim(age=OLD)
        wallet = Wallet()
        engine = CopyEngine(self.db, wallet)
        await self.passes(engine)
        wallet.positions = [holding(16.0)]             # the fill surfaces during the wait
        await self.passes(engine, 3)
        self.assertEqual(1, await self.claims())
        self.assertEqual([USER], wallet.reads)
        elapse(engine, RECHECK + 1)
        await self.passes(engine)
        self.assertEqual(0, await self.claims())
        row = await self.db.fetchone("SELECT * FROM copy_positions")
        self.assertEqual(("open", 16.0, 0.5, 8.0, TRADER),
                         (row["status"], row["shares"], row["entry_price"],
                          row["notional_usd"], row["trader_address"]))

    async def test_a_restart_weighs_every_claim_on_its_first_pass(self):
        await self.claim(age=OLD)
        wallet = Wallet()
        await self.passes(CopyEngine(self.db, wallet), 2)
        self.assertEqual([USER], wallet.reads)
        wallet.positions = [holding(16.0)]
        await self.passes(CopyEngine(self.db, wallet))            # a new process
        self.assertEqual([USER, USER], wallet.reads)
        self.assertEqual(0, await self.claims())

    async def test_one_ready_claim_has_all_of_the_users_claims_weighed(self):
        await self.claim(age=OLD)
        await self.claim(age=YOUNG, token=TOKEN_B, claim_id="claim-2")
        wallet = Wallet()
        engine = CopyEngine(self.db, wallet)
        await self.passes(engine)
        wallet.positions = [holding(16.0)]             # for the old, waiting claim
        await self.passes(engine)
        self.assertEqual([USER, USER], wallet.reads)
        self.assertEqual(
            ["claim-2"],
            [c["claim_id"] for c in await self.db.fetchall("SELECT * FROM copy_open_claims")])
        self.assertEqual(TOKEN, await self.db.fetchval("SELECT token_id FROM copy_positions"))

    async def test_only_the_waiting_users_read_is_skipped(self):
        await self.claim(age=OLD)
        await self.claim(age=YOUNG, user=OTHER, claim_id="claim-2")
        wallet = Wallet()
        engine = CopyEngine(self.db, wallet)
        await self.passes(engine, 3)
        self.assertEqual(1, wallet.reads.count(USER))
        self.assertEqual(3, wallet.reads.count(OTHER))

    async def test_a_failed_read_sets_no_wait(self):
        await self.claim(age=OLD)
        wallet = Wallet()
        wallet.fail = True
        engine = CopyEngine(self.db, wallet)
        with self.assertLogs("copy_engine", level="ERROR"):
            await self.passes(engine, 2)
        self.assertEqual([USER, USER], wallet.reads)
        self.assertEqual({}, engine._claim_next_check)

    async def test_a_failed_settlement_sets_no_wait(self):
        await self.claim(age=OLD)
        wallet = Wallet()
        engine = CopyEngine(self.db, wallet)
        await self.passes(engine)                                   # retained, waiting
        elapse(engine, RECHECK + 1)
        engine._settle_uncertain_claim = AsyncMock(side_effect=RuntimeError("db hiccup"))
        with self.assertLogs("copy_engine", level="ERROR"):
            await self.passes(engine, 2)
        self.assertEqual([USER] * 3, wallet.reads)
        self.assertEqual({}, engine._claim_next_check)

    async def test_a_truncated_scan_waits_like_any_retained_outcome(self):
        await self.claim(age=OLD)
        wallet = Wallet(complete=False)
        engine = CopyEngine(self.db, wallet)
        await self.passes(engine, 3)
        self.assertEqual([USER], wallet.reads)
        self.assertEqual(1, await self.claims())

    async def test_a_quarantined_old_claim_waits_too(self):
        await self.claim(age=OLD)
        wallet = Wallet([holding(40.0)])
        engine = CopyEngine(self.db, wallet)
        with self.assertLogs("copy_engine", level="CRITICAL") as logs:
            await self.passes(engine, 3)
        self.assertEqual(1, len(logs.records))
        self.assertEqual([USER], wallet.reads)
        self.assertEqual(1, await self.claims())

    async def test_a_resize_claim_waits_and_then_adopts_the_top_up(self):
        await self.claim(action="resize", age=OLD)
        await self.position(shares=20.0)
        wallet = Wallet([holding(20.0)])
        engine = CopyEngine(self.db, wallet)
        await self.passes(engine)
        wallet.positions = [holding(30.0)]             # 10 more shares, $5 of the $8
        await self.passes(engine, 2)
        self.assertEqual(20.0, (await self.row())["shares"])
        elapse(engine, RECHECK + 1)
        await self.passes(engine)
        self.assertEqual(0, await self.claims())
        self.assertEqual(30.0, (await self.row())["shares"])

    async def test_the_chain_is_read_at_the_same_weighings_as_the_wallet(self):
        await self.claim(age=OLD)
        wallet = Wallet([holding(16.0)])               # the indexer objects to a release
        chain = Chain(shares=0.0)
        engine = CopyEngine(self.db, wallet, ctf=chain)
        await self.passes(engine, 3)
        self.assertEqual((1, [USER]), (chain.reads, wallet.reads))
        self.assertEqual(1, await self.claims())
        wallet.positions = []                           # the indexer caught up
        elapse(engine, RECHECK + 1)
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await self.passes(engine)
        self.assertEqual(1, len(lines(logs, "RELEASED on on-chain evidence")))
        self.assertEqual(0, await self.claims())


# ---------------------------------------------------------------------------
# F6: the external close and stuck-closing recovery
# ---------------------------------------------------------------------------
class ManualCloseFenceTests(EngineDb):
    def close(self, wallet: Wallet):
        return close_external_position(
            CloseExternalBody(token_id=TOKEN), SimpleNamespace(),
            user={"id": USER}, db=self.db, pmc=wallet)

    def order(self, result=None, side_effect=None):
        return patch("backend.api.routes_positions.execution.place_market_order",
                     AsyncMock(return_value=result, side_effect=side_effect))

    def client(self):
        return patch("backend.api.routes_positions.get_user_client",
                     AsyncMock(return_value=object()))

    async def manual_row(self) -> dict:
        return await self.db.fetchone(
            "SELECT * FROM copy_positions WHERE trader_address='manual'")

    async def test_the_external_close_stamps_closing_at_with_opened_at(self):
        uncertain = OrderResult(ok=False, reason="timeout", submission_uncertain=True)
        before = now_iso()
        with self.client(), self.order(uncertain):
            await self.close(Wallet([holding(12.0, 0.35)]))
        row = await self.manual_row()
        self.assertEqual("closing", row["status"])
        self.assertEqual(row["opened_at"], row["closing_at"])
        # Written by now_iso like every other closing_at, so the recovery
        # cutoff compares against one format.
        self.assertLessEqual(before, row["closing_at"])
        self.assertLessEqual(row["closing_at"], now_iso())

    async def test_a_reconcile_pass_during_the_sell_leaves_the_row_alone(self):
        wallet = Wallet([holding(12.0, 0.35)])         # the shares are still there mid-SELL
        engine = CopyEngine(self.db, wallet)
        seen = []

        async def place(client, pm, token, side, amount, **kwargs):
            await engine._recover_stuck_closings()
            seen.append((await self.manual_row())["status"])
            return OrderResult(ok=True, side=side, token_id=token,
                               filled_shares=amount, avg_price=0.5)

        with self.client(), self.order(side_effect=place):
            response = await self.close(wallet)
        self.assertEqual(["closing"], seen)
        self.assertEqual([], wallet.reads)              # no stuck row, so no wallet scan
        self.assertTrue(response["ok"])
        row = await self.manual_row()
        self.assertEqual(("closed", 0.5), (row["status"], row["exit_price"]))
        self.assertAlmostEqual((0.5 - 0.35) * 12.0, row["realized_pnl"])

    async def test_a_fresh_row_without_closing_at_is_left_alone(self):
        await self.position(status="closing", opened_age=5.0, trader="manual")
        wallet = Wallet([holding(20.0)])
        await CopyEngine(self.db, wallet)._recover_stuck_closings()
        self.assertEqual("closing", (await self.row())["status"])
        self.assertEqual([], wallet.reads)

    async def test_an_old_row_without_closing_at_is_still_recovered(self):
        await self.position(status="closing", opened_age=3600.0, trader="manual")
        with self.assertLogs("copy_engine", level="WARNING"):
            await CopyEngine(self.db, Wallet([holding(20.0)]))._recover_stuck_closings()
        self.assertEqual("open", (await self.row())["status"])

    async def test_a_fresh_closing_at_outranks_an_old_opened_at(self):
        await self.position(status="closing", opened_age=3600.0, closing_age=5.0)
        await CopyEngine(self.db, Wallet([holding(20.0)]))._recover_stuck_closings()
        self.assertEqual("closing", (await self.row())["status"])

    async def test_an_old_closing_at_is_recovered_as_before(self):
        await self.position(status="closing", opened_age=7200.0, closing_age=3600.0)
        with self.assertLogs("copy_engine", level="WARNING"):
            await CopyEngine(self.db, Wallet([holding(20.0)]))._recover_stuck_closings()
        self.assertEqual("open", (await self.row())["status"])

    async def test_a_stuck_manual_close_is_finalized_from_its_own_sell(self):
        uncertain = OrderResult(ok=False, reason="timeout", submission_uncertain=True)
        with self.client(), self.order(uncertain):
            await self.close(Wallet([holding(12.0, 0.35)]))
        # An hour on, the SELL turns out to have filled. Only the stamps the
        # route wrote are aged.
        stamp = iso_ago(3600)
        await self.db.execute(
            "UPDATE copy_positions SET opened_at=?, "
            "closing_at=CASE WHEN closing_at IS NOT NULL THEN ? END "
            "WHERE trader_address='manual'", (stamp, stamp))
        fenced = dt.datetime.fromisoformat(stamp).timestamp()

        def sell(at: float, price: float) -> Trade:
            return Trade(proxy_wallet=USER, timestamp=int(at), condition_id=COND, side="SELL",
                         asset=TOKEN, outcome="Yes", outcome_index=0, price=price, size=12.0,
                         usd_size=12.0 * price, title="Market", slug="market", tx_hash="0xabc")

        wallet = Wallet()
        # The row's own SELL, and an older SELL of the same token from before the fence.
        wallet.trades = [sell(fenced + 10, 0.6), sell(fenced - 3600, 0.9)]
        with self.assertLogs("copy_engine", level="WARNING"):
            await CopyEngine(self.db, wallet)._recover_stuck_closings()
        row = await self.manual_row()
        self.assertEqual(("closed", 0.6), (row["status"], row["exit_price"]))
        self.assertAlmostEqual((0.6 - 0.35) * 12.0, row["realized_pnl"])


# ---------------------------------------------------------------------------
# F15: the share-dust tolerance in stuck-closing recovery
# ---------------------------------------------------------------------------
class StuckClosingDustTests(EngineDb):
    async def test_a_wallet_within_dust_of_the_row_reopens_it(self):
        await self.position(status="closing", closing_age=3600.0)
        wallet = Wallet([holding(20.0 - ce.SHARE_DUST / 2)])
        with self.assertLogs("copy_engine", level="WARNING"):
            await CopyEngine(self.db, wallet)._recover_stuck_closings()
        self.assertEqual("open", (await self.row())["status"])

    async def test_a_wallet_short_by_more_than_dust_is_left_for_review(self):
        await self.position(status="closing", closing_age=3600.0)
        wallet = Wallet([holding(20.0 - 3 * ce.SHARE_DUST)])
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await CopyEngine(self.db, wallet)._recover_stuck_closings()
        self.assertEqual("closing", (await self.row())["status"])
        self.assertIn("leaving for manual review", logs.output[0])

    async def test_a_dust_holding_counts_as_gone(self):
        await self.position(status="closing", closing_age=3600.0)
        engine = CopyEngine(self.db, Wallet([holding(ce.SHARE_DUST)]))
        engine._settle_stuck_closing = AsyncMock()
        await engine._recover_stuck_closings()
        self.assertIsNone(engine._settle_stuck_closing.await_args.args[2])


# ---------------------------------------------------------------------------
# D13a: realized_pnl in closed history
# ---------------------------------------------------------------------------
class ClosedHistoryPnlTests(EngineDb):
    async def closed(self) -> list[dict]:
        pm = SimpleNamespace(get_positions=AsyncMock(return_value=[]))
        return await closed_positions(user={"id": USER}, db=self.db, pmc=pm)

    async def closed_row(self, stored_pnl: float) -> None:
        await self.position(status="closed")
        await self.db.execute(
            "UPDATE copy_positions SET closed_at=?, exit_price=?, realized_pnl=? WHERE id='pos-1'",
            (now_iso(), 0.6, stored_pnl))

    async def event(self, pnl) -> None:
        await self.db.execute(
            "INSERT INTO trade_events(id,user_id,position_id,event_type,amount_usd,pnl,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (os.urandom(8).hex(), USER, "pos-1", "close", 10.0, pnl, now_iso()))

    async def test_closed_history_shows_the_ledger_total(self):
        await self.closed_row(stored_pnl=1.0)
        await self.event(2.0)
        await self.event(3.0)
        rows = await self.closed()
        self.assertEqual(1, len(rows))
        self.assertEqual(5.0, rows[0]["realized_pnl"])

    async def test_a_row_without_ledger_pnl_keeps_its_stored_figure(self):
        await self.closed_row(stored_pnl=1.0)
        await self.event(None)                          # an 'open' style event carries no pnl
        rows = await self.closed()
        self.assertEqual(1.0, rows[0]["realized_pnl"])

    async def test_the_row_carries_the_table_columns_and_nothing_else(self):
        await self.closed_row(stored_pnl=1.0)
        await self.event(2.0)
        rows = await self.closed()
        self.assertEqual(set(await self.row()), set(rows[0]))


if __name__ == "__main__":
    unittest.main()
