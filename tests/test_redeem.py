"""Redeeming resolved winnings (review finding E5, 2026-10-18).

Everything runs against fakes: a fake SDK client hands each relayer batch to a
fake chain, which decodes the calls exactly as the CTF adapters would receive
them. No network, no funds.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address
from fastapi.testclient import TestClient
from polymarket import errors as pm_errors
from polymarket.environments import PRODUCTION

from backend.core import redeem
from backend.core.health import Heartbeats
from backend.core.polymarket import Position
from backend.db.database import Database, now_iso

CONTRACTS = redeem.contracts_for(PRODUCTION)
USER = "0x" + "1" * 40
OTHER = "0x" + "3" * 40
TRADER = "0x" + "2" * 40
COND = "0x" + "ab" * 32
COND_B = "0x" + "cd" * 32
YES, NO = "1001", "1002"
YES_B, NO_B = "2001", "2002"
TEN = 10 * redeem.UNITS_PER_USD          # ten winning shares, in token units

SET_APPROVAL = keccak(text="setApprovalForAll(address,bool)")[:4]
REDEEM_POSITIONS = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
TX_HASH = "0x" + "f" * 64


def position(cid=COND, asset=YES, *, opposite=NO, index=0, size=10.0, value=10.0,
             redeemable=True, neg_risk=False, wallet=USER) -> Position:
    return Position(
        proxy_wallet=wallet, asset=asset, condition_id=cid, size=size, avg_price=0.5,
        cur_price=(value / size) if size else 0.0, initial_value=size * 0.5,
        current_value=value, cash_pnl=0.0, percent_pnl=0.0, realized_pnl=0.0,
        redeemable=redeemable, mergeable=False, negative_risk=neg_risk,
        outcome="Yes" if index == 0 else "No", outcome_index=index,
        opposite_asset=opposite, title="Will it happen?", slug="m", icon="",
        event_slug="e", end_date="")


def gamma_market(cid, yes, no, *, neg_risk=False):
    return SimpleNamespace(
        condition_id=cid, state=SimpleNamespace(neg_risk=neg_risk),
        outcomes=SimpleNamespace(yes=SimpleNamespace(token_id=yes),
                                 no=SimpleNamespace(token_id=no)))


class FakeChain:
    """The CTF, its two collateral adapters and pUSD, reduced to dicts."""

    def __init__(self):
        self.denominators: dict[str, int] = {}
        self.numerators: dict[tuple[str, int], int] = {}
        self.balances: dict[tuple[str, str], int] = {}
        self.approved: set[tuple[str, str]] = set()
        self.pusd: dict[str, int] = {}
        self.markets: dict[str, SimpleNamespace] = {}

    # --- the ChainReader surface -------------------------------------------
    async def payout_denominator(self, ctf, cid):
        assert ctf == CONTRACTS.ctf
        return self.denominators.get(cid, 0)

    async def payout_numerator(self, ctf, cid, index):
        assert ctf == CONTRACTS.ctf
        return self.numerators.get((cid, index), 0)

    async def balance_of(self, ctf, owner, token_id):
        assert ctf == CONTRACTS.ctf
        return self.balances.get((owner.lower(), token_id), 0)

    async def is_approved_for_all(self, ctf, owner, operator):
        assert ctf == CONTRACTS.ctf
        return (owner.lower(), operator.lower()) in self.approved

    # --- executing a relayer batch ------------------------------------------
    def execute(self, wallet: str, calls) -> None:
        for call in calls:
            data = bytes.fromhex(call.data[2:])
            if data[:4] == SET_APPROVAL:
                assert call.to.lower() == CONTRACTS.ctf.lower()
                operator, approved = decode(["address", "bool"], data[4:])
                if approved:
                    self.approved.add((wallet.lower(), operator.lower()))
            elif data[:4] == REDEEM_POSITIONS:
                self._redeem(wallet, call.to, data[4:])
            else:
                raise AssertionError(f"unexpected call {data[:4].hex()}")

    def _redeem(self, wallet, adapter, args) -> None:
        _collateral, parent, cid_bytes, index_sets = decode(
            ["address", "bytes32", "bytes32", "uint256[]"], args)
        assert parent == b"\x00" * 32 and list(index_sets) == [1, 2]
        cid = "0x" + cid_bytes.hex()
        if (wallet.lower(), adapter.lower()) not in self.approved:
            # CTF.safeBatchTransferFrom(msg.sender, adapter) without the grant
            raise pm_errors.TransactionFailedError(
                "ERC1155: need operator approval for 3rd party transfers")
        den = self.denominators.get(cid, 0)
        if den == 0:
            raise pm_errors.TransactionFailedError("result for condition not received yet")
        market = self.markets[cid]
        right = (CONTRACTS.neg_risk_collateral_adapter if market.state.neg_risk
                 else CONTRACTS.collateral_adapter)
        if adapter.lower() != right.lower():
            return    # wrong collateral: other position ids, nothing pulled
        payout = 0
        for index, token in enumerate((market.outcomes.yes.token_id,
                                       market.outcomes.no.token_id)):
            stake = self.balances.pop((wallet.lower(), token), 0)
            payout += stake * self.numerators.get((cid, index), 0) // den
        self.pusd[wallet.lower()] = self.pusd.get(wallet.lower(), 0) + payout


class FakeHandle:
    def __init__(self, client, calls):
        self.client, self.calls = client, calls

    async def wait(self):
        client = self.client
        client.shared.in_flight += 1
        client.shared.peak.append(client.shared.in_flight)
        try:
            if client.wait_delay:
                await asyncio.sleep(client.wait_delay)
            if client.fail_wait is not None:
                raise client.fail_wait
            if client.apply:
                client.chain.execute(client.wallet, self.calls)
            return SimpleNamespace(transaction_hash=TX_HASH, transaction_id="tx-1")
        finally:
            client.shared.in_flight -= 1


class FakeClient:
    """The slice of AsyncSecureClient the redeemer uses."""

    def __init__(self, wallet, chain, *, wallet_type="DEPOSIT_WALLET", shared=None):
        self.wallet = to_checksum_address(wallet)
        self.wallet_type = wallet_type
        self.environment = PRODUCTION
        self.chain = chain
        self.batches: list[tuple[list, str]] = []
        self.market_queries: list[dict] = []
        self.fail_submit: Exception | None = None
        self.fail_wait: Exception | None = None
        self.apply = True
        self.wait_delay = 0.0
        self.shared = shared if shared is not None else SimpleNamespace(in_flight=0, peak=[])

    def list_markets(self, *, condition_ids, closed, page_size):
        self.market_queries.append({"condition_ids": list(condition_ids),
                                    "closed": closed, "page_size": page_size})
        items = tuple(self.chain.markets[c] for c in condition_ids if c in self.chain.markets)

        async def first_page():
            return SimpleNamespace(items=items, has_more=False)

        return SimpleNamespace(first_page=first_page)

    async def execute_transaction(self, *, calls, metadata=None):
        self.batches.append((list(calls), metadata))
        if self.fail_submit is not None:
            raise self.fail_submit
        return FakeHandle(self, list(calls))


class FakePM:
    def __init__(self):
        self.positions: dict[str, list[Position]] = {}
        self.reads: list[str] = []

    async def get_all_positions(self, wallet, *, size_threshold=1.0, page_size=500,
                                max_pages=6):
        self.reads.append(wallet)
        return list(self.positions.get(wallet.lower(), [])), True


class RedeemDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.add_user(USER)
        self.chain = FakeChain()
        self.pm = FakePM()
        self.clients: dict[str, FakeClient] = {}
        self.client_kwargs: dict = {}
        self.now = 1_000.0

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def add_user(self, user_id):
        await self.db.execute(
            "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
            (user_id, "encrypted", now_iso()))

    async def client_for(self, user):
        uid = user["id"]
        if uid not in self.clients:
            self.clients[uid] = FakeClient(uid, self.chain, **self.client_kwargs)
        return self.clients[uid]

    def redeemer(self, **kwargs) -> redeem.Redeemer:
        return redeem.Redeemer(self.db, self.pm, self.client_for, self.chain,
                               clock=lambda: self.now, **kwargs)

    def resolved(self, cid=COND, yes=YES, no=NO, *, winner=0, held=(TEN, 0),
                 neg_risk=False, wallet=USER):
        """A market resolved on-chain, with the wallet's holdings both on-chain
        and as the data-api reports them."""
        self.chain.markets[cid] = gamma_market(cid, yes, no, neg_risk=neg_risk)
        self.chain.denominators[cid] = 1
        self.chain.numerators[(cid, 0)] = int(winner == 0)
        self.chain.numerators[(cid, 1)] = int(winner == 1)
        rows = self.pm.positions.setdefault(wallet.lower(), [])
        for index, (token, other, amount) in enumerate(((yes, no, held[0]),
                                                       (no, yes, held[1]))):
            if not amount:
                continue
            self.chain.balances[(wallet.lower(), token)] = amount
            shares = amount / redeem.UNITS_PER_USD
            rows.append(position(cid, token, opposite=other, index=index, size=shares,
                                 value=shares if index == winner else 0.0,
                                 neg_risk=neg_risk, wallet=wallet))

    def batches(self, wallet=USER):
        client = self.clients.get(wallet)
        return client.batches if client else []

    async def ledger_row(self, status, *, token=YES, cid=COND, row_id="pos-1"):
        settled = status in ("closed", "resolved")
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,"
            "exit_price,realized_pnl,opened_at,closed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, USER, TRADER, cid, token, "Will it happen?", "YES", 10.0, 100.0,
             0.5, 5.0, status, 1.0 if settled else None, 5.0 if settled else None,
             now_iso(), now_iso() if settled else None))
        return await self.db.fetchone("SELECT * FROM copy_positions WHERE id=?", (row_id,))


def decoded(call):
    data = bytes.fromhex(call.data[2:])
    return data[:4], data[4:]


# ---------------------------------------------------------------------------
# Selection (pure)
# ---------------------------------------------------------------------------
class SelectionTests(unittest.TestCase):
    def select(self, positions, **kwargs):
        args = dict(blocked_tokens=set(), blocked_conditions=set(), cooling=set())
        args.update(kwargs)
        return redeem.select_candidates(positions, **args)

    def test_only_resolved_holdings_worth_something_are_candidates(self):
        candidates, fenced = self.select([
            position(COND, YES),                                       # resolved winner
            position(COND_B, YES_B, opposite=NO_B, redeemable=False),  # still trading
            position("0x" + "ef" * 32, "3001", opposite="3002", value=0.0),   # lost
            position("0x" + "12" * 32, "4001", opposite="4002", size=0.005,
                     value=0.005),                                     # dust
            position("condition-1", "5001"),                           # not a condition id
        ])
        self.assertEqual([c.condition_id for c in candidates], [COND])
        self.assertEqual(candidates[0].token_ids, (YES, NO))
        self.assertEqual(fenced, 0)

    def test_both_sides_of_one_market_make_one_candidate(self):
        candidates, _ = self.select([
            position(COND, YES, opposite=NO, index=0, value=10.0),
            position(COND, NO, opposite=YES, index=1, value=0.0),
        ])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].token_ids, (YES, NO))
        self.assertEqual(candidates[0].value_usd, 10.0)

    def test_ledger_fences_count_and_cooldowns_drop(self):
        positions = [position(COND, YES), position(COND_B, YES_B, opposite=NO_B)]
        for kwargs in ({"blocked_tokens": {YES}}, {"blocked_tokens": {NO}},
                       {"blocked_conditions": {COND}}):
            candidates, fenced = self.select(positions, **kwargs)
            self.assertEqual([c.condition_id for c in candidates], [COND_B], kwargs)
            self.assertEqual(fenced, 1, kwargs)
        candidates, fenced = self.select(positions, cooling={COND})
        self.assertEqual([c.condition_id for c in candidates], [COND_B])
        self.assertEqual(fenced, 0)

    def test_contradicting_neg_risk_flags_route_nothing(self):
        candidates, _ = self.select([
            position(COND, YES, index=0, neg_risk=False),
            position(COND, NO, opposite=YES, index=1, neg_risk=True),
        ])
        self.assertEqual(candidates, [])

    def test_largest_first_and_capped_per_transaction(self):
        positions = [position("0x" + f"{i:02x}" * 32, str(9000 + 2 * i),
                              opposite=str(9001 + 2 * i), size=float(i), value=float(i))
                     for i in range(1, 6)]
        candidates, _ = self.select(positions, limit=3)
        self.assertEqual([c.value_usd for c in candidates], [5.0, 4.0, 3.0])


# ---------------------------------------------------------------------------
# The job, end to end against fakes
# ---------------------------------------------------------------------------
class RedeemTests(RedeemDb):
    async def test_a_winning_position_is_redeemed_with_its_adapter_grant(self):
        self.resolved()
        with self.assertLogs("redeem", level="INFO") as logs:
            summary = await self.redeemer().run_pass()

        [(calls, metadata)] = self.batches()
        self.assertEqual(len(calls), 2)
        selector, args = decoded(calls[0])
        self.assertEqual(selector, SET_APPROVAL)
        self.assertEqual(calls[0].to.lower(), CONTRACTS.ctf.lower())
        operator, approved = decode(["address", "bool"], args)
        self.assertEqual(operator.lower(), CONTRACTS.collateral_adapter.lower())
        self.assertTrue(approved)
        selector, args = decoded(calls[1])
        self.assertEqual(selector, REDEEM_POSITIONS)
        self.assertEqual(calls[1].to.lower(), CONTRACTS.collateral_adapter.lower())
        collateral, parent, cid, index_sets = decode(
            ["address", "bytes32", "bytes32", "uint256[]"], args)
        self.assertEqual(collateral.lower(), CONTRACTS.collateral.lower())
        self.assertEqual(parent, b"\x00" * 32)
        self.assertEqual("0x" + cid.hex(), COND)
        self.assertEqual(list(index_sets), [1, 2])
        self.assertIn("Redeem 1 resolved position", metadata)

        self.assertEqual(self.chain.pusd[USER.lower()], TEN)
        self.assertNotIn((USER.lower(), YES), self.chain.balances)
        self.assertEqual((summary.redeemed, summary.payout_units, summary.failed),
                         (1, TEN, 0))
        self.assertTrue(any("REDEEMED" in line and TX_HASH in line for line in logs.output))

    async def test_a_neg_risk_market_redeems_through_the_neg_risk_adapter(self):
        self.resolved(neg_risk=True)
        summary = await self.redeemer().run_pass()
        [(calls, _)] = self.batches()
        operator, _ = decode(["address", "bool"], decoded(calls[0])[1])
        self.assertEqual(operator.lower(), CONTRACTS.neg_risk_collateral_adapter.lower())
        self.assertEqual(calls[1].to.lower(), CONTRACTS.neg_risk_collateral_adapter.lower())
        self.assertEqual(summary.redeemed, 1)
        self.assertEqual(self.chain.pusd[USER.lower()], TEN)

    async def test_an_existing_grant_is_not_requested_again(self):
        self.chain.approved.add((USER.lower(), CONTRACTS.collateral_adapter.lower()))
        self.resolved()
        await self.redeemer().run_pass()
        [(calls, _)] = self.batches()
        self.assertEqual([decoded(c)[0] for c in calls], [REDEEM_POSITIONS])

    async def test_several_markets_share_one_transaction(self):
        self.resolved()
        self.resolved(COND_B, YES_B, NO_B, winner=1, held=(0, 2 * TEN))
        summary = await self.redeemer().run_pass()
        [(calls, metadata)] = self.batches()
        self.assertEqual([decoded(c)[0] for c in calls],
                         [SET_APPROVAL, REDEEM_POSITIONS, REDEEM_POSITIONS])
        self.assertIn("Redeem 2 resolved position", metadata)
        self.assertEqual((summary.redeemed, summary.payout_units), (2, 3 * TEN))

    async def test_a_market_unresolved_on_chain_is_never_touched(self):
        """The data-api flag is a hint; only payoutDenominator > 0 is proof."""
        self.resolved()
        self.chain.denominators[COND] = 0
        summary = await self.redeemer().run_pass()
        self.assertEqual(self.batches(), [])
        self.assertEqual((summary.redeemed, summary.skipped), (0, 1))
        self.assertIn((USER.lower(), YES), self.chain.balances)

    async def test_tokens_that_lost_on_chain_are_not_redeemed(self):
        """A stale data-api value must not buy a transaction for a $0 payout."""
        self.resolved()
        self.chain.numerators[(COND, 0)] = 0
        self.chain.numerators[(COND, 1)] = 1
        summary = await self.redeemer().run_pass()
        self.assertEqual(self.batches(), [])
        self.assertEqual(summary.skipped, 1)

    async def test_markets_the_copy_ledger_still_manages_are_left_alone(self):
        for status in ("open", "closing", "reconciliation_required"):
            with self.subTest(status=status):
                await self.db.execute("DELETE FROM copy_positions")
                self.pm.positions.clear()
                self.resolved()
                await self.ledger_row(status, token=NO)   # either side fences
                summary = await self.redeemer().run_pass()
                self.assertEqual(self.batches(), [])
                self.assertEqual(summary.fenced, 1)

    async def test_a_buy_claim_on_the_token_fences_the_market(self):
        self.resolved()
        await self.db.execute(
            "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,"
            "state,reserved_usd,risk_revision,claimed_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (USER, YES, TRADER, "c1", "open", "uncertain", 5.0, 0, now_iso(), now_iso()))
        summary = await self.redeemer().run_pass()
        self.assertEqual(self.batches(), [])
        self.assertEqual(summary.fenced, 1)

    async def test_a_row_fenced_by_condition_alone_still_fences(self):
        """A row can name the market without a token the data-api shows."""
        self.resolved()
        await self.ledger_row("open", token="9999", cid=COND)
        summary = await self.redeemer().run_pass()
        self.assertEqual(self.batches(), [])
        self.assertEqual(summary.fenced, 1)

    async def test_settled_ledger_rows_are_redeemed_and_never_written(self):
        self.resolved()
        resolved_row = await self.ledger_row("resolved")
        closed_row = await self.ledger_row("closed", token=NO, row_id="pos-2")
        summary = await self.redeemer().run_pass()
        self.assertEqual(summary.redeemed, 1)
        self.assertEqual(
            await self.db.fetchone("SELECT * FROM copy_positions WHERE id='pos-1'"),
            resolved_row)
        self.assertEqual(
            await self.db.fetchone("SELECT * FROM copy_positions WHERE id='pos-2'"),
            closed_row)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM trade_events"), 0)

    async def test_a_second_pass_finds_nothing_to_do(self):
        """Idempotent through the chain: the data-api still lists the redeemed
        position (indexer lag), but the wallet holds nothing on-chain."""
        self.resolved()
        redeemer = self.redeemer()
        first = await redeemer.run_pass()
        second = await redeemer.run_pass()
        self.assertEqual(first.redeemed, 1)
        self.assertEqual(len(self.batches()), 1)
        self.assertEqual((second.redeemed, second.skipped), (0, 1))
        self.assertEqual(self.chain.pusd[USER.lower()], TEN)

    async def test_a_failed_attempt_cools_down_before_it_is_retried(self):
        self.resolved()
        redeemer = self.redeemer(retry_seconds=600)
        client = await self.client_for({"id": USER})
        client.fail_wait = pm_errors.TransactionFailedError("execution reverted")
        first = await redeemer.run_pass()
        self.assertEqual((first.failed, first.redeemed), (1, 0))
        self.assertEqual(len(client.batches), 1)

        client.fail_wait = None
        self.now += 599
        await redeemer.run_pass()
        self.assertEqual(len(client.batches), 1)          # still cooling

        self.now += 2
        retried = await redeemer.run_pass()
        self.assertEqual(len(client.batches), 2)
        self.assertEqual(retried.redeemed, 1)

    async def test_an_unconfirmed_wait_is_treated_like_a_failure(self):
        self.resolved()
        redeemer = self.redeemer()
        client = await self.client_for({"id": USER})
        client.fail_wait = pm_errors.TimeoutError("still pending after 100 polls")
        summary = await redeemer.run_pass()
        self.assertEqual(summary.failed, 1)
        await redeemer.run_pass()
        self.assertEqual(len(client.batches), 1)          # not resubmitted at once

    async def test_a_policy_rejection_halts_every_later_submission(self):
        await self.add_user(OTHER)
        self.resolved()
        self.resolved(COND_B, YES_B, NO_B, wallet=OTHER)
        redeemer = self.redeemer()
        for wallet in (USER, OTHER):
            client = await self.client_for({"id": wallet})
            client.fail_submit = pm_errors.RequestRejectedError(
                "operator is not in the allowed list", status=400)
        with self.assertLogs("redeem", level="ERROR") as logs:
            summary = await redeemer.run_pass(concurrency=1)
        self.assertIsNotNone(redeemer.halted)
        self.assertIn("allowed list", summary.halted)
        self.assertEqual(len(self.batches(USER)) + len(self.batches(OTHER)), 1)
        self.assertTrue(any("REDEEM HALTED" in line for line in logs.output))

        reads = len(self.pm.reads)
        later = await redeemer.run_pass()
        self.assertEqual(len(self.pm.reads), reads)        # nothing even read
        self.assertIsNotNone(later.halted)

    async def test_an_sdk_refusal_halts(self):
        self.resolved()
        redeemer = self.redeemer()
        client = await self.client_for({"id": USER})
        client.fail_submit = pm_errors.UserInputError(
            "Gasless transactions require a Builder API Key or Relayer API Key.")
        await redeemer.run_pass()
        self.assertIn("Builder API Key", redeemer.halted)

    async def test_a_busy_wallet_only_cools_down(self):
        self.resolved()
        redeemer = self.redeemer()
        client = await self.client_for({"id": USER})
        client.fail_submit = pm_errors.RequestRejectedError(
            "wallet busy: active action in progress", status=400)
        summary = await redeemer.run_pass()
        self.assertIsNone(redeemer.halted)
        self.assertEqual(summary.failed, 1)
        await redeemer.run_pass()
        self.assertEqual(len(client.batches), 1)

    async def test_an_eoa_wallet_is_skipped(self):
        """An EOA would broadcast the batch itself and needs gas."""
        self.resolved()
        self.client_kwargs = {"wallet_type": "EOA"}
        summary = await self.redeemer().run_pass()
        self.assertEqual(self.batches(), [])
        self.assertEqual(summary.skipped, 1)

    async def test_a_client_bound_to_another_wallet_is_refused(self):
        self.resolved()
        self.clients[USER] = FakeClient(OTHER, self.chain)
        summary = await self.redeemer().run_pass()
        self.assertEqual(self.clients[USER].batches, [])
        self.assertEqual(summary.skipped, 1)

    async def test_routing_needs_gamma_to_agree(self):
        cases = {
            "not closed in Gamma": lambda: self.chain.markets.pop(COND),
            "negRisk disagrees": lambda: setattr(
                self.chain.markets[COND].state, "neg_risk", True),
            "negRisk unknown": lambda: setattr(
                self.chain.markets[COND].state, "neg_risk", None),
            "different tokens": lambda: setattr(
                self.chain.markets[COND].outcomes.yes, "token_id", "7777"),
        }
        for name, spoil in cases.items():
            with self.subTest(name):
                self.pm.positions.clear()
                self.clients.clear()
                self.resolved()
                spoil()
                summary = await self.redeemer().run_pass()
                self.assertEqual(self.batches(), [])
                self.assertEqual(summary.skipped, 1)

    async def test_gamma_is_asked_for_closed_markets_only(self):
        self.resolved()
        await self.redeemer().run_pass()
        [query] = self.clients[USER].market_queries
        self.assertEqual(query, {"condition_ids": [COND], "closed": True, "page_size": 1})

    async def test_confirmed_but_still_held_is_rechecked_later_not_resubmitted(self):
        """An RPC node behind the relayer's confirmation must not cause a
        second transaction straight away."""
        self.resolved()
        redeemer = self.redeemer()
        client = await self.client_for({"id": USER})
        client.apply = False
        summary = await redeemer.run_pass()
        self.assertEqual((summary.redeemed, summary.skipped), (0, 1))
        await redeemer.run_pass()
        self.assertEqual(len(client.batches), 1)

    async def test_users_run_with_bounded_concurrency(self):
        shared = SimpleNamespace(in_flight=0, peak=[])
        self.client_kwargs = {"shared": shared}
        wallets = [USER] + ["0x" + f"{i}" * 40 for i in range(4, 9)]
        for wallet in wallets[1:]:
            await self.add_user(wallet)
        for i, wallet in enumerate(wallets):
            self.resolved("0x" + f"{i + 1:02x}" * 32, str(5000 + 2 * i),
                          str(5001 + 2 * i), wallet=wallet)
        for wallet in wallets:
            client = await self.client_for({"id": wallet})
            client.wait_delay = 0.05
        summary = await self.redeemer().run_pass(concurrency=2)
        self.assertEqual(summary.redeemed, len(wallets))
        self.assertEqual(max(shared.peak), 2)

    async def test_one_users_failure_does_not_stop_the_others(self):
        await self.add_user(OTHER)
        self.resolved()
        self.resolved(COND_B, YES_B, NO_B, wallet=OTHER)
        self.clients[USER] = FakeClient(USER, self.chain)

        def broken(**_kwargs):
            raise RuntimeError("market lookup broke")

        self.clients[USER].list_markets = broken
        with self.assertLogs("redeem", level="ERROR"):
            summary = await self.redeemer().run_pass(concurrency=1)
        self.assertEqual((summary.failed, summary.redeemed), (1, 1))
        self.assertEqual(self.chain.pusd[OTHER.lower()], TEN)

    async def test_a_stop_request_starts_no_further_users(self):
        await self.add_user(OTHER)
        self.resolved()
        self.resolved(COND_B, YES_B, NO_B, wallet=OTHER)
        stop = asyncio.Event()
        stop.set()
        summary = await self.redeemer().run_pass(stop=stop)
        self.assertEqual((summary.users, summary.redeemed), (0, 0))
        self.assertEqual(self.batches(USER) + self.batches(OTHER), [])

    async def test_nothing_redeemable_builds_no_client(self):
        self.pm.positions[USER.lower()] = [position(redeemable=False)]
        summary = await self.redeemer().run_pass()
        self.assertEqual(self.clients, {})
        self.assertEqual(summary.users, 1)


# ---------------------------------------------------------------------------
# On-chain reads
# ---------------------------------------------------------------------------
class ChainReaderTests(unittest.IsolatedAsyncioTestCase):
    RPC = "https://polygon.example/v2/secret-key"

    def reader(self, handler):
        transport = httpx.MockTransport(handler)
        return redeem.ChainReader(self.RPC, client=httpx.AsyncClient(transport=transport))

    @staticmethod
    def word(value: int) -> str:
        return "0x" + encode(["uint256"], [value]).hex()

    async def test_the_four_ctf_views_are_encoded_and_decoded(self):
        seen = []
        answers = {
            keccak(text="payoutDenominator(bytes32)")[:4]: 2,
            keccak(text="payoutNumerators(bytes32,uint256)")[:4]: 1,
            keccak(text="balanceOf(address,uint256)")[:4]: 42,
            keccak(text="isApprovedForAll(address,address)")[:4]: 1,
        }

        def handler(request):
            body = json.loads(request.content)
            call = body["params"][0]
            seen.append(call)
            selector = bytes.fromhex(call["data"][2:])[:4]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": self.word(answers[selector])})

        chain = self.reader(handler)
        try:
            self.assertEqual(await chain.payout_denominator(CONTRACTS.ctf, COND), 2)
            self.assertEqual(await chain.payout_numerator(CONTRACTS.ctf, COND, 1), 1)
            self.assertEqual(await chain.balance_of(CONTRACTS.ctf, USER, YES), 42)
            self.assertTrue(await chain.is_approved_for_all(
                CONTRACTS.ctf, USER, CONTRACTS.collateral_adapter))
        finally:
            await chain.aclose()

        self.assertTrue(all(call["to"] == CONTRACTS.ctf for call in seen))
        self.assertEqual(decode(["bytes32"], bytes.fromhex(seen[0]["data"][10:])),
                         (bytes.fromhex(COND[2:]),))
        self.assertEqual(decode(["bytes32", "uint256"], bytes.fromhex(seen[1]["data"][10:])),
                         (bytes.fromhex(COND[2:]), 1))
        owner, token = decode(["address", "uint256"], bytes.fromhex(seen[2]["data"][10:]))
        self.assertEqual((owner.lower(), token), (USER.lower(), int(YES)))
        owner, operator = decode(["address", "address"], bytes.fromhex(seen[3]["data"][10:]))
        self.assertEqual((owner.lower(), operator.lower()),
                         (USER.lower(), CONTRACTS.collateral_adapter.lower()))

    async def test_errors_never_carry_the_rpc_url(self):
        def unauthorized(request):
            return httpx.Response(401, json={"error": "bad key"})

        chain = self.reader(unauthorized)
        try:
            with self.assertRaises(redeem.ChainReadError) as ctx:
                await chain.payout_denominator(CONTRACTS.ctf, COND)
        finally:
            await chain.aclose()
        self.assertNotIn("secret-key", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    async def test_an_empty_or_failed_call_is_an_error_not_a_zero(self):
        """'0x' is what a call to an address without code returns; reading it
        as 0 would turn a misconfigured contract into 'not resolved'."""
        for reply in ({"result": "0x"},
                      {"error": {"code": -32000, "message": "execution reverted"}}):
            with self.subTest(reply=reply):
                chain = self.reader(lambda request, reply=reply: httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, **reply}))
                try:
                    with self.assertRaises(redeem.ChainReadError):
                        await chain.payout_denominator(CONTRACTS.ctf, COND)
                finally:
                    await chain.aclose()

    async def test_a_malformed_condition_id_is_refused_before_any_call(self):
        def unreachable(request):
            raise AssertionError("no request expected")

        chain = self.reader(unreachable)
        try:
            with self.assertRaises(ValueError):
                await chain.payout_denominator(CONTRACTS.ctf, "condition-1")
        finally:
            await chain.aclose()


class ContractTests(unittest.TestCase):
    def test_the_redeem_contracts_are_the_sdks_own_and_reviewed(self):
        """Read from the SDK, so they move with it; pinned here, so an SDK
        upgrade that moves them is reviewed rather than silently followed.
        Verified on Polygonscan 2026-10-18: neither adapter is a proxy."""
        from polymarket._internal.environment import get_environment_config
        cfg = get_environment_config(PRODUCTION)
        self.assertEqual(CONTRACTS, redeem.Contracts(
            ctf=cfg.conditional_tokens, collateral=cfg.collateral_token,
            collateral_adapter=cfg.collateral_adapter,
            neg_risk_collateral_adapter=cfg.neg_risk_collateral_adapter))
        self.assertEqual(CONTRACTS.ctf, "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        self.assertEqual(CONTRACTS.collateral, "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
        self.assertEqual(CONTRACTS.collateral_adapter,
                         "0xAdA100Db00Ca00073811820692005400218FcE1f")
        self.assertEqual(CONTRACTS.neg_risk_collateral_adapter,
                         "0xadA2005600Dec949baf300f4C6120000bDB6eAab")

    def test_rejections_are_classified(self):
        def rejected(message, status=400):
            return pm_errors.RequestRejectedError(message, status=status)

        self.assertTrue(redeem._is_policy_rejection(
            rejected("operator is not in the allowed list")))
        self.assertTrue(redeem._is_policy_rejection(rejected("invalid key", status=401)))
        self.assertFalse(redeem._is_policy_rejection(
            rejected("wallet busy: active action in progress")))
        self.assertFalse(redeem._is_policy_rejection(
            rejected("batch nonce 3 does not match on-chain nonce 4")))
        self.assertFalse(redeem._is_policy_rejection(rejected("deadline expired")))


# ---------------------------------------------------------------------------
# Wiring in main.py
# ---------------------------------------------------------------------------
class LoopWiringTests(unittest.TestCase):
    def boot(self):
        from backend import main
        with TestClient(main.app):
            pass

    def test_the_loop_is_off_by_default(self):
        from backend import main
        with patch.object(main, "_redeem_loop", new=AsyncMock()) as loop, \
                patch.dict(os.environ, {"REDEEM_AUTOSTART": "0"}):
            self.boot()
        loop.assert_not_called()

    def test_enabled_without_builder_credentials_it_stays_off(self):
        from backend import main
        with patch.object(main, "_redeem_loop", new=AsyncMock()) as loop, \
                patch.object(main, "_gasless_configured", return_value=False), \
                patch.dict(os.environ, {"REDEEM_AUTOSTART": "1"}):
            with self.assertLogs("main", level="WARNING") as logs:
                self.boot()
        loop.assert_not_called()
        self.assertTrue(any("REDEEM_AUTOSTART=1" in line for line in logs.output))

    def test_enabled_with_credentials_it_starts(self):
        from backend import main
        with patch.object(main, "_redeem_loop", new=AsyncMock()) as loop, \
                patch.object(main, "_gasless_configured", return_value=True), \
                patch.dict(os.environ, {"REDEEM_AUTOSTART": "1"}):
            self.boot()
        loop.assert_called_once()


class LoopHeartbeatTests(RedeemDb):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.passes = 0
        read = self.pm.get_all_positions

        async def counting(wallet, **kwargs):
            self.passes += 1
            return await read(wallet, **kwargs)

        self.pm.get_all_positions = counting

    async def run_loop_once(self, redeemer_cls=None):
        from backend import main
        hb = Heartbeats()
        app = SimpleNamespace(state=SimpleNamespace(db=self.db, pm=self.pm, clients={}))
        stop = asyncio.Event()
        closed = []

        class Chain:
            def __init__(self, url):
                self.url = url

            async def aclose(self):
                closed.append(True)

        patches = [patch.object(main, "heartbeats", hb),
                   patch.object(redeem, "ChainReader", Chain),
                   patch.dict(os.environ, {"REDEEM_SECONDS": "3600"})]
        if redeemer_cls is not None:
            patches.append(patch.object(redeem, "Redeemer", redeemer_cls))
        for p in patches:
            p.start()
        try:
            task = asyncio.create_task(main._redeem_loop(app, stop))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if self.passes:
                    break
            await asyncio.sleep(0.01)
            stop.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            for p in reversed(patches):
                p.stop()
        return hb.snapshot()["redeem"], closed

    async def test_a_completed_pass_marks_the_heartbeat(self):
        snap, closed = await self.run_loop_once()
        self.assertTrue(snap["registered"])
        self.assertEqual(snap["successes"], 1)
        self.assertEqual(snap["interval_seconds"], 3600)
        self.assertEqual(closed, [True])

    async def test_a_halted_redeemer_stops_the_heartbeat(self):
        test = self

        class Halted:
            def __init__(self, *args, **kwargs):
                self.halted = None

            async def run_pass(self, **kwargs):
                test.passes += 1
                self.halted = "relayer refused the redeem batch (400: not allowed)"
                return redeem.PassSummary(halted=self.halted)

        snap, closed = await self.run_loop_once(Halted)
        self.assertTrue(snap["registered"])
        self.assertEqual(snap["successes"], 0)
        self.assertTrue(snap["stale"])
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
