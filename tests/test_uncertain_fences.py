"""E3 from the 2026-10-18 review: exchange rejections froze tokens and blocked exits.

Every rejected order is uncertain, and uncertain BUY claims were never
released, so a FOK kill fenced that token for that user for good and, while
fenced, no SELL of the position could run. Now:
  - uncertain claims settle from the deposit wallet's on-chain balance;
  - a SELL of the tracked shares never waits for an uncertain claim;
  - the operator can list and clear fences without SQL;
  - (2026-10-19) a BUY the exchange refused outright (FOK kill, not enough
    balance / allowance) is a definite failure and leaves no fence at all.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from polymarket.clients._transport import AsyncTransport
from polymarket.models.clob import order_response
from polymarket.models.clob.order_response import RawOrderResponse, normalize_order_response

from backend.api import routes_positions
from backend.core import copy_engine as ce
from backend.core import execution
from backend.core import fences
from backend.core.copy_engine import Action, CopyEngine, chain_verdict
from backend.core.ctf import ChainBalance, ChainHead, CtfError, CtfReader, sdk_ctf_contract
from backend.core.execution import OrderResult
from backend.core.polymarket import Level, OrderBook, Position
from backend.db.database import Database, now_iso

USER = "0x" + "1" * 40
TRADER = "0x" + "2" * 40
TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
COND = "condition-1"
WAIT = ce.UNCERTAIN_CLAIM_EVIDENCE_SECONDS
OLD = WAIT + 600                 # comfortably past the evidence wait


def iso_ago(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat()


def wallet_position(size: float, avg: float = 0.5, token: str = TOKEN) -> Position:
    return Position(
        proxy_wallet=USER, asset=token, condition_id=COND, size=size,
        avg_price=avg, cur_price=avg, initial_value=size * avg,
        current_value=size * avg, cash_pnl=0.0, percent_pnl=0.0,
        realized_pnl=0.0, redeemable=False, mergeable=False,
        negative_risk=False, outcome="Yes", outcome_index=0,
        opposite_asset="", title="Market", slug="market", icon="",
        event_slug="event", end_date="")


class FakeChain:
    """Stands in for CtfReader: one head block and a balance per token."""

    def __init__(self, shares: float = 0.0, *, head_age: float = 0.0,
                 fail_head: bool = False, fail_balance: bool = False, during_read=None):
        self.shares = shares
        self.head_timestamp = int(time.time() - head_age)
        self.fail_head = fail_head
        self.fail_balance = fail_balance
        self.during_read = during_read
        self.reads: list[tuple] = []
        self.closed = False

    async def head(self) -> ChainHead:
        if self.fail_head:
            raise CtfError("eth_getBlockByNumber: HTTP 503")
        return ChainHead(number=1000, timestamp=self.head_timestamp)

    async def balance(self, owner, token_id, head) -> ChainBalance:
        self.reads.append((owner, token_id, head.number))
        if self.during_read is not None:
            await self.during_read()
        if self.fail_balance:
            raise CtfError("eth_call: RPC error -32000")
        return ChainBalance(shares=self.shares, head=head)

    async def aclose(self):
        self.closed = True


class FenceDb(unittest.IsolatedAsyncioTestCase):
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
            "INSERT INTO followed_traders(id,user_id,trader_address,copy_ratio_pct,"
            "max_position_usd,paused,is_active,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("follow-1", USER, TRADER, 1.0, 50.0, 0, 1, now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def claim(self, *, action="open", state="uncertain", age=OLD,
                    reserved=8.0, claim_id="claim-1") -> None:
        await self.db.execute(
            "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,"
            "state,reserved_usd,claimed_at,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (USER, TOKEN, TRADER, claim_id, action, state, reserved,
             iso_ago(age + 5), iso_ago(age), "order_rejected: FOK order killed"))

    async def position(self, *, status="open", shares=20.0, opened_age=OLD + 3600,
                       closing_age=None, closed_age=None, trader_shares=100.0) -> dict:
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,"
            "opened_at,closing_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("pos-1", USER, TRADER, COND, TOKEN, "Market", "YES", shares, trader_shares,
             0.5, shares * 0.5, status, iso_ago(opened_age),
             iso_ago(closing_age) if closing_age is not None else None,
             iso_ago(closed_age) if closed_age is not None else None))
        return await self.row()

    async def row(self, pid: str = "pos-1") -> dict | None:
        return await self.db.fetchone("SELECT * FROM copy_positions WHERE id=?", (pid,))

    async def claims(self) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM copy_open_claims"))

    def pm(self, wallet=(), *, complete=True, fail=False):
        async def get_all_positions(wallet_address, **kwargs):
            if fail:
                raise RuntimeError("data-api down")
            return list(wallet), complete

        async def get_positions(wallet_address, **kwargs):
            return [p for p in wallet if wallet_address == USER]

        return SimpleNamespace(get_all_positions=get_all_positions, get_positions=get_positions)

    def engine(self, wallet=(), *, chain=None, complete=True, fail=False, **kwargs) -> CopyEngine:
        return CopyEngine(self.db, self.pm(wallet, complete=complete, fail=fail), ctf=chain,
                          **kwargs)


# ---------------------------------------------------------------------------
# The chain reader
# ---------------------------------------------------------------------------
def rpc(results: dict, seen: list, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        call = json.loads(request.content)
        seen.append(call)
        if status != 200:
            return httpx.Response(status, request=request)
        result = results[call["method"]]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, dict) and "error" in result:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": call["id"],
                                             "error": result["error"]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": call["id"], "result": result})
    return httpx.MockTransport(handler)


POLYGON = {
    "eth_chainId": "0x89",
    "eth_getCode": "0x6080604052",
    "eth_getBlockByNumber": {"number": "0x10", "timestamp": hex(1_760_000_000)},
    "eth_call": "0x" + format(2_500_000, "064x"),
}
SECRET_URL = "https://polygon.example/v2/secret-api-key"


class CtfReaderTests(unittest.IsolatedAsyncioTestCase):
    def reader(self, results=None, seen=None, **kwargs) -> CtfReader:
        return CtfReader(SECRET_URL, transport=rpc(results or POLYGON,
                                                    seen if seen is not None else [], **kwargs))

    def test_the_contract_is_the_one_the_sdk_trades_against(self):
        self.assertEqual(("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045", 137), sdk_ctf_contract())

    async def test_balance_is_read_with_balance_of_at_the_named_block(self):
        seen: list = []
        reader = self.reader(seen=seen)
        head = await reader.head()
        held = await reader.balance(USER, TOKEN, head)

        self.assertEqual(ChainHead(number=16, timestamp=1_760_000_000), head)
        self.assertEqual(2.5, held.shares)
        call = next(c for c in seen if c["method"] == "eth_call")
        target, block = call["params"]
        self.assertEqual("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045", target["to"])
        self.assertEqual("0x10", block)
        self.assertEqual("0x00fdd58e" + ("1" * 40).rjust(64, "0") + format(int(TOKEN), "064x"),
                         target["data"])
        await reader.aclose()

    async def test_chain_and_contract_are_verified_once(self):
        seen: list = []
        reader = self.reader(seen=seen)
        await reader.head()
        await reader.head()
        self.assertEqual(1, sum(c["method"] == "eth_chainId" for c in seen))
        self.assertEqual(1, sum(c["method"] == "eth_getCode" for c in seen))
        await reader.aclose()

    async def test_a_testnet_rpc_gives_no_evidence(self):
        # a zero balance from the wrong chain would release every fence
        reader = self.reader({**POLYGON, "eth_chainId": hex(80002)})
        with self.assertRaisesRegex(CtfError, "chain 80002"):
            await reader.head()
        await reader.aclose()

    async def test_an_address_without_code_gives_no_evidence(self):
        reader = self.reader({**POLYGON, "eth_getCode": "0x"})
        with self.assertRaisesRegex(CtfError, "no contract code"):
            await reader.head()
        await reader.aclose()

    async def test_failures_never_carry_the_rpc_url(self):
        failures = [
            {"status": 401},
            {"results": {**POLYGON, "eth_chainId": httpx.ConnectError(f"failed {SECRET_URL}")}},
            {"results": {**POLYGON, "eth_chainId": {"error": {"code": -32005, "message": SECRET_URL}}}},
        ]
        for failure in failures:
            with self.subTest(failure=str(failure)[:40]):
                reader = CtfReader(SECRET_URL, transport=rpc(
                    failure.get("results", POLYGON), [], status=failure.get("status", 200)))
                with self.assertRaises(CtfError) as raised:
                    await reader.head()
                self.assertNotIn("secret-api-key", str(raised.exception))
                # nor can a logged traceback show the original exception's text
                self.assertTrue(raised.exception.__context__ is None
                                or raised.exception.__suppress_context__)
                await reader.aclose()

    async def test_malformed_answers_are_errors_not_zero(self):
        reader = self.reader({**POLYGON, "eth_call": "0x"})
        head = await reader.head()
        with self.assertRaisesRegex(CtfError, "malformed"):
            await reader.balance(USER, TOKEN, head)
        with self.assertRaisesRegex(CtfError, "uint256"):
            await reader.balance(USER, "not-a-token", head)
        await reader.aclose()


# ---------------------------------------------------------------------------
# The evidence rule (pure)
# ---------------------------------------------------------------------------
class ChainVerdictTests(unittest.TestCase):
    def verdict(self, *, held, indexed, rows=(), waited=WAIT + 60):
        head = ChainHead(number=1000, timestamp=int(time.time()))
        last_change = dt.datetime.fromtimestamp(head.timestamp - waited, dt.timezone.utc)
        return chain_verdict(rows=list(rows), last_change=last_change,
                             onchain=ChainBalance(shares=held, head=head), indexed=indexed)[0]

    def test_nothing_held_beyond_the_tracked_shares_releases(self):
        self.assertEqual(ce.RELEASE, self.verdict(held=0.0, indexed=0.0))
        self.assertEqual(ce.RELEASE, self.verdict(
            held=20.004, indexed=20.0, rows=[{"status": "open", "shares": 20.0}]))

    def test_the_indexer_can_veto_a_release(self):
        self.assertEqual(ce.RETAIN, self.verdict(held=0.0, indexed=16.0))

    def test_a_block_too_close_to_the_last_change_proves_nothing(self):
        self.assertEqual(ce.RETAIN, self.verdict(held=0.0, indexed=0.0, waited=WAIT - 1))

    def test_unknown_timing_retains(self):
        head = ChainHead(number=1, timestamp=int(time.time()))
        self.assertEqual(ce.RETAIN, chain_verdict(
            rows=[], last_change=None, onchain=ChainBalance(0.0, head), indexed=0.0)[0])

    def test_a_row_mid_close_or_duplicate_rows_retain(self):
        self.assertEqual(ce.RETAIN, self.verdict(
            held=0.0, indexed=0.0, rows=[{"status": "closing", "shares": 20.0}]))
        self.assertEqual(ce.RETAIN, self.verdict(
            held=0.0, indexed=0.0, rows=[{"status": "open", "shares": 20.0},
                                         {"status": "reconciliation_required", "shares": 5.0}]))

    def test_extra_shares_both_sources_agree_on_are_adopted(self):
        self.assertEqual(ce.ADOPT, self.verdict(held=16.0, indexed=16.0))
        self.assertEqual(ce.ADOPT, self.verdict(
            held=30.0, indexed=30.0, rows=[{"status": "open", "shares": 20.0}]))

    def test_extra_shares_the_indexer_cannot_confirm_retain(self):
        self.assertEqual(ce.RETAIN, self.verdict(held=16.0, indexed=0.0))
        self.assertEqual(ce.RETAIN, self.verdict(held=16.0, indexed=12.0))


# ---------------------------------------------------------------------------
# The engine settles uncertain claims from the chain
# ---------------------------------------------------------------------------
class OnChainSettlementTests(FenceDb):
    async def test_a_killed_order_with_nothing_on_chain_is_released_loudly(self):
        await self.claim()
        chain = FakeChain(0.0)
        engine = self.engine(chain=chain)

        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await engine._reconcile_uncertain_claims()

        self.assertEqual(0, await self.claims())
        self.assertEqual([(USER, TOKEN, 1000)], chain.reads)
        self.assertTrue(any("RELEASED on on-chain evidence" in line for line in logs.output))
        # the token is copyable again
        action = Action(kind="open", token_id=TOKEN, condition_id=COND, outcome="YES",
                        side="BUY", amount=8.0, notional_usd=8.0, reference_price=0.5,
                        trader_shares=2000.0, trader_address=TRADER,
                        position=SimpleNamespace(cur_price=0.5, current_value=1000.0, size=2000.0))
        self.assertIsNotNone(await engine._prepare_buy(USER, action))

    async def test_a_young_claim_is_neither_read_on_chain_nor_released(self):
        await self.claim(age=WAIT / 2)
        chain = FakeChain(0.0)

        await self.engine(chain=chain)._reconcile_uncertain_claims()

        self.assertEqual(1, await self.claims())
        self.assertEqual([], chain.reads)

    async def test_a_lagging_node_cannot_release(self):
        # the node's newest block is too close to the claim to cover late settlement
        await self.claim(age=WAIT + 60)
        await self.engine(chain=FakeChain(0.0, head_age=120))._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())

    async def test_shares_on_chain_are_adopted_within_the_budget(self):
        await self.claim()
        engine = self.engine([wallet_position(16.0)], chain=FakeChain(16.0))

        await engine._reconcile_uncertain_claims()

        self.assertEqual(0, await self.claims())
        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE token_id=?", (TOKEN,))
        self.assertEqual(("open", 16.0, 8.0), (row["status"], row["shares"], row["notional_usd"]))

    async def test_shares_beyond_the_budget_stay_quarantined(self):
        await self.claim()   # $8 reserved, $20 held
        await self.engine([wallet_position(40.0)], chain=FakeChain(40.0))._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM copy_positions"))

    async def test_chain_shares_the_indexer_cannot_price_are_retained(self):
        await self.claim()
        await self.engine([], chain=FakeChain(16.0))._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())
        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM copy_positions"))

    async def test_an_indexer_holding_the_chain_does_not_show_blocks_the_release(self):
        await self.claim()
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            await self.engine([wallet_position(16.0)], chain=FakeChain(0.0))._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())
        self.assertTrue(any("sources disagree" in line for line in logs.output))

    async def test_a_failed_chain_read_keeps_todays_behaviour(self):
        for chain in (FakeChain(0.0, fail_head=True), FakeChain(0.0, fail_balance=True)):
            with self.subTest(head_fails=chain.fail_head):
                await self.claim()
                await self.engine([], chain=chain)._reconcile_uncertain_claims()
                self.assertEqual(1, await self.claims())
                await self.db.execute("DELETE FROM copy_open_claims")

    async def test_without_an_rpc_nothing_is_ever_released(self):
        await self.claim(age=ce.UNCERTAIN_CLAIM_RELEASE_SECONDS + 60)
        await self.engine([], chain=None)._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())

    async def test_a_failed_or_truncated_wallet_scan_blocks_the_release(self):
        for kwargs in ({"fail": True}, {"complete": False}):
            with self.subTest(**kwargs):
                await self.claim()
                await self.engine([], chain=FakeChain(0.0), **kwargs)._reconcile_uncertain_claims()
                self.assertEqual(1, await self.claims())
                await self.db.execute("DELETE FROM copy_open_claims")

    async def test_a_top_up_that_left_the_row_unchanged_is_released(self):
        await self.position(shares=20.0)
        await self.claim(action="resize")
        await self.engine([wallet_position(20.0)], chain=FakeChain(20.0))._reconcile_uncertain_claims()
        self.assertEqual(0, await self.claims())
        self.assertEqual(20.0, (await self.row())["shares"])

    async def test_a_top_up_the_chain_shows_is_adopted_into_the_row(self):
        await self.position(shares=20.0)
        await self.claim(action="resize")

        await self.engine([wallet_position(30.0)], chain=FakeChain(30.0))._reconcile_uncertain_claims()

        self.assertEqual(0, await self.claims())
        self.assertEqual(30.0, (await self.row())["shares"])
        self.assertEqual(1, await self.db.fetchval(
            "SELECT COUNT(*) FROM trade_events WHERE event_type='partial'"))

    async def test_a_row_closed_meanwhile_with_nothing_left_is_released(self):
        await self.position(status="closed", closing_age=WAIT + 130, closed_age=WAIT + 120)
        await self.claim(action="resize")
        await self.engine([], chain=FakeChain(0.0))._reconcile_uncertain_claims()
        self.assertEqual(0, await self.claims())

    async def test_a_row_closed_meanwhile_with_shares_left_adopts_a_new_row(self):
        await self.position(status="closed", closing_age=WAIT + 130, closed_age=WAIT + 120)
        await self.claim(action="resize")

        await self.engine([wallet_position(10.0)], chain=FakeChain(10.0))._reconcile_uncertain_claims()

        self.assertEqual(0, await self.claims())
        adopted = await self.db.fetchone(
            "SELECT * FROM copy_positions WHERE token_id=? AND status='open'", (TOKEN,))
        self.assertEqual((10.0, TRADER), (adopted["shares"], adopted["trader_address"]))

    async def test_a_recent_close_waits_for_the_chain_to_settle(self):
        # the close's own SELL may not have settled on-chain yet
        await self.position(status="closed", closing_age=70, closed_age=60)
        await self.claim(action="resize")
        await self.engine([], chain=FakeChain(0.0))._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())

    async def test_a_row_mid_close_is_retained(self):
        await self.position(status="closing", closing_age=OLD)
        await self.claim(action="resize")
        await self.engine([], chain=FakeChain(0.0))._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())

    async def test_a_sell_claiming_the_row_during_the_read_keeps_the_claim(self):
        await self.position(shares=20.0)
        await self.claim(action="resize")

        sells = []

        async def sell_starts():
            sells.append(await self.db.claim_managed_sell(USER, TOKEN, "pos-1"))

        await self.engine([wallet_position(20.0)],
                          chain=FakeChain(0.0, during_read=sell_starts))._reconcile_uncertain_claims()
        self.assertEqual([True], sells)
        self.assertEqual(1, await self.claims())

    async def test_release_rechecks_the_tracked_rows_under_the_user_lock(self):
        row = await self.position(shares=20.0)
        await self.claim(action="resize")
        claim = await self.db.fetchone("SELECT * FROM copy_open_claims")
        engine = self.engine()

        self.assertTrue(await self.db.claim_managed_sell(USER, TOKEN, "pos-1"))
        self.assertFalse(await engine._release_on_evidence(USER, claim, [row]))
        await self.db.execute("UPDATE copy_positions SET status='closed' WHERE id='pos-1'")
        self.assertFalse(await engine._release_on_evidence(USER, claim, [row]))
        self.assertTrue(await engine._release_on_evidence(USER, claim, []))
        self.assertEqual(0, await self.claims())

    async def test_a_top_up_adoption_never_overwrites_a_row_a_sell_just_changed(self):
        stale = await self.position(shares=20.0)
        await self.claim(action="resize")
        claim = await self.db.fetchone("SELECT * FROM copy_open_claims")
        # a partial SELL of 10 lands between the read and the adoption
        await self.db.execute("UPDATE copy_positions SET shares=10.0 WHERE id='pos-1'")

        with self.assertRaisesRegex(RuntimeError, "lost position race"):
            await self.engine()._adopt_uncertain_resize(USER, claim, stale, wallet_position(30.0))

        self.assertEqual(10.0, (await self.row())["shares"])
        self.assertEqual(1, await self.claims(), "the claim delete rolled back with it")

    async def test_closing_the_engine_closes_the_reader(self):
        chain = FakeChain()
        await self.engine(chain=chain).aclose()
        self.assertTrue(chain.closed)


# ---------------------------------------------------------------------------
# Exits never wait for an uncertain claim
# ---------------------------------------------------------------------------
class ExitsDuringUncertainClaimTests(FenceDb):
    async def test_a_sell_claims_the_row_despite_an_uncertain_buy(self):
        await self.position()
        await self.claim(action="resize")
        self.assertTrue(await self.db.claim_managed_sell(USER, TOKEN, "pos-1"))
        self.assertEqual("closing", (await self.row())["status"])

    async def test_an_in_flight_buy_still_blocks_the_sell(self):
        await self.position()
        for state in ("reserved", "submitting"):
            with self.subTest(state=state):
                await self.claim(action="resize", state=state, age=10)
                self.assertFalse(await self.db.claim_managed_sell(USER, TOKEN, "pos-1"))
                self.assertEqual("open", (await self.row())["status"])
                await self.db.execute("DELETE FROM copy_open_claims")

    async def test_the_reconciler_closes_the_tracked_shares(self):
        row = await self.position(shares=20.0)
        await self.claim(action="resize")
        place = AsyncMock(return_value=OrderResult(ok=True, filled_shares=20.0, avg_price=0.55))
        engine = self.engine(place_order=place)

        await engine._execute(USER, object(), Action(
            kind="close", token_id=TOKEN, side="SELL", amount=row["shares"], row=row))

        self.assertEqual(20.0, place.await_args.args[4])
        self.assertEqual("closed", (await self.row())["status"])
        self.assertEqual(1, await self.claims(), "the fence still guards BUYs")

    async def test_the_fast_path_exit_runs(self):
        await self.position(shares=20.0, trader_shares=100.0)
        await self.claim(action="resize")
        follow = await self.db.fetchone("SELECT * FROM followed_traders")
        place = AsyncMock(return_value=OrderResult(ok=True, filled_shares=20.0, avg_price=0.55))

        async def client_factory(user):
            return object()

        engine = self.engine(place_order=place, client_factory=client_factory)
        trade = SimpleNamespace(side="SELL", asset=TOKEN, size=100.0, price=0.5,
                                timestamp=int(time.time()), condition_id=COND, tx_hash="0xexit")
        await engine._handle_leader_trade(follow, trade)

        place.assert_awaited_once()
        self.assertEqual("closed", (await self.row())["status"])

    async def test_buys_stay_fenced(self):
        await self.claim()
        action = Action(kind="open", token_id=TOKEN, condition_id=COND, outcome="YES",
                        side="BUY", amount=8.0, notional_usd=8.0, reference_price=0.5,
                        trader_shares=2000.0, trader_address=TRADER,
                        position=SimpleNamespace(cur_price=0.5, current_value=1000.0, size=2000.0))
        self.assertIsNone(await self.engine()._prepare_buy(USER, action))

    async def close_route(self, place):
        pmc = self.pm([wallet_position(26.0)])
        with (patch.object(routes_positions, "get_user_client", AsyncMock(return_value=object())),
              patch.object(routes_positions.execution, "place_market_order", place)):
            return await routes_positions.close_position(
                "pos-1", SimpleNamespace(), routes_positions.CloseBody(),
                user={"id": USER}, db=self.db, pmc=pmc)

    async def test_the_users_close_sells_through_an_uncertain_claim(self):
        await self.position(shares=20.0)
        await self.claim(action="resize")
        place = AsyncMock(return_value=OrderResult(ok=True, filled_shares=20.0, avg_price=0.55))

        response = await self.close_route(place)

        self.assertTrue(response["ok"])
        self.assertEqual(20.0, place.await_args.args[4], "sells the tracked shares only")
        self.assertEqual("closed", (await self.row())["status"])

    async def test_the_users_close_still_waits_for_an_in_flight_buy(self):
        await self.position(shares=20.0)
        await self.claim(action="resize", state="submitting", age=10)
        place = AsyncMock()
        with self.assertRaises(HTTPException) as raised:
            await self.close_route(place)
        self.assertEqual(409, raised.exception.status_code)
        place.assert_not_awaited()

    async def test_the_open_view_keeps_the_row_closable(self):
        await self.position(shares=20.0)
        await self.claim(action="resize")

        rows = await routes_positions.open_positions(
            user={"id": USER}, db=self.db, pmc=self.pm([wallet_position(26.0)]))

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(("open", False), (row["status"], row["reconciliation_required"]))
        self.assertEqual((20.0, 26.0), (row["shares"], row["live_shares"]))
        self.assertEqual(("uncertain", "resize"), (row["claim_state"], row["claim_action"]))

    async def test_the_open_view_still_flags_an_in_flight_buy(self):
        await self.position(shares=20.0)
        await self.claim(action="resize", state="reserved", age=10)
        rows = await routes_positions.open_positions(
            user={"id": USER}, db=self.db, pmc=self.pm([wallet_position(20.0)]))
        self.assertTrue(rows[0]["reconciliation_required"])


# ---------------------------------------------------------------------------
# Without on-chain evidence, settlement tolerates the row moving under it
# ---------------------------------------------------------------------------
class SettlementAfterExitTests(FenceDb):
    async def test_a_holding_left_after_the_row_closed_is_adopted(self):
        await self.position(status="closed", closing_age=3700, closed_age=3600)
        await self.claim(action="resize")
        await self.engine([wallet_position(10.0)])._reconcile_uncertain_claims()
        self.assertEqual(0, await self.claims())
        self.assertEqual(1, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_positions WHERE status='open'"))

    async def test_nothing_left_after_the_row_closed_is_never_released(self):
        await self.position(status="closed", closing_age=3700, closed_age=3600)
        await self.claim(action="resize")
        await self.engine([])._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())

    async def test_an_indexer_behind_a_recent_partial_sell_is_not_a_fill(self):
        # 10 of 20 shares were just sold; the indexer still shows the 20
        await self.position(shares=10.0, closing_age=30)
        await self.claim(action="resize")
        await self.engine([wallet_position(20.0)])._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())
        self.assertEqual(10.0, (await self.row())["shares"])

    async def test_an_indexer_behind_a_recent_close_is_not_adopted(self):
        await self.position(status="closed", closing_age=40, closed_age=30)
        await self.claim(action="resize")
        await self.engine([wallet_position(20.0)])._reconcile_uncertain_claims()
        self.assertEqual(1, await self.claims())
        self.assertEqual(0, await self.db.fetchval(
            "SELECT COUNT(*) FROM copy_positions WHERE status='open'"))


# ---------------------------------------------------------------------------
# The operator's fence tool
# ---------------------------------------------------------------------------
class FenceToolTests(FenceDb):
    async def run_list(self, **kwargs) -> str:
        lines: list[str] = []
        self.assertEqual(0, await fences.list_fences(self.db, out=lines.append, **kwargs))
        return "\n".join(lines)

    async def run_clear(self, *, expect: int, **kwargs) -> str:
        lines: list[str] = []
        self.assertEqual(expect, await fences.clear_fence(
            self.db, kwargs.pop("claim_id", "claim-1"), out=lines.append, **kwargs))
        return "\n".join(lines)

    async def test_list_shows_each_fence_with_its_tracked_row(self):
        await self.position(shares=20.0)
        await self.claim(action="resize")

        text = await self.run_list()

        self.assertIn("claim-1", text)
        self.assertIn("uncertain", text)
        self.assertIn("open row pos-1 (20.0000 shares)", text)
        self.assertIn("0x1111…1111", text)
        self.assertNotIn(USER, text, "wallet addresses are shortened")

    async def test_list_with_evidence_shows_what_the_engine_will_do(self):
        await self.claim()
        pm = SimpleNamespace(get_all_positions=AsyncMock(return_value=([], True)))
        text = await self.run_list(pm=pm, ctf=FakeChain(0.0), evidence=True)
        self.assertIn("on-chain: 0.0000 shares at block 1000", text)
        self.assertIn("engine: RELEASE", text)

    async def test_list_without_fences_says_so(self):
        self.assertIn("no active BUY fences", await self.run_list())

    async def test_clear_is_a_dry_run_until_yes(self):
        await self.claim()
        text = await self.run_clear(expect=0, ctf=FakeChain(0.0))
        self.assertIn("dry run", text)
        self.assertEqual(1, await self.claims())

    async def test_clear_with_yes_deletes_the_fence(self):
        await self.claim()
        text = await self.run_clear(expect=0, ctf=FakeChain(0.0), yes=True)
        self.assertIn("cleared fence claim-1", text)
        self.assertEqual(0, await self.claims())

    async def test_clear_never_touches_in_flight_claims(self):
        await self.claim(state="submitting", age=10)
        await self.run_clear(expect=2, ctf=FakeChain(0.0), yes=True)
        self.assertEqual(1, await self.claims())

    async def test_clear_refuses_while_the_chain_shows_untracked_shares(self):
        await self.claim()
        text = await self.run_clear(expect=2, ctf=FakeChain(16.0), yes=True)
        self.assertIn("16.0000 shares on-chain beyond", text)
        self.assertEqual(1, await self.claims())
        text = await self.run_clear(expect=0, ctf=FakeChain(16.0), yes=True, force=True)
        self.assertIn("(forced)", text)
        self.assertEqual(0, await self.claims())

    async def test_clear_refuses_when_the_chain_cannot_be_read(self):
        await self.claim()
        await self.run_clear(expect=2, ctf=FakeChain(fail_head=True), yes=True)
        self.assertEqual(1, await self.claims())

    async def test_clear_without_an_rpc_warns_but_obeys_yes(self):
        await self.claim()
        text = await self.run_clear(expect=0, ctf=None, yes=True)
        self.assertIn("POLYGON_RPC_URL is not set", text)
        self.assertEqual(0, await self.claims())

    async def test_an_unknown_claim_id_is_an_error(self):
        await self.run_clear(expect=1, claim_id="no-such-claim")

    def test_the_cli_parses_both_commands(self):
        parser = fences.build_parser()
        self.assertTrue(parser.parse_args(["list", "--evidence"]).evidence)
        args = parser.parse_args(["clear", "claim-1", "--yes"])
        self.assertEqual(("claim-1", True, False), (args.claim_id, args.yes, args.force))


# ---------------------------------------------------------------------------
# The exchange's terminal refusals (2026-10-19)
# ---------------------------------------------------------------------------
FOK_KILLED = "order couldn't be fully filled. FOK orders are fully filled or killed."
NO_BALANCE = "not enough balance / allowance"


def clob_body(**fields) -> dict:
    """A POST /order response body, success=false unless overridden."""
    body = {"success": False, "errorMsg": "", "orderID": "", "status": "",
            "makingAmount": "", "takingAmount": ""}
    body.update(fields)
    return body


class SdkClob:
    """Answers POST /order with one canned CLOB response, decoded by the pinned
    polymarket-client's own transport and response parser, so execution sees
    exactly the exception or RejectedOrder it would get in production."""

    def __init__(self, status, payload=None):
        self.status, self.payload, self.calls = status, payload, 0

    async def place_market_order(self, **kwargs):
        self.calls += 1

        def answer(request):
            if self.status == "timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            return httpx.Response(self.status, json=self.payload)

        async with httpx.AsyncClient(base_url="https://clob.test",
                                     transport=httpx.MockTransport(answer)) as http:
            data = await AsyncTransport(base_url="https://clob.test",
                                        client=http).post_json("/order", json={})
        return normalize_order_response(RawOrderResponse.parse_response(data))


class Book:
    async def get_geoblock(self):
        return {"blocked": False}

    async def get_orderbook(self, token_id):
        return OrderBook(token_id=token_id, condition_id=COND, bids=(Level(0.49, 1000),),
                         asks=(Level(0.5, 1000),), tick_size=0.01, min_order_size=1,
                         neg_risk=False, last_trade_price=0.5)


class TerminalRejectionTests(unittest.IsolatedAsyncioTestCase):
    """Which answers after submission prove that a BUY never executed."""

    async def place(self, clob, side="BUY"):
        return await execution.place_market_order(clob, Book(), TOKEN, side, 10.0,
                                                  reference_price=0.5)

    async def test_the_two_terminal_verdicts_fail_the_buy_definitely(self):
        figures = NO_BALANCE + ": the balance is not enough -> balance: 0, order amount: 10000000"
        for name, clob, code in (
                ("FOK kill, HTTP 400", SdkClob(400, {"error": FOK_KILLED}), "fok_not_filled"),
                ("balance, HTTP 400", SdkClob(400, {"error": NO_BALANCE}), "not_enough_balance"),
                ("balance with figures", SdkClob(400, {"error": figures}), "not_enough_balance"),
                ("FOK kill, success=false", SdkClob(200, clob_body(errorMsg=FOK_KILLED)),
                 "fok_not_filled"),
                ("balance, success=false", SdkClob(200, clob_body(errorMsg=NO_BALANCE)),
                 "not_enough_balance")):
            with self.subTest(name):
                res = await self.place(clob)
                self.assertEqual(1, clob.calls, "the order did go on the wire")
                self.assertFalse(res.ok)
                self.assertFalse(res.submission_uncertain)
                self.assertEqual(code, res.exchange_rejection)

    async def test_every_other_answer_after_submission_stays_uncertain(self):
        delayed = "order match delayed due to market conditions"
        for name, clob in (
                ("transport timeout", SdkClob("timeout")),
                ("rate limited", SdkClob(429, {"error": "Too Many Requests"})),
                ("5xx carrying the FOK text", SdkClob(500, {"error": FOK_KILLED})),
                ("another 4xx carrying the FOK text", SdkClob(422, {"error": FOK_KILLED})),
                ("400 with any other text", SdkClob(400, {"error": "invalid order payload"})),
                ("a reworded FOK text", SdkClob(400, {"error": "order couldn't be fully filled"})),
                ("400 merely mentioning the balance text", SdkClob(400, {
                    "error": "could not run the execution: " + NO_BALANCE})),
                # The next two can still execute after the response is sent.
                ("delayed, carrying an errorMsg", SdkClob(200, clob_body(
                    success=True, status="delayed", orderID="0xabc", errorMsg=delayed))),
                ("accepted, nothing filled yet", SdkClob(200, clob_body(
                    success=True, status="delayed", orderID="0xabc"))),
                ("unmatched placement", SdkClob(200, clob_body(
                    status="unmatched", errorMsg=NO_BALANCE)))):
            with self.subTest(name):
                res = await self.place(clob)
                self.assertFalse(res.ok)
                self.assertTrue(res.submission_uncertain)
                self.assertEqual("", res.exchange_rejection)

    async def test_a_sell_keeps_todays_classification(self):
        """Stuck-closing recovery settles an uncertain SELL from the wallet, and
        also finalizes a row whose shares are already gone, the usual reason a
        SELL is refused for balance. Only the BUY fence changed."""
        for text in (FOK_KILLED, NO_BALANCE):
            with self.subTest(text):
                res = await self.place(SdkClob(400, {"error": text}), side="SELL")
                self.assertTrue(res.submission_uncertain)
                self.assertEqual("", res.exchange_rejection)

    def test_the_texts_match_the_pinned_sdk(self):
        """A new SDK release must not silently change what counts as terminal."""
        self.assertEqual(FOK_KILLED, order_response._FOK_NOT_FILLED_MSG)
        self.assertEqual(FOK_KILLED, execution._FOK_NOT_FILLED)
        self.assertEqual(NO_BALANCE, execution._NOT_ENOUGH_BALANCE)


class RefusedBuyTests(FenceDb):
    """A refused BUY leaves no fence and holds no budget, and the engine still
    does not hammer a book that just refused it."""

    def buy_engine(self) -> CopyEngine:
        pm = self.pm()
        pm.get_geoblock = Book().get_geoblock
        pm.get_orderbook = Book().get_orderbook
        return CopyEngine(self.db, pm)

    def open_action(self, token: str = TOKEN) -> Action:
        return Action(kind="open", token_id=token, condition_id=COND, outcome="YES",
                      side="BUY", amount=8.0, notional_usd=8.0, reference_price=0.5,
                      trader_shares=2000.0, trader_address=TRADER,
                      position=SimpleNamespace(cur_price=0.5, current_value=1000.0,
                                               size=2000.0))

    async def other_token_reservation(self, engine) -> float:
        """What a BUY of another token may reserve under a $12 user cap."""
        prepared = await engine._prepare_buy(USER, self.open_action("other-token"))
        await self.db.execute("DELETE FROM copy_open_claims WHERE token_id='other-token'")
        return prepared[0].amount

    async def fence_list(self) -> str:
        lines: list[str] = []
        await fences.list_fences(self.db, out=lines.append)
        return "\n".join(lines)

    async def test_a_refused_buy_leaves_no_fence_and_releases_its_budget(self):
        await self.db.execute("UPDATE users SET max_total_exposure_usd=12 WHERE id=?", (USER,))
        for text in (FOK_KILLED, NO_BALANCE):
            with self.subTest(text):
                engine, clob = self.buy_engine(), SdkClob(400, {"error": text})
                self.assertEqual(0.0, await engine._execute(USER, clob, self.open_action()))
                self.assertEqual(1, clob.calls)
                self.assertEqual(0, await self.claims(), "no claim of any state is kept")
                self.assertIn("no active BUY fences", await self.fence_list())
                self.assertEqual(8.0, await self.other_token_reservation(engine),
                                 "the refused $8 still counts against the user cap")
                self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM copy_positions"))

    async def test_the_fill_or_kill_budget_brakes_the_retry(self):
        """2026-10-19: once the fence was cleared, the engine re-fired within
        seconds and the book killed it again. Each refusal spends the budget,
        so the leader's intent is dropped after MAX_FILL_ATTEMPTS."""
        engine, clob = self.buy_engine(), SdkClob(400, {"error": FOK_KILLED})
        for _ in range(ce.MAX_FILL_ATTEMPTS + 3):
            await engine._execute(USER, clob, self.open_action())
        self.assertEqual(ce.MAX_FILL_ATTEMPTS, clob.calls)
        self.assertEqual(0, await self.claims())

    async def test_a_refusal_that_used_the_whole_cap_waits_out_its_ttl(self):
        """The refused notional went on the wire, so it keeps counting against
        the cap for SUBMITTED_BASIS_TTL_SECONDS: no immediate re-fire."""
        await self.db.execute("UPDATE followed_traders SET max_position_usd=8 WHERE id='follow-1'")
        engine, clob = self.buy_engine(), SdkClob(400, {"error": FOK_KILLED})
        await engine._execute(USER, clob, self.open_action())
        await engine._execute(USER, clob, self.open_action())          # the next tick
        self.assertEqual(1, clob.calls, "re-fired while the refused notional still counts")
        self.assertEqual(0, await self.claims())

        engine._submitted[(USER, TOKEN)][1] = time.monotonic() - 1     # TTL lapsed
        await engine._execute(USER, clob, self.open_action())
        self.assertEqual(2, clob.calls)

    async def test_an_ambiguous_outcome_still_fences_and_keeps_its_budget(self):
        await self.db.execute("UPDATE users SET max_total_exposure_usd=12 WHERE id=?", (USER,))
        for name, clob in (("timeout", SdkClob("timeout")),
                           ("rate limited", SdkClob(429, {"error": "Too Many Requests"})),
                           ("other 400", SdkClob(400, {"error": "invalid order payload"}))):
            with self.subTest(name):
                engine = self.buy_engine()
                await engine._execute(USER, clob, self.open_action())
                await engine._execute(USER, clob, self.open_action())  # the next tick
                self.assertEqual(1, clob.calls, "the fence blocks the retry")
                self.assertEqual(1, await self.db.fetchval(
                    "SELECT COUNT(*) FROM copy_open_claims WHERE state='uncertain'"))
                self.assertEqual(4.0, await self.other_token_reservation(engine),
                                 "the fenced $8 stays reserved")
                await self.db.execute("DELETE FROM copy_open_claims")


if __name__ == "__main__":
    unittest.main()
