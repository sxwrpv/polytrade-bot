"""Fill-or-kill: an intent that cannot fill must be abandoned, not retried forever.

plan_actions is stateless — it re-derives the same intent from live positions
every tick — so before this, an order needing >2% over mark on a ~10%-spread
book was re-attempted every 5s indefinitely, spending a geoblock probe and an
orderbook read each time against an API that already returns 429s.
"""
from __future__ import annotations

import pathlib
import re
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from backend.core.copy_engine import MAX_FILL_ATTEMPTS, Action, CopyEngine
from backend.core.execution import OrderResult, PreflightError


def _engine() -> CopyEngine:
    return CopyEngine(db=SimpleNamespace(), pm=SimpleNamespace())


def _resize(shares=20000.0, amount=5.0) -> Action:
    return Action(kind="resize", subkind="increase", token_id="tok", side="BUY",
                  amount=amount, trader_shares=shares)


def _open() -> Action:
    return Action(kind="open", token_id="tok", side="BUY", amount=8.0, trader_shares=2000.0)


RISK = {"slippage": 2.0, "min_price": 0.1, "max_price": 0.98}


class FillOrKillBudgetTests(unittest.TestCase):
    def test_intent_is_abandoned_after_the_attempt_budget(self):
        eng, act = _engine(), _resize()
        for i in range(MAX_FILL_ATTEMPTS):
            self.assertFalse(eng._fill_budget_exhausted("u", act),
                             f"killed early on attempt {i + 1}")
            eng._record_fill_outcome("u", act, filled=False, reason="slippage_exceeded")
        self.assertTrue(eng._fill_budget_exhausted("u", act))

    def test_a_fill_clears_the_budget(self):
        eng, act = _engine(), _resize()
        eng._record_fill_outcome("u", act, filled=False, reason="slippage_exceeded")
        eng._record_fill_outcome("u", act, filled=True)
        for _ in range(MAX_FILL_ATTEMPTS):
            self.assertFalse(eng._fill_budget_exhausted("u", act))
            eng._record_fill_outcome("u", act, filled=False, reason="x")
        self.assertTrue(eng._fill_budget_exhausted("u", act))

    def test_budget_rearms_when_the_leader_moves_again(self):
        """A wide spread must not freeze the position permanently."""
        eng, act = _engine(), _resize(shares=20000.0)
        for _ in range(MAX_FILL_ATTEMPTS):
            eng._record_fill_outcome("u", act, filled=False, reason="slippage_exceeded")
        self.assertTrue(eng._fill_budget_exhausted("u", act))
        moved = _resize(shares=41000.0)          # leader resized again -> new intent
        self.assertFalse(eng._fill_budget_exhausted("u", moved))

    def test_clamped_amount_does_not_rearm_the_budget(self):
        """The regression that would silently defeat the kill.

        _prepare_buy/_clamp_to_verified_position rewrite action.amount from live
        collateral and MAX/TRADE headroom, so it drifts between ticks. If the
        fingerprint included it, every attempt would look like a new intent and
        the loop would continue forever.
        """
        eng = _engine()
        for amount in (5.0, 4.31, 5.92):         # same leader size, clamped differently
            act = _resize(shares=20000.0, amount=amount)
            self.assertFalse(eng._fill_budget_exhausted("u", act))
            eng._record_fill_outcome("u", act, filled=False, reason="slippage_exceeded")
        self.assertTrue(eng._fill_budget_exhausted("u", _resize(shares=20000.0, amount=6.5)))

    def test_budgets_are_independent_per_token_and_user(self):
        eng = _engine()
        a = _resize()
        b = Action(kind="resize", subkind="increase", token_id="other", side="BUY",
                   amount=5.0, trader_shares=20000.0)
        for _ in range(MAX_FILL_ATTEMPTS):
            eng._record_fill_outcome("u", a, filled=False, reason="x")
        self.assertTrue(eng._fill_budget_exhausted("u", a))
        self.assertFalse(eng._fill_budget_exhausted("u", b),   # different token
                         "one stuck token must not block others")
        self.assertFalse(eng._fill_budget_exhausted("other-user", a))

    def test_abandoning_an_exit_is_escalated(self):
        """Abandoning an ENTRY loses an opportunity; abandoning an EXIT means
        still holding a position the leader already left."""
        eng = _engine()
        exit_action = Action(kind="close", token_id="tok", side="SELL", amount=10.0)
        with self.assertLogs("copy_engine", level="WARNING") as logs:
            for _ in range(MAX_FILL_ATTEMPTS):
                eng._record_fill_outcome("u", exit_action, filled=False,
                                         reason="slippage_exceeded")
        joined = " ".join(logs.output)
        self.assertIn("STILL HOLDING", joined)


