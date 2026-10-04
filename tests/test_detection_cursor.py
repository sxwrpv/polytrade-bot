"""Fast-path detection: the cursor second must not swallow a late fill.

The engine's per-follow cursor is the timestamp of the last trade it handled.
The activity indexer does not publish a second's fills together, so a fill
that surfaced a tick after another fill in the same second was filtered out
twice over -- by the detector (`> since`) and the engine (`<= cursor`) -- and
never copied by the fast path. The boundary second is now deduped by tx_hash.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.core.copy_engine import CopyEngine
from backend.core.detection import ActivityPollDetector

USER, LEADER = "0xuser", "0xleader"


def trade(ts, tx, side="BUY"):
    return SimpleNamespace(timestamp=ts, tx_hash=tx, side=side, asset="tok")


class _Detector:
    def __init__(self, *batches):
        self.batches = list(batches)
        self.calls = []

    async def new_trades(self, trader_address, since_ts):
        self.calls.append(since_ts)
        return self.batches.pop(0) if self.batches else []


def engine_with(detector, cursor=100):
    db = AsyncMock()
    db.fetchall.return_value = [
        {"user_id": USER, "trader_address": LEADER, "is_active": 1}]
    engine = CopyEngine(db, SimpleNamespace(), detector=detector)
    engine._cursors[(USER, LEADER)] = cursor
    engine._handle_leader_trade = AsyncMock()
    return engine


def handled(engine):
    return [c.args[1].tx_hash for c in engine._handle_leader_trade.await_args_list]


class DetectCursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_fills_in_one_second_are_both_handled(self):
        engine = engine_with(_Detector([trade(105, "0xa"), trade(105, "0xb")]))
        await engine._detect_tick()
        self.assertEqual(handled(engine), ["0xa", "0xb"])
        self.assertEqual(engine._cursors[(USER, LEADER)], 105)

    async def test_late_indexed_fill_at_cursor_second_is_handled(self):
        engine = engine_with(_Detector([trade(105, "0xa")],
                                       [trade(105, "0xa"), trade(105, "0xb")]))
        await engine._detect_tick()
        await engine._detect_tick()
        self.assertEqual(handled(engine), ["0xa", "0xb"])

    async def test_repeated_boundary_trade_is_not_handled_twice(self):
        sell = trade(105, "0xa", side="SELL")
        engine = engine_with(_Detector([sell], [sell], [sell]))
        for _ in range(3):
            await engine._detect_tick()
        self.assertEqual(handled(engine), ["0xa"])

    async def test_boundary_trade_without_tx_hash_is_skipped(self):
        # Indistinguishable from a repeat, and a repeated SELL sells twice.
        engine = engine_with(_Detector([trade(100, "")]))
        await engine._detect_tick()
        engine._handle_leader_trade.assert_not_awaited()

    async def test_trade_before_cursor_is_skipped(self):
        engine = engine_with(_Detector([trade(99, "0xa")]))
        await engine._detect_tick()
        engine._handle_leader_trade.assert_not_awaited()


class ActivityPollDetectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_cursor_second_inclusively(self):
        pm = SimpleNamespace(get_trade_history=AsyncMock(return_value=[
            trade(99, "0xold"), trade(100, "0xsame"), trade(101, "0xnew")]))
        trades = await ActivityPollDetector(pm).new_trades(LEADER, 100)
        self.assertEqual([t.tx_hash for t in trades], ["0xsame", "0xnew"])


if __name__ == "__main__":
    unittest.main()
