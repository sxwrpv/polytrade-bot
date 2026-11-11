"""2026-11 review, upstream client.

A data-api page without a row list is a failed read, leader books page at
1,000 rows, a single-wallet board row has to name that wallet, an empty
activity page mid-feed leaves the window uncovered, and the on-demand trader
profile has a per-user budget.
"""
from __future__ import annotations

import inspect
import time
import unittest
from unittest import mock

from fastapi import HTTPException

from backend.api import routes_traders
from backend.core import trader_stats
from backend.core.polymarket import MalformedResponseError, PolymarketClient, Trade

WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40

# Bodies that parse as JSON and carry no row list.
MALFORMED = [None, [], "oops", {}, {"data": None}, {"data": {"token_id": "x"}},
             {"error": "upstream"}]


class CannedClient(PolymarketClient):
    """Answers every data-api GET from a queue of bodies."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = []

    async def _get(self, url, params=None):
        self.calls.append(dict(params or {}))
        return self.bodies.pop(0)


def page(rows, cursor=None, has_more=None):
    has_more = bool(cursor) if has_more is None else has_more
    return {"data": rows,
            "pagination": {"limit": 1000, "has_more": has_more, "next_cursor": cursor}}


def position_rows(start, count):
    return [{"token_id": f"tok-{i}", "current_size": 10.0}
            for i in range(start, start + count)]


def trade(size=5.0, ts=None):
    return Trade(proxy_wallet=WALLET, timestamp=int(ts or time.time()), condition_id="c1",
                 side="BUY", asset="tok-1", outcome="YES", outcome_index=0, price=0.5,
                 size=size, usd_size=size * 0.5, title="t", slug="s", tx_hash="0xhash")


class MalformedPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_list_route_without_a_row_list_is_a_failed_read(self):
        readers = {
            "get_positions": lambda pm: pm.get_positions(WALLET),
            "get_all_positions": lambda pm: pm.get_all_positions(WALLET),
            "get_leaderboard_page": lambda pm: pm.get_leaderboard_page(),
            "redeems_page": lambda pm: pm.redeems_page(WALLET),
            "trade_history_page": lambda pm: pm.trade_history_page(WALLET),
        }
        for body in MALFORMED:
            for name, read in readers.items():
                with self.subTest(body=body, reader=name):
                    with self.assertRaises(MalformedResponseError):
                        await read(CannedClient(body))

    def test_the_failed_read_is_a_value_error_like_a_body_that_is_no_json(self):
        self.assertTrue(issubclass(MalformedResponseError, ValueError))

    async def test_a_wallet_with_no_positions_reads_as_complete_and_empty(self):
        self.assertEqual(([], True), await CannedClient(page([])).get_all_positions(WALLET))
        self.assertEqual([], await CannedClient(page([])).get_positions(WALLET))

    async def test_more_rows_with_no_cursor_reads_as_incomplete(self):
        pm = CannedClient(page(position_rows(0, 3), cursor=None, has_more=True))
        positions, complete = await pm.get_all_positions(WALLET)
        self.assertEqual(3, len(positions))
        self.assertFalse(complete)

    async def test_a_single_wallet_route_still_takes_null(self):
        self.assertIsNone(await CannedClient(page(None)).get_leaderboard_user(WALLET))
        self.assertIsNone(await CannedClient(None).get_leaderboard_user(WALLET))


class LeaderBookPagingTests(unittest.IsolatedAsyncioTestCase):
    def test_a_book_is_read_a_thousand_rows_a_page_for_three_pages(self):
        params = inspect.signature(PolymarketClient.get_all_positions).parameters
        self.assertEqual(1000, params["page_size"].default)
        self.assertEqual(3, params["max_pages"].default)

    async def test_the_walk_follows_the_cursor_to_the_last_page(self):
        pm = CannedClient(page(position_rows(0, 1000), "c1"), page(position_rows(1000, 5)))
        positions, complete = await pm.get_all_positions(WALLET)
        self.assertTrue(complete)
        self.assertEqual(1005, len({p.asset for p in positions}))
        self.assertEqual([1000, 1000], [c["limit"] for c in pm.calls])
        self.assertNotIn("cursor", pm.calls[0])
        self.assertEqual("c1", pm.calls[1]["cursor"])

    async def test_a_book_past_three_thousand_rows_is_flagged_incomplete(self):
        pm = CannedClient(page(position_rows(0, 1000), "c1"),
                          page(position_rows(1000, 1000), "c2"),
                          page(position_rows(2000, 1000), "c3"))
        positions, complete = await pm.get_all_positions(WALLET)
        self.assertFalse(complete)
        self.assertEqual(3000, len(positions))
        self.assertEqual(3, len(pm.calls))


class LeaderboardUserRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_wallets_own_row_is_taken_whatever_its_case(self):
        row = {"user_id": "0x" + "A" * 40, "pnl": 12.5, "rank_pnl": 7}
        entry = await CannedClient(page(row)).get_leaderboard_user(WALLET)
        self.assertEqual(12.5, entry.pnl)

    async def test_a_row_without_a_wallet_is_taken_as_the_answer(self):
        entry = await CannedClient(page({"pnl": 3.0})).get_leaderboard_user(WALLET)
        self.assertEqual(3.0, entry.pnl)

    async def test_another_wallets_row_is_a_failed_read(self):
        with self.assertRaises(MalformedResponseError):
            await CannedClient(page({"user_id": OTHER, "pnl": 99.0})).get_leaderboard_user(WALLET)


class ActivityWindowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def feed(*pages):
        queue = list(pages)

        async def fetch(limit, cursor):
            return queue.pop(0)
        return fetch

    async def test_an_empty_page_that_names_a_cursor_leaves_the_window_uncovered(self):
        first = trade()
        rows, covered = await trader_stats._fetch_activity_window(
            self.feed(([first], "c1"), ([], "c2")), days=30, max_pages=5)
        self.assertEqual([first], rows)
        self.assertFalse(covered)

    async def test_the_end_of_the_feed_covers_the_window(self):
        rows, covered = await trader_stats._fetch_activity_window(
            self.feed(([trade()], None)), days=30, max_pages=5)
        self.assertEqual(1, len(rows))
        self.assertTrue(covered)

    async def test_a_repeated_fill_is_kept_once_and_a_different_fill_stays(self):
        now = time.time()
        rows, _ = await trader_stats._fetch_activity_window(
            self.feed(([trade(5.0, now), trade(5.0, now), trade(6.0, now)], None)),
            days=30, max_pages=5)
        self.assertEqual([5.0, 6.0], [t.size for t in rows])


class ProfileBudgetTests(unittest.TestCase):
    def setUp(self):
        routes_traders.reset_rate_limits()
        self.addCleanup(routes_traders.reset_rate_limits)

    def spend(self, user="u1"):
        for _ in range(routes_traders.PROFILE_RATE_LIMIT):
            routes_traders._enforce_profile_rate_limit(user)

    def test_a_user_past_the_budget_is_told_to_wait(self):
        self.spend()
        with self.assertRaises(HTTPException) as caught:
            routes_traders._enforce_profile_rate_limit("u1")
        self.assertEqual(429, caught.exception.status_code)
        self.assertEqual("60", caught.exception.headers["Retry-After"])

    def test_one_users_lookups_leave_another_users_budget_alone(self):
        self.spend("u1")
        routes_traders._enforce_profile_rate_limit("u2")

    def test_the_budget_refills_once_the_window_has_passed(self):
        with mock.patch.object(routes_traders.time, "monotonic", return_value=1000.0):
            self.spend()
        later = 1000.0 + routes_traders.PROFILE_RATE_WINDOW_SECONDS + 1
        with mock.patch.object(routes_traders.time, "monotonic", return_value=later):
            routes_traders._enforce_profile_rate_limit("u1")

    def test_the_reset_hook_clears_every_bucket(self):
        self.spend()
        routes_traders.reset_rate_limits()
        routes_traders._enforce_profile_rate_limit("u1")

    def test_the_route_checks_the_budget_before_any_upstream_work(self):
        source = inspect.getsource(routes_traders.trader_profile)
        self.assertLess(source.index("_enforce_profile_rate_limit"),
                        source.index("refresh_trader_analysis"))