class ExchangeRefusalBudgetTests(unittest.IsolatedAsyncioTestCase):
    """A BUY the exchange refused outright (execution._terminal_rejection) is a
    non-fill like any other, so this budget is what brakes its retry. Before
    2026-10-19 such a refusal was uncertain, and its fence was the only brake.
    """

    def engine(self, outcome) -> CopyEngine:
        async def place_order(*args, **kwargs):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def prepare(user_id, action):
            return replace(action, claim_id="claim-1"), RISK

        async def clamp(user_id, action, risk):
            return action

        eng = CopyEngine(db=SimpleNamespace(), pm=SimpleNamespace(), place_order=place_order)
        eng._prepare_buy = prepare
        eng._clamp_to_verified_position = clamp
        eng._mark_claim_submitting = AsyncMock(return_value=True)
        eng._release_buy_claim = AsyncMock()
        eng._mark_claim_uncertain = AsyncMock()
        return eng

    async def test_a_refused_buy_spends_the_budget_and_releases_its_claim(self):
        eng = self.engine(OrderResult(ok=False, exchange_rejection="fok_not_filled"))
        for _ in range(MAX_FILL_ATTEMPTS):
            self.assertFalse(eng._fill_budget_exhausted("u", _open()))
            await eng._execute("u", object(), _open())
        self.assertTrue(eng._fill_budget_exhausted("u", _open()))
        self.assertEqual(MAX_FILL_ATTEMPTS, eng._release_buy_claim.await_count)
        eng._mark_claim_uncertain.assert_not_awaited()

    async def test_an_uncertain_outcome_still_does_not_spend_it(self):
        eng = self.engine(OrderResult(ok=False, reason="api_error: read timed out",
                                      submission_uncertain=True))
        for _ in range(MAX_FILL_ATTEMPTS + 1):
            await eng._execute("u", object(), _open())
        self.assertFalse(eng._fill_budget_exhausted("u", _open()))
        eng._release_buy_claim.assert_not_awaited()

    async def test_a_refusal_keeps_its_notional_and_an_unsent_order_does_not(self):
        """The refused order went on the wire, so its notional keeps counting
        against the cap for the TTL. An order that was never sent is forgotten,
        exactly as before: a local skip, or a PreflightError."""
        refused = self.engine(OrderResult(ok=False, exchange_rejection="not_enough_balance"))
        await refused._execute("u", object(), _open())
        self.assertEqual(8.0, refused._submitted_basis("u", "tok"))

        for outcome in (OrderResult(ok=False, reason="slippage_exceeded"),
                        PreflightError("orderbook", httpx.ReadTimeout("slow"))):
            with self.subTest(type(outcome).__name__):
                unsent = self.engine(outcome)
                await unsent._execute("u", object(), _open())
                self.assertEqual(0.0, unsent._submitted_basis("u", "tok"))
                unsent._release_buy_claim.assert_awaited_once()
                unsent._mark_claim_uncertain.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class SingleSourceOfTruthTests(unittest.TestCase):
    """Guards against the two definitions drifting apart again."""

    def test_exposure_uses_one_status_set_in_both_paths(self):
        """Same capital-at-risk definition in the reconciler and the fast path.

        They previously disagreed: the fast path counted
        open+closing+reconciliation_required while the reconciler counted only
        'open', so the same wallet reported two different exposures and
        MAX EXPOSURE could be exceeded via the reconciler.
        """
        src = pathlib.Path("backend/core/copy_engine.py").read_text()
        risk_sets = re.findall(
            r"SELECT \* FROM copy_positions WHERE user_id ?= ?\? ?\"?\s*\"?AND status ([^\"]+)", src)
        self.assertTrue(risk_sets, "could not locate the position-set queries")
        for got in risk_sets:
            self.assertIn("'closing'", got)
            self.assertIn("'reconciliation_required'", got)

    def test_minimum_size_is_per_wallet_everywhere(self):
        """The per-wallet slider is the ONLY minimum-size authority.

        Three separate sites gate BUY size. They previously disagreed: opens
        used the slider, resizes used a hardcoded $1, and a later fix clamped
        two of them with max(). A global constant silently overriding the
        slider is the same bug as the open/resize split it replaced.
        """
        src = pathlib.Path("backend/core/copy_engine.py").read_text()
        floors = re.findall(r"^\s*floor = (.+)$", src, re.M)
        self.assertGreaterEqual(len(floors), 3, "expected every BUY-size gate")
        for got in floors:
            self.assertEqual('risk["ignore_below"]', got.strip(),
                             "a size gate bypasses the per-wallet slider")
