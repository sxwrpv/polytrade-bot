"""Storage compaction for equity_snapshots.

The snapshot stream has no expiry — the equity curve IS the product, so old
points cannot simply be deleted. They are collapsed instead, to the coarsest
resolution any chart still renders them at. These tests pin the two properties
that makes safe: the rendered line does not change, and repeated runs converge.
"""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from backend.core import equity as equity_mod
from backend.db.database import Database, now_iso

USER = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
NOW = dt.datetime(2026, 8, 23, 12, 0, 0, tzinfo=dt.timezone.utc)


class EquityCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=str(Path(self.tmp.name) / "equity.db"), dsn="")
        await self.db.connect()
        await self.db.init()
        for uid in (USER, OTHER):
            await self.db.execute(
                "INSERT INTO users(id, telegram_user_id, private_key_enc, created_at) "
                "VALUES(?,?,?,?)", (uid, abs(hash(uid)) % 10**9, "enc", now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def seed(self, user: str, *, days_ago: float, count: int, step_seconds: int = 300):
        """`count` snapshots every `step_seconds`, starting `days_ago` back."""
        start = NOW - dt.timedelta(days=days_ago)
        for i in range(count):
            ts = (start + dt.timedelta(seconds=i * step_seconds)).isoformat()
            await self.db.execute(
                "INSERT INTO equity_snapshots(user_id, ts, equity, balance, "
                "positions_value, realized_pnl, unrealized_pnl) VALUES(?,?,?,?,?,?,?)",
                (user, ts, 100.0 + i, 50.0, 50.0 + i, float(i), 0.0))

    async def rows(self, user: str = USER):
        return await self.db.fetchall(
            "SELECT id, ts, equity FROM equity_snapshots WHERE user_id=? ORDER BY ts", (user,))

    async def count(self, user: str = USER) -> int:
        return len(await self.rows(user))

    # --- the core promise ---------------------------------------------------

    async def test_recent_snapshots_are_never_touched(self):
        """Inside 7 days the 7d chart renders 5-minute points, so every row is
        still visible and none may be collapsed."""
        await self.seed(USER, days_ago=3, count=120)
        deleted = await equity_mod.compact_snapshots(self.db, now=NOW)
        self.assertEqual(0, deleted)
        self.assertEqual(120, await self.count())

    async def test_mid_age_collapses_to_one_row_per_thirty_minutes(self):
        # 12 rows at 5-min spacing = one hour = exactly two 30-minute buckets.
        await self.seed(USER, days_ago=14, count=12)
        await equity_mod.compact_snapshots(self.db, now=NOW)
        self.assertEqual(2, await self.count())

    async def test_old_rows_collapse_to_one_row_per_four_hours(self):
        # 96 rows at 5-min spacing = 8 hours = two 4-hour buckets.
        await self.seed(USER, days_ago=60, count=96)
        await equity_mod.compact_snapshots(self.db, now=NOW)
        self.assertEqual(2, await self.count())

    async def test_the_surviving_row_is_the_one_the_chart_already_drew(self):
        """get_series keeps the LAST row in each bucket. Compaction must keep
        the same one, or a compacted curve draws a different line."""
        await self.seed(USER, days_ago=60, count=48)   # 4 hours -> one bucket
        before = await equity_mod.get_series(self.db, USER, "all")
        await equity_mod.compact_snapshots(self.db, now=NOW)
        after = await equity_mod.get_series(self.db, USER, "all")
        self.assertEqual(before, after, "the rendered series changed")

    async def test_compaction_is_invisible_to_every_window(self):
        """The whole design rests on this: for each window, the series before
        and after compaction are identical."""
        await self.seed(USER, days_ago=2, count=60)
        await self.seed(USER, days_ago=20, count=60)
        await self.seed(USER, days_ago=120, count=60)
        before = {p: await equity_mod.get_series(self.db, USER, p) for p in ("7d", "30d", "all")}
        await equity_mod.compact_snapshots(self.db, now=NOW)
        for period, series in before.items():
            self.assertEqual(series, await equity_mod.get_series(self.db, USER, period), period)

    # --- safety -------------------------------------------------------------

    async def test_running_twice_deletes_nothing_the_second_time(self):
        await self.seed(USER, days_ago=60, count=96)
        first = await equity_mod.compact_snapshots(self.db, now=NOW)
        second = await equity_mod.compact_snapshots(self.db, now=NOW)
        self.assertGreater(first, 0)
        self.assertEqual(0, second, "compaction is not idempotent")

    async def test_users_are_compacted_independently(self):
        """Two accounts snapshotting in the same bucket must both keep a row —
        bucketing is per user, never global."""
        await self.seed(USER, days_ago=60, count=48)
        await self.seed(OTHER, days_ago=60, count=48)
        await equity_mod.compact_snapshots(self.db, now=NOW)
        self.assertEqual(1, await self.count(USER))
        self.assertEqual(1, await self.count(OTHER))

    async def test_a_lone_old_snapshot_survives(self):
        """A wallet with one ancient point still has a curve endpoint."""
        await self.seed(USER, days_ago=300, count=1)
        await equity_mod.compact_snapshots(self.db, now=NOW)
        self.assertEqual(1, await self.count())

    async def test_the_coarse_tier_is_not_undone_by_the_finer_one(self):
        """Tiers are applied coarsest-first and each owns a disjoint band. If
        the 30-minute tier also saw 60-day-old rows it would keep a survivor
        per half hour and silently undo the 4-hour pass."""
        await self.seed(USER, days_ago=60, count=48)   # 4 hours of 5-min rows
        await equity_mod.compact_snapshots(self.db, now=NOW)
        self.assertEqual(1, await self.count())

    async def test_a_bounded_pass_never_leaves_a_bucket_empty(self):
        """The scan limit splits work across runs. Whatever it cuts off, no
        bucket may lose its last row — that would delete a visible point."""
        await self.seed(USER, days_ago=60, count=300)
        await equity_mod.compact_snapshots(self.db, now=NOW, scan_limit=37)
        rows = await self.rows()
        self.assertGreater(len(rows), 0)
        # Every 4-hour bucket that had rows still has at least one.
        buckets = {int(equity_mod._epoch(r["ts"]) // 14400) for r in rows}
        self.assertEqual(len(buckets), len({int(equity_mod._epoch(r["ts"]) // 14400) for r in rows}))
        for _ in range(20):
            if not await equity_mod.compact_snapshots(self.db, now=NOW, scan_limit=37):
                break
        self.assertEqual(sorted(buckets), sorted(
            {int(equity_mod._epoch(r["ts"]) // 14400) for r in await self.rows()}),
            "a bucket lost every row across bounded passes")

    async def test_an_empty_table_is_a_no_op(self):
        self.assertEqual(0, await equity_mod.compact_snapshots(self.db, now=NOW))

    # --- the relationship the design depends on -----------------------------

    def test_every_tier_matches_a_query_bucket(self):
        """Compaction is only invisible while each tier stores at least as much
        detail as the finest window reaching that age. If _BUCKETS changes and
        this fails, the chart is about to ask for detail that was discarded."""
        for older_than_days, stored_bucket in equity_mod.COMPACTION_TIERS:
            finest = min(
                (bucket for days, bucket in equity_mod._BUCKETS.values()
                 if days > older_than_days),
                default=None)
            self.assertIsNotNone(finest, f"no window reaches {older_than_days}d")
            self.assertLessEqual(
                stored_bucket, finest,
                f"rows older than {older_than_days}d are stored at {stored_bucket}s "
                f"but a window renders them at {finest}s")


if __name__ == "__main__":
    unittest.main()
