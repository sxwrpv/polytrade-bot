"""Backoff and quarantine for wallets whose upstream data keeps failing.

Production, 30 Aug - 2 Sep: 381 of 388 windowed-stats failures came from just
two addresses. The cause was the batch ordering — refresh_all sorted by
"stats_refreshed_at IS NULL DESC", and a wallet that never succeeds never sets
that column, so it sat at the front of every batch forever, burning upstream
capacity and displacing wallets that would have refreshed fine.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import tempfile
import unittest

import httpx

from backend.core import trader_stats as ts
from backend.db.database import Database, now_iso


def run(coro):
    return asyncio.run(coro)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://data-api.polymarket.com/activity")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


class BackoffCurveTests(unittest.TestCase):
    def test_cooldown_grows_with_consecutive_failures(self):
        delays = [ts.refresh_backoff_seconds(n) for n in range(1, 6)]
        self.assertEqual(delays, sorted(delays))
        self.assertLess(delays[0], delays[-1])

    def test_cooldown_is_capped(self):
        self.assertLessEqual(ts.refresh_backoff_seconds(50),
                             ts.REFRESH_BACKOFF_MAX_SECONDS)

    def test_address_specific_status_quarantines_for_hours(self):
        """A 500 on this wallet is not going to clear on the next pass."""
        self.assertGreaterEqual(ts.refresh_backoff_seconds(1, 500),
                                ts.ADDRESS_SPECIFIC_MIN_SECONDS)

    def test_transport_blip_starts_gently(self):
        self.assertLess(ts.refresh_backoff_seconds(1, None),
                        ts.ADDRESS_SPECIFIC_MIN_SECONDS)

    def test_status_is_read_through_the_exception_chain(self):
        try:
            try:
                raise _status_error(500)
            except httpx.HTTPStatusError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            self.assertEqual(ts._http_status_of(outer), 500)

    def test_status_of_a_plain_error_is_none(self):
        self.assertIsNone(ts._http_status_of(ValueError("nope")))


class RefreshRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path)
        await self.db.connect()
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def _add(self, address, **fields):
        await ts._upsert(self.db, address, fields or {"display_name": None})

    async def _col(self, address, column):
        row = await self.db.fetchone(
            f"SELECT {column} AS v FROM trader_cache WHERE address=?", (address,))
        return row["v"]

    async def test_a_failing_wallet_stops_leading_every_batch(self):
        """The actual production bug: never-successful wallets sorted first,
        forever, because stats_refreshed_at stayed NULL."""
        await self._add("0xbad")
        await self._add("0xgood")

        calls: list[str] = []

        async def fake_refresh(address, db, pm):
            calls.append(address)
            if address == "0xbad":
                raise _status_error(500)
            return {}

        original = ts.refresh_trader_stats
        ts.refresh_trader_stats = fake_refresh
        try:
            await ts.refresh_all(self.db, pm=None, limit=10, concurrency=1)
            self.assertIn("0xbad", calls)
            first_round = len(calls)
            # second pass, immediately after
            await ts.refresh_all(self.db, pm=None, limit=10, concurrency=1)
        finally:
            ts.refresh_trader_stats = original

        self.assertNotIn("0xbad", calls[first_round:],
                         "quarantined wallet was retried immediately")
        self.assertIn("0xgood", calls[first_round:],
                      "healthy wallet was dropped from the rotation")

    async def test_failure_records_state_and_a_future_retry(self):
        await self._add("0xbad")

        async def fake_refresh(address, db, pm):
            raise _status_error(500)

        original = ts.refresh_trader_stats
        ts.refresh_trader_stats = fake_refresh
        try:
            await ts.refresh_all(self.db, pm=None, limit=10, concurrency=1)
        finally:
            ts.refresh_trader_stats = original

        self.assertEqual(await self._col("0xbad", "refresh_failure_count"), 1)
        self.assertEqual(await self._col("0xbad", "data_completeness"),
                         "stale_refresh_failed")
        self.assertIn("500", await self._col("0xbad", "last_refresh_error"))
        self.assertGreater(await self._col("0xbad", "next_retry_at"), now_iso())

    async def test_consecutive_failures_extend_the_cooldown(self):
        await self._add("0xbad")

        async def fake_refresh(address, db, pm):
            raise ValueError("transient")

        original = ts.refresh_trader_stats
        ts.refresh_trader_stats = fake_refresh
        try:
            await ts.refresh_all(self.db, pm=None, limit=10, concurrency=1)
            first = await self._col("0xbad", "next_retry_at")
            # let it become eligible again, then fail once more
            await self.db.execute(
                "UPDATE trader_cache SET next_retry_at=? WHERE address=?",
                ("2000-01-01T00:00:00+00:00", "0xbad"))
            await ts.refresh_all(self.db, pm=None, limit=10, concurrency=1)
        finally:
            ts.refresh_trader_stats = original

        self.assertEqual(await self._col("0xbad", "refresh_failure_count"), 2)
        self.assertGreater(await self._col("0xbad", "next_retry_at"), first)

    async def test_success_clears_the_failure_state(self):
        await self._add("0xflaky")
        await self.db.execute(
            "UPDATE trader_cache SET refresh_failure_count=4, next_retry_at=?, "
            "last_refresh_error='old', data_completeness='stale_refresh_failed' "
            "WHERE address=?", ("2000-01-01T00:00:00+00:00", "0xflaky"))

        async def fake_refresh(address, db, pm):
            return {}

        original = ts.refresh_trader_stats
        ts.refresh_trader_stats = fake_refresh
        try:
            done = await ts.refresh_all(self.db, pm=None, limit=10, concurrency=1)
        finally:
            ts.refresh_trader_stats = original

        self.assertEqual(done, 1)
        self.assertEqual(await self._col("0xflaky", "refresh_failure_count"), 0)
        self.assertIsNone(await self._col("0xflaky", "next_retry_at"))
        self.assertIsNone(await self._col("0xflaky", "last_refresh_error"))
        self.assertEqual(await self._col("0xflaky", "data_completeness"), "complete")
        self.assertIsNotNone(await self._col("0xflaky", "last_refresh_success_at"))

    async def test_cached_stats_survive_a_quarantine(self):
        """Stale, not deleted — the board keeps showing the last good numbers."""
        await self._add("0xbad", total_pnl=1234.5, pnl_30d=99.0)

        async def fake_refresh(address, db, pm):
            raise _status_error(500)

        original = ts.refresh_trader_stats
        ts.refresh_trader_stats = fake_refresh
        try:
            await ts.refresh_all(self.db, pm=None, limit=10, concurrency=1)
        finally:
            ts.refresh_trader_stats = original

        self.assertEqual(await self._col("0xbad", "total_pnl"), 1234.5)
        self.assertEqual(await self._col("0xbad", "pnl_30d"), 99.0)

    async def test_a_wallet_whose_cooldown_expired_is_retried(self):
        await self._add("0xbad")
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        await self.db.execute(
            "UPDATE trader_cache SET refresh_failure_count=3, next_retry_at=? "
            "WHERE address=?", (past, "0xbad"))
        calls = []

        async def fake_refresh(address, db, pm):
            calls.append(address)
            return {}

        original = ts.refresh_trader_stats
        ts.refresh_trader_stats = fake_refresh
        try:
            await ts.refresh_all(self.db, pm=None, limit=10, concurrency=1)
        finally:
            ts.refresh_trader_stats = original
        self.assertEqual(calls, ["0xbad"])


if __name__ == "__main__":
    unittest.main()
