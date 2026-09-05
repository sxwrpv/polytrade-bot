"""Regression tests for the 2026-09-02 audit findings (H2, H3, M1, M2, P4).

Each test names the failure it prevents, because every one of these was a
guard that existed, was documented as working, and was wired to an input that
could not carry the signal it needed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import stat as stat_mod
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.core import copy_engine as ce
from backend.core import runtime_security
from backend.core.copy_engine import CopyEngine


def run(coro):
    return asyncio.run(coro)


def _iso_ago(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds)).isoformat()


# --------------------------------------------------------------------------
# H3 — an uncertain claim must not fence a token forever
# --------------------------------------------------------------------------
class UncertainClaimReleaseTests(unittest.TestCase):
    """execution classifies EVERY post-submission failure as uncertain, on
    purpose. _settle_uncertain_claim only adopted or retained, so a routine
    rate-limit rejection fenced that token for the life of the deployment:
    no BUY, no SELL, no admin route, only direct SQL."""

    def _engine(self):
        eng = CopyEngine.__new__(CopyEngine)
        eng.db = SimpleNamespace()
        eng.released = []

        async def fetchone(sql, params=()):
            return None                     # no tracked row explains it

        async def release(user_id, token, claim_id):
            eng.released.append((user_id, token, claim_id))

        eng.db.fetchone = fetchone
        eng._release_buy_claim = release
        return eng

    def _claim(self, age_seconds):
        return {"claim_id": "c1", "token_id": "tok", "action": "open",
                "user_id": "0xu", "reserved_usd": 15.0,
                "claimed_at": _iso_ago(age_seconds), "updated_at": _iso_ago(1),
                "last_error": "api_error: RateLimitError"}

    def test_a_fresh_uncertain_claim_is_retained(self):
        eng = self._engine()
        run(eng._settle_uncertain_claim("0xu", self._claim(60), None))
        self.assertEqual(eng.released, [], "released before the window elapsed")

    def test_an_aged_claim_the_wallet_cannot_explain_is_released(self):
        eng = self._engine()
        age = ce.UNCERTAIN_CLAIM_RELEASE_SECONDS + 60
        run(eng._settle_uncertain_claim("0xu", self._claim(age), None))
        self.assertEqual(eng.released, [("0xu", "tok", "c1")],
                         "token stayed fenced past the release window")

    def test_a_visible_holding_is_still_adopted_not_released(self):
        """Release is for provable non-fills only. Shares present must still
        take the adoption path, or we would abandon a real position."""
        eng = self._engine()
        adopted = []

        async def adopt(user_id, claim, p):
            adopted.append(claim["claim_id"])

        eng._adopt_uncertain_fill = adopt
        held = SimpleNamespace(size=10.0, avg_price=0.5, asset="tok",
                               condition_id="c", slug="s", title="t", outcome="YES")
        age = ce.UNCERTAIN_CLAIM_RELEASE_SECONDS + 60
        run(eng._settle_uncertain_claim("0xu", self._claim(age), held))
        self.assertEqual(adopted, ["c1"])
        self.assertEqual(eng.released, [])

    def test_claim_age_survives_a_naive_timestamp(self):
        eng = self._engine()
        naive = dt.datetime.now() - dt.timedelta(seconds=120)
        age = CopyEngine._claim_age_seconds({"claimed_at": naive.isoformat()})
        self.assertIsNotNone(age)

    def test_an_unparseable_timestamp_does_not_release(self):
        """Unknown age must fail closed — retain, never release."""
        eng = self._engine()
        run(eng._settle_uncertain_claim(
            "0xu", {"claim_id": "c1", "token_id": "tok", "action": "open",
                    "claimed_at": "not-a-date", "updated_at": None}, None))
        self.assertEqual(eng.released, [])


# --------------------------------------------------------------------------
# M2 — both paths must count capital at risk identically
# --------------------------------------------------------------------------
class ExposureAccountingTests(unittest.TestCase):
    def test_fast_path_counts_the_same_statuses_as_the_reconciler(self):
        """_sync_user's comment asserted the fast path already counted all
        three statuses. It counted only 'open', so its MAX OPEN / MAX EXPOSURE
        pre-checks were laxer than the gate that enforces them."""
        source = Path("backend/core/copy_engine.py").read_text()
        fast = source.index("async def _handle_leader_trade")
        recon = source.index("async def _reconcile_tick")
        fast_block = source[fast:recon]
        self.assertIn(
            "status IN ('open','closing','reconciliation_required')", fast_block,
            "the fast path is back to counting only 'open' rows")
        self.assertNotIn("AND status = 'open'\", (user_id,))", fast_block)


# --------------------------------------------------------------------------
# P4 — hardening must not abort on the first unreadable path
# --------------------------------------------------------------------------
class HardenRuntimeFilesTests(unittest.TestCase):
    def test_one_unreadable_candidate_does_not_skip_the_rest(self):
        """In the container /app/data/copybot.db is root-owned 0700 and
        unreadable to the app user, so this raised PermissionError and
        hardened NOTHING — logging an ERROR every boot while the runbook said
        it ran."""
        with tempfile.TemporaryDirectory() as root:
            root_p = Path(root)
            env = root_p / ".env"
            env.write_text("SECRET=1")
            env.chmod(0o644)

            blocked = root_p / "blocked"
            blocked.mkdir()
            db = blocked / "copybot.db"
            db.write_text("x")
            blocked.chmod(0o000)
            try:
                runtime_security.harden_runtime_files(root_p, db_path=str(db))
                mode = stat_mod.S_IMODE(env.stat().st_mode)
                self.assertEqual(mode, 0o600,
                                 ".env was left world-readable because an "
                                 "earlier candidate raised")
            finally:
                blocked.chmod(0o755)

    def test_it_still_tightens_what_it_can_reach(self):
        with tempfile.TemporaryDirectory() as root:
            root_p = Path(root)
            env = root_p / ".env"
            env.write_text("SECRET=1")
            env.chmod(0o666)
            db = root_p / "copybot.db"
            db.write_text("x")
            db.chmod(0o666)
            runtime_security.harden_runtime_files(root_p, db_path="copybot.db")
            self.assertEqual(stat_mod.S_IMODE(env.stat().st_mode), 0o600)
            self.assertEqual(stat_mod.S_IMODE(db.stat().st_mode), 0o600)


# --------------------------------------------------------------------------
# M1 — a truncated wallet page is not proof a position was exited
# --------------------------------------------------------------------------
class StuckClosingTruncationTests(unittest.TestCase):
    """Every other reconciler here pages and honours `complete`. This one read
    a single 500-row page sorted by current value, so a holding past the page
    boundary looked departed and got finalized while the shares were still in
    the wallet."""

    def _engine(self, positions, complete):
        eng = CopyEngine.__new__(CopyEngine)
        eng.settled = []
        rows = [{"id": "row-1", "user_id": "0xu", "token_id": "tok",
                 "shares": 10.0, "closing_at": _iso_ago(9999),
                 "condition_id": "cond", "entry_price": 0.4,
                 "notional_usd": 4.0}]

        async def fetchall(sql, params=()):
            return rows

        async def get_all_positions(user_id, size_threshold=1.0):
            return positions, complete

        async def settle(user_id, row, p):
            eng.settled.append((row["id"], p))

        eng.db = SimpleNamespace(fetchall=fetchall)
        eng.pm = SimpleNamespace(get_all_positions=get_all_positions)
        eng._settle_stuck_closing = settle
        return eng

    def test_absence_from_a_TRUNCATED_scan_is_not_acted_on(self):
        eng = self._engine(positions=[], complete=False)
        run(eng._recover_stuck_closings())
        self.assertEqual(eng.settled, [],
                         "finalized a position on an incomplete wallet scan")

    def test_absence_from_a_COMPLETE_scan_is_acted_on(self):
        eng = self._engine(positions=[], complete=True)
        run(eng._recover_stuck_closings())
        self.assertEqual([r for r, _ in eng.settled], ["row-1"])

    def test_a_visible_holding_is_always_acted_on(self):
        held = SimpleNamespace(asset="tok", size=10.0, redeemable=False,
                               cur_price=0.5, avg_price=0.4)
        eng = self._engine(positions=[held], complete=False)
        run(eng._recover_stuck_closings())
        self.assertEqual(len(eng.settled), 1)
        self.assertIs(eng.settled[0][1], held)


# --------------------------------------------------------------------------
# H2 — on-chain trades must carry the block's time, not the scan's
# --------------------------------------------------------------------------
class OnChainTimestampTests(unittest.TestCase):
    """MAX_LEADER_TRADE_AGE_SECONDS compares wall clock to trade.timestamp.
    Stamping time.time() made every on-chain trade report an age of ~0, so the
    gate added after 2026-08-23 could never fire — and a detector stall makes
    _scan replay up to max_block_span blocks, about an hour of Polygon."""

    def _detector(self, block_times):
        from backend.core.detection import OnChainDetector
        d = OnChainDetector.__new__(OnChainDetector)
        d._block_times = {}
        d.w3 = SimpleNamespace(eth=SimpleNamespace(
            get_block=lambda n: {"timestamp": block_times[n]}))
        d._Web3 = SimpleNamespace(to_hex=lambda b: "0x" + b.hex())
        return d

    def _log(self, block, maker_asset=0, taker_asset=7, maker_amt=1_000_000,
             taker_amt=2_000_000):
        data = b"".join(int(w).to_bytes(32, "big") for w in
                        (maker_asset, taker_asset, maker_amt, taker_amt, 0))
        return {"blockNumber": block, "logIndex": 0, "data": data,
                "transactionHash": b"\x01" * 32}

    def test_the_block_timestamp_is_used(self):
        d = self._detector({100: 1_700_000_000})
        t = d._decode(self._log(100), "0xlead", timestamp=d._block_time(100))
        self.assertEqual(t.timestamp, 1_700_000_000)

    def test_an_old_block_reports_a_real_age(self):
        """The exact regression: a replayed hour-old fill must look hour-old."""
        import time
        old = int(time.time()) - 3600
        d = self._detector({500: old})
        t = d._decode(self._log(500), "0xlead", timestamp=d._block_time(500))
        age = time.time() - t.timestamp
        self.assertGreater(age, 3000,
                           "a replayed fill still claims to be brand new")
        self.assertGreater(age, ce.MAX_LEADER_TRADE_AGE_SECONDS,
                           "the age gate would not skip this trade")

    def test_block_times_are_memoized(self):
        calls = []

        def get_block(n):
            calls.append(n)
            return {"timestamp": 1_700_000_000}

        d = self._detector({})
        d.w3 = SimpleNamespace(eth=SimpleNamespace(get_block=get_block))
        for _ in range(5):
            d._block_time(42)
        self.assertEqual(calls, [42], "one RPC call per distinct block")

    def test_the_memo_is_bounded(self):
        d = self._detector({})
        d.w3 = SimpleNamespace(eth=SimpleNamespace(
            get_block=lambda n: {"timestamp": n}))
        for n in range(5000):
            d._block_time(n)
        self.assertLessEqual(len(d._block_times), 4200)


if __name__ == "__main__":
    unittest.main()
