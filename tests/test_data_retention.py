"""Retention for the stores that only grew: empty-account equity snapshots and
the wallet screener cache.

An empty account stored a zero snapshot every pass, 72% of the table in
production on 2026-10-23. It now stores the first reading of each run, and the
chart redraws the rest from it. These tests pin that the chart stays the same,
that the backlog shrinks to each run's first row, and that the screener cache
drops only rows nothing uses.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import main
from backend.core import equity as equity_mod
from backend.core import trader_stats
from backend.db.database import Database, now_iso

USER = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
NOW = dt.datetime(2026, 10, 23, 12, 0, 0, tzinfo=dt.timezone.utc)


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=str(Path(self.tmp.name) / "retention.db"), dsn="")
        await self.db.connect()
        await self.db.init()
        for uid in (USER, OTHER):
            await self.db.execute(
                "INSERT INTO users(id, telegram_user_id, private_key_enc, created_at) "
                "VALUES(?,?,?,?)", (uid, abs(hash(uid)) % 10**9, "enc", now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def row(self, user: str, minutes_ago: float, equity: float, *,
                  held: float = 0.0, realized: float = 0.0, unrealized: float = 0.0):
        ts = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()
        await self.db.execute(
            "INSERT INTO equity_snapshots(user_id, ts, equity, balance, positions_value, "
            "realized_pnl, unrealized_pnl) VALUES(?,?,?,?,?,?,?)",
            (user, ts, equity, equity - held, held, realized, unrealized))

    async def equities(self, user: str = USER) -> list[float]:
        rows = await self.db.fetchall(
            "SELECT equity FROM equity_snapshots WHERE user_id=? ORDER BY ts, id", (user,))
        return [r["equity"] for r in rows]


def _client(cash_usd: float):
    return SimpleNamespace(get_balance_allowance=AsyncMock(
        return_value=SimpleNamespace(balance=int(round(cash_usd * 1e6)))))


def _pm(*positions):
    return SimpleNamespace(get_all_positions=AsyncMock(return_value=(list(positions), True)))


class SnapshotWriteTests(_DbCase):
    async def snap(self, cash: float):
        return await equity_mod.take_snapshot(self.db, USER, _client(cash), _pm())

    async def test_an_empty_account_stores_the_first_reading_of_a_run(self):
        for _ in range(5):
            snapshot = await self.snap(0.0)
            self.assertEqual(0.0, snapshot["equity"], "each pass still returns its reading")
        self.assertEqual([0.0], await self.equities())

    async def test_an_account_with_money_stores_every_reading(self):
        for _ in range(3):
            await self.snap(25.0)
        self.assertEqual([25.0, 25.0, 25.0], await self.equities())

    async def test_money_leaving_and_arriving_are_both_stored(self):
        await self.snap(25.0)
        for _ in range(3):
            await self.snap(0.0)
        await self.snap(10.0)
        self.assertEqual([25.0, 0.0, 10.0], await self.equities())

    async def test_an_empty_reading_that_changed_is_stored(self):
        """A loss booked while the account holds nothing moves realized PnL,
        so that reading is new and kept."""
        await self.snap(0.0)
        await self.db.execute(
            "INSERT INTO copy_positions(id, user_id, trader_address, condition_id, token_id, "
            "outcome, shares, entry_price, notional_usd, opened_at, status, realized_pnl) "
            "VALUES('p1', ?, ?, 'c1', 't1', 'YES', 10, 0.5, 5, ?, 'closed', -5)",
            (USER, OTHER, now_iso()))
        await self.snap(0.0)
        await self.snap(0.0)
        rows = await self.db.fetchall(
            "SELECT realized_pnl FROM equity_snapshots WHERE user_id=? ORDER BY id", (USER,))
        self.assertEqual([0.0, -5.0], [r["realized_pnl"] for r in rows])


class SeriesTests(_DbCase):
    @staticmethod
    def shape(series: list[dict], bucket: int) -> list[tuple]:
        """What the chart draws: one value per bucket, in order."""
        return [(int(dt.datetime.fromisoformat(p["ts"]).timestamp() // bucket),
                 p["equity"], p["balance"], p["pnl"]) for p in series]

    async def test_the_chart_is_the_same_after_the_backlog_goes(self):
        """Funded for two days, then empty for three, with a row every pass:
        each window draws the same series before and after the repeats go."""
        for minutes_ago in range(5 * 24 * 60, -1, -5):
            await self.row(USER, minutes_ago, 40.0 if minutes_ago > 3 * 24 * 60 else 0.0)
        before = {p: await equity_mod.get_series(self.db, USER, p, now=NOW) for p in ("7d", "30d", "all")}

        removed = await equity_mod.prune_flat_snapshots(self.db)

        self.assertEqual(3 * 24 * 12, removed, "every empty row after the run's first")
        for period, series in before.items():
            bucket = equity_mod._BUCKETS[period][1]
            after = await equity_mod.get_series(self.db, USER, period, now=NOW)
            self.assertEqual(self.shape(series, bucket), self.shape(after, bucket), period)

    async def test_an_empty_run_that_began_before_the_window_fills_it(self):
        await self.row(USER, 20 * 24 * 60, 0.0)
        series = await equity_mod.get_series(self.db, USER, "7d", now=NOW)
        self.assertEqual(7 * 24 * 12 + 1, len(series), "every 5-minute bucket through now")
        self.assertTrue(all(p["equity"] == 0.0 for p in series))

    async def test_a_gap_after_a_reading_with_money_stays_a_gap(self):
        """A missing reading after one with money means the read failed, so
        its value is unknown and nothing is drawn there."""
        await self.row(USER, 60, 40.0)
        series = await equity_mod.get_series(self.db, USER, "7d", now=NOW)
        self.assertEqual([40.0], [p["equity"] for p in series])


class FlatRunPruneTests(_DbCase):
    async def test_each_run_keeps_its_first_row(self):
        for minutes_ago, value in ((60, 0.0), (55, 0.0), (50, 0.0), (45, 5.0), (40, 0.0), (35, 0.0)):
            await self.row(USER, minutes_ago, value)
        for minutes_ago in (60, 55):
            await self.row(OTHER, minutes_ago, 0.0)

        self.assertEqual(4, await equity_mod.prune_flat_snapshots(self.db))

        self.assertEqual([0.0, 5.0, 0.0], await self.equities(USER))
        self.assertEqual([0.0], await self.equities(OTHER))

    async def test_readings_with_money_or_a_change_are_kept(self):
        await self.row(USER, 30, 5.0, held=5.0)
        await self.row(USER, 25, 5.0, held=5.0)
        await self.row(USER, 20, 0.0, realized=-1.0)
        await self.row(USER, 15, 0.0, realized=-2.0)
        await self.row(USER, 10, 0.0, unrealized=-3.0)
        self.assertEqual(0, await equity_mod.prune_flat_snapshots(self.db))
        self.assertEqual(5, len(await self.equities()))

    async def test_rows_with_missing_values_are_kept(self):
        for minutes_ago in (20, 15, 10):
            await self.db.execute(
                "INSERT INTO equity_snapshots(user_id, ts, equity, balance, positions_value, "
                "realized_pnl, unrealized_pnl) VALUES(?,?,0,0,0,NULL,0)",
                (USER, (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()))
        self.assertEqual(0, await equity_mod.prune_flat_snapshots(self.db))

    async def test_bounded_passes_converge_on_the_first_row(self):
        for minutes_ago in range(500, 0, -5):
            await self.row(USER, minutes_ago, 0.0)
        passes = 0
        while await equity_mod.prune_flat_snapshots(self.db, scan_limit=7):
            passes += 1
            self.assertLess(passes, 50, "pruning did not converge")
        self.assertEqual([0.0], await self.equities())
        self.assertEqual(0, await equity_mod.prune_flat_snapshots(self.db))


class StaleTraderPruneTests(_DbCase):
    async def cache(self, address: str, *, refreshed: int, success: int | None = None,
                    stats: int | None = None):
        def stamp(days):
            return None if days is None else (NOW - dt.timedelta(days=days)).isoformat()
        await self.db.execute(
            "INSERT INTO trader_cache(address, last_refreshed, last_refresh_success_at, "
            "stats_refreshed_at) VALUES(?,?,?,?)",
            (address, stamp(refreshed), stamp(success), stamp(stats)))

    async def follow(self, address: str, *, active: bool):
        await self.db.execute(
            "INSERT INTO followed_traders(id, user_id, trader_address, created_at, is_active) "
            "VALUES(?,?,?,?,?)", (f"f-{address[-6:]}", USER, address, now_iso(), int(active)))

    async def addresses(self) -> set[str]:
        return {r["address"] for r in await self.db.fetchall("SELECT address FROM trader_cache")}

    async def test_only_dead_unused_rows_go(self):
        dead = "0x" + "a1" * 20
        fresh = "0x" + "a2" * 20
        stats_recent = "0x" + "a3" * 20
        followed = "0x" + "a4" * 20
        stopped = "0x" + "a5" * 20
        positioned = "0x" + "a6" * 20
        checksummed = "0x" + "Ab" * 20
        await self.cache(dead, refreshed=40)
        await self.cache(fresh, refreshed=2)
        await self.cache(stats_recent, refreshed=40, stats=5)
        for address in (followed, stopped, positioned, checksummed):
            await self.cache(address, refreshed=40)
        await self.follow(followed, active=True)
        await self.follow(stopped, active=False)
        await self.follow(checksummed.lower(), active=True)
        await self.db.execute(
            "INSERT INTO copy_positions(id, user_id, trader_address, condition_id, token_id, "
            "outcome, shares, entry_price, notional_usd, opened_at, status, realized_pnl) "
            "VALUES('p1', ?, ?, 'c1', 't1', 'YES', 10, 0.5, 5, ?, 'closed', 1)",
            (USER, positioned, now_iso()))

        self.assertEqual(1, await trader_stats.prune_stale_traders(self.db, days=30, now=NOW))

        self.assertEqual({fresh, stats_recent, followed, stopped, positioned, checksummed},
                         await self.addresses())


class RetentionLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_prune_runs_on_the_first_pass_and_then_daily(self):
        stop = asyncio.Event()
        waits = 0

        async def fake_wait_for(awaitable, timeout):
            nonlocal waits
            awaitable.close()
            waits += 1
            if waits > 49:
                stop.set()
                return None
            raise asyncio.TimeoutError

        fake_asyncio = SimpleNamespace(wait_for=fake_wait_for, TimeoutError=asyncio.TimeoutError)
        with patch.object(main, "asyncio", fake_asyncio), \
                patch.object(main.telemetry, "prune_product_events", AsyncMock(return_value=0)), \
                patch.object(main.equity, "compact_snapshots", AsyncMock(return_value=0)), \
                patch.object(main.equity, "prune_flat_snapshots", AsyncMock(return_value=0)) as flat, \
                patch.object(main.trader_stats, "prune_stale_traders", AsyncMock(return_value=0)) as stale:
            await main._telemetry_retention_loop(object(), stop)

        self.assertEqual(49, flat.await_count, "empty runs are pruned every hourly pass")
        self.assertEqual(3, stale.await_count, "passes 1, 25 and 49")
