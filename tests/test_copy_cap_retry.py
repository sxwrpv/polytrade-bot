"""The per-token cap must hold across a burst of retries.

Incident 2026-08-23: three BUYs of ~$15.54 went out for one token in 13
seconds against a $15 cap. Each was reported failed, so the claim was released
and no row was written; the retry budget then spent itself and every attempt
sized itself fresh, because the two guards that should have stopped it were
both blind — `row_basis` is 0 until a position row exists, and `wallet_cost`
comes from an indexer that lags a fill by seconds. All three actually filled.

These tests pin the behaviour that makes that impossible, and the assumption
whose failure caused it.
"""
from __future__ import annotations

import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from backend.core import copy_engine as ce


class _Engine(ce.CopyEngine):
    """Bare engine: only the basis bookkeeping is under test, so nothing else
    is constructed."""
    def __init__(self):
        self._submitted = {}


def _action(token="tok", amount=15.0):
    return ce.Action(kind="open", token_id=token, side="BUY",
                     amount=amount, notional_usd=amount)


class SubmittedBasisTests(unittest.TestCase):
    def setUp(self):
        self.e = _Engine()

    def test_a_submitted_notional_counts_immediately(self):
        """The whole point: it is true the instant the order goes out, before
        any indexer can confirm it."""
        self.assertEqual(0.0, self.e._submitted_basis("u", "tok"))
        self.e._note_submitted("u", "tok", 15.0)
        self.assertEqual(15.0, self.e._submitted_basis("u", "tok"))

    def test_repeated_submissions_accumulate(self):
        """Three attempts of $15 must read as $45, not as $15 three times."""
        for _ in range(3):
            self.e._note_submitted("u", "tok", 15.0)
        self.assertEqual(45.0, self.e._submitted_basis("u", "tok"))

    def test_it_is_scoped_per_token_and_per_user(self):
        self.e._note_submitted("u", "tok", 15.0)
        self.assertEqual(0.0, self.e._submitted_basis("u", "other"))
        self.assertEqual(0.0, self.e._submitted_basis("other", "tok"))

    def test_it_expires_so_a_stuck_entry_cannot_block_forever(self):
        self.e._note_submitted("u", "tok", 15.0)
        self.e._submitted[("u", "tok")][1] = time.monotonic() - 1
        self.assertEqual(0.0, self.e._submitted_basis("u", "tok"))
        self.assertNotIn(("u", "tok"), self.e._submitted, "expired entry not reaped")

    def test_expiry_resets_rather_than_accumulating_across_windows(self):
        self.e._note_submitted("u", "tok", 15.0)
        self.e._submitted[("u", "tok")][1] = time.monotonic() - 1
        self.e._note_submitted("u", "tok", 15.0)
        self.assertEqual(15.0, self.e._submitted_basis("u", "tok"))

    def test_clearing_retires_it(self):
        self.e._note_submitted("u", "tok", 15.0)
        self.e._clear_submitted("u", "tok")
        self.assertEqual(0.0, self.e._submitted_basis("u", "tok"))


class ClampTests(unittest.IsolatedAsyncioTestCase):
    """The gate itself, with the wallet read stubbed to whatever the indexer
    would have said at that moment."""

    def _engine(self, wallet_cost):
        e = _Engine()
        e._wallet_position = AsyncMock(return_value=(wallet_cost, 0.0))
        return e

    RISK = {"max_per_trade": 15.0, "ignore_below": 1.0}

    async def test_the_second_attempt_of_a_burst_is_refused(self):
        """The exact incident: attempt one is reported failed and the indexer
        still shows nothing, so attempt two must be stopped by memory alone."""
        e = self._engine(wallet_cost=0.0)
        first = await e._clamp_to_verified_position("u", _action(), self.RISK)
        self.assertIsNotNone(first)
        self.assertAlmostEqual(15.0, first.amount)

        e._note_submitted("u", "tok", first.amount)          # it went on the wire
        second = await e._clamp_to_verified_position("u", _action(), self.RISK)
        self.assertIsNone(second, "a retry sized itself fresh — the cap is breachable")

    async def test_three_attempts_can_never_total_more_than_the_cap(self):
        """Property form of the same thing, over the whole retry budget."""
        e = self._engine(wallet_cost=0.0)
        total = 0.0
        for _ in range(ce.MAX_FILL_ATTEMPTS):
            clamped = await e._clamp_to_verified_position("u", _action(), self.RISK)
            if clamped is None:
                break
            total += clamped.amount
            e._note_submitted("u", "tok", clamped.amount)
        self.assertLessEqual(total, self.RISK["max_per_trade"] + 0.005,
                             f"retries totalled {total:.2f} against a 15.00 cap")

    async def test_a_partial_first_attempt_still_leaves_its_own_headroom(self):
        """Memory must clamp, not block outright: $6 submitted leaves $9."""
        e = self._engine(wallet_cost=0.0)
        e._note_submitted("u", "tok", 6.0)
        clamped = await e._clamp_to_verified_position("u", _action(amount=15.0), self.RISK)
        self.assertIsNotNone(clamped)
        self.assertAlmostEqual(9.0, clamped.amount, places=2)

    async def test_the_wallet_wins_once_it_can_see_the_shares(self):
        """Otherwise the remembered figure would double-count against the real
        basis and freeze the token for the rest of the TTL."""
        e = self._engine(wallet_cost=15.0)
        e._note_submitted("u", "tok", 15.0)
        self.assertIsNone(await e._clamp_to_verified_position("u", _action(), self.RISK))
        self.assertEqual(0.0, e._submitted_basis("u", "tok"),
                         "memory should retire once the wallet is authoritative")

    async def test_an_unverifiable_wallet_still_fails_closed(self):
        e = _Engine()
        e._wallet_position = AsyncMock(return_value=None)
        self.assertIsNone(await e._clamp_to_verified_position("u", _action(), self.RISK))


class AssumptionTests(unittest.TestCase):
    def test_cap_safety_does_not_depend_only_on_process_memory(self):
        """Post-submission exceptions must retain the durable claim. The
        process-local submitted basis is only a second layer, not the fence."""
        import inspect
        from backend.core import execution
        src = inspect.getsource(execution.place_market_order)
        self.assertNotIn("except InsufficientLiquidityError", src)
        self.assertIn("res.submission_uncertain = True", src)


if __name__ == "__main__":
    unittest.main()


class LeaderAgeGateTests(unittest.TestCase):
    """A trade found long after the leader made it must not be chased.

    leader_age was computed and logged from the day the fast path was written,
    and never enforced — so a trade surfaced late by a restart, a detector
    stall, or a funding change was copied at whatever the book said hours
    later. That is the entry the 2026-08-23 report opened with.
    """

    def test_the_gate_exists_and_is_minutes_not_hours(self):
        self.assertTrue(hasattr(ce, "MAX_LEADER_TRADE_AGE_SECONDS"))
        self.assertGreater(ce.MAX_LEADER_TRADE_AGE_SECONDS, 0)
        self.assertLessEqual(
            ce.MAX_LEADER_TRADE_AGE_SECONDS, 900,
            "an hour-old leader trade must not qualify; that was the incident")

    def test_the_gate_is_actually_enforced_not_just_logged(self):
        """Guards the specific regression: a computed-but-unused age."""
        import inspect
        src = inspect.getsource(ce.CopyEngine._handle_leader_trade)
        self.assertIn("MAX_LEADER_TRADE_AGE_SECONDS", src,
                      "leader_age is computed but never compared — the original bug")
        self.assertIn("leader_trade_too_old", src)


class NoBackfillTests(unittest.IsolatedAsyncioTestCase):
    """A new follow must not inherit the leader's existing book."""

    def _engine(self):
        e = _Engine()
        e._no_backfill = {}
        return e

    def test_first_sight_excludes_everything_held(self):
        e = self._engine()
        key = ("u", "leader")
        held = {"a", "b"}
        e._no_backfill[key] = set(held)
        self.assertEqual({"a", "b"}, e._no_backfill[key])

    def test_a_token_the_leader_exits_stops_being_excluded(self):
        """So a genuine RE-entry later is copied normally rather than being
        blacklisted forever."""
        e = self._engine()
        key = ("u", "leader")
        e._no_backfill[key] = {"a", "b"}
        e._no_backfill[key] &= {"b"}          # leader exited "a"
        self.assertEqual({"b"}, e._no_backfill[key])

    def test_the_reconciler_consults_it(self):
        import inspect
        src = inspect.getsource(ce.CopyEngine._sync_user)
        self.assertIn("_no_backfill", src)
        self.assertIn("predates copying", src)


class AdoptUntrackedTests(unittest.TestCase):
    """A BUY reported as failed that actually filled leaves shares with no row,
    no claim and no alert — invisible, and never managed or exited."""

    def test_adoption_is_scoped_to_what_we_can_prove_we_submitted(self):
        """The scope is the safety property: without it, adoption would sweep
        up the user's own manual trades."""
        import inspect
        src = inspect.getsource(ce.CopyEngine._adopt_untracked_submissions)
        self.assertIn("_submitted_basis", src,
                      "adoption must require proof this engine submitted for the token")
        self.assertIn("copy_open_claims", src,
                      "a token with a live claim belongs to the uncertain path")

    def test_it_notifies_and_records_an_event(self):
        import inspect
        src = inspect.getsource(ce.CopyEngine._adopt_untracked_submissions)
        self.assertIn("_notify_position", src, "a rescued position must alert the user")
        self.assertIn("_event", src, "a rescued position must appear in trade history")

    def test_the_reconciler_runs_it(self):
        import inspect
        src = inspect.getsource(ce.CopyEngine._sync_user)
        self.assertIn("_adopt_untracked_submissions", src)
