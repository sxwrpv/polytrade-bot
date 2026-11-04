"""The Data API v2 contract PolymarketClient reads.

Polymarket retired Data API v1 on 2026-11-02: every v1 route answers 410, so
the copy engine stopped seeing leader trades and positions. v2 wraps rows in
{"data", "pagination"}, pages by cursor (an `offset` parameter is a 400) and
names fields in snake_case. The row shapes below follow live responses read on
2026-11-04.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.core import trader_stats
from backend.core.polymarket import LeaderEntry, PolymarketClient, Position, Trade

WALLET = "0x" + "ab" * 20

POSITION_ROW = {
    "proxy_wallet": WALLET, "token_id": "111", "condition_id": "0xc1",
    "current_size": 12.5, "avg_price": 0.4, "entry_cost_usdc": 5.0,
    "current_price": 0.6, "current_value": 7.5, "realized_pnl": 1.25,
    "unrealized_pnl": 2.5, "percent_pnl": 50.0, "status": "OPEN",
    "redeemable": False, "mergeable": False, "negative_risk": True,
    "title": "Market", "slug": "market", "event_slug": "event", "outcome": "Yes",
    "outcome_index": 0, "opposite_outcome": "No", "opposite_token_id": "222",
    "end_date": "2026-11-04", "icon": "icon.png",
}
ACTIVITY_ROW = {
    "proxy_wallet": WALLET, "timestamp": 1793776866, "condition_id": "0xc2",
    "type": "TRADE", "size": 310.09, "usdc_size": 0.31009,
    "transaction_hash": "0xfeed", "price": 0.001, "token_id": "333",
    "side": "SELL", "outcome_index": 1, "title": "Market", "slug": "market",
    "event_slug": "event", "outcome": "No",
}
BOARD_ROW = {
    "rank": 3, "user_id": WALLET, "pnl": 1250.5, "volume": 19826.25,
    "user_name": "alice", "profile_image": "p.png", "x_username": "al", "verified": True,
}


def envelope(data, next_cursor=None):
    return {"data": data,
            "pagination": {"limit": 50, "offset": 0, "has_more": next_cursor is not None,
                           "next_cursor": next_cursor}}


class FakeClient(PolymarketClient):
    """Answers each GET from `pages` in order and records (url, params)."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.requests = []

    async def _get(self, url, params=None):
        self.requests.append((url, dict(params or {})))
        return self.pages.pop(0)


class RowParsingTests(unittest.TestCase):
    def test_a_v2_position_row_parses_into_every_field(self):
        p = Position.from_api(POSITION_ROW)
        self.assertEqual(("111", "0xc1", 12.5, 0.4, 0.6), (p.asset, p.condition_id, p.size,
                                                          p.avg_price, p.cur_price))
        self.assertEqual((5.0, 7.5, 2.5, 1.25), (p.initial_value, p.current_value,
                                                 p.cash_pnl, p.realized_pnl))
        self.assertEqual((True, "222", "event", "2026-11-04"),
                         (p.negative_risk, p.opposite_asset, p.event_slug, p.end_date))

    def test_a_v2_trade_row_parses_its_usdc_and_hash(self):
        t = Trade.from_api(ACTIVITY_ROW)
        self.assertEqual(("333", "0xc2", 0.31009, "0xfeed", 1, 1793776866),
                         (t.asset, t.condition_id, t.usd_size, t.tx_hash, t.outcome_index,
                          t.timestamp))

    def test_board_and_single_user_rows_parse(self):
        e = LeaderEntry.from_api(BOARD_ROW)
        self.assertEqual((3, WALLET, 19826.25, "alice", True),
                         (e.rank, e.proxy_wallet, e.vol, e.user_name, e.verified))
        single = {k: v for k, v in BOARD_ROW.items() if k != "rank"}
        self.assertEqual(59, LeaderEntry.from_api({**single, "rank_pnl": 59}).rank)

    def test_v1_rows_still_parse(self):
        p = Position.from_api({"asset": "9", "size": "3", "curPrice": "0.5", "cashPnl": "1"})
        self.assertEqual(("9", 3.0, 0.5, 1.0), (p.asset, p.size, p.cur_price, p.cash_pnl))


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_positions_read_v2_with_its_filter_and_sort_names(self):
        pm = FakeClient(envelope([POSITION_ROW]))
        positions = await pm.get_positions(WALLET, size_threshold=0, market="0xc1")
        url, params = pm.requests[0]
        self.assertEqual("https://data-api.polymarket.com/v2/positions", url)
        self.assertEqual({"user": WALLET, "filter_type": "TOKENS", "filter_amount": 0,
                          "sort_by": "CURRENT_VALUE", "sort_direction": "DESC",
                          "condition": "0xc1", "limit": 500}, params)
        self.assertEqual(["111"], [p.asset for p in positions])

    async def test_no_request_carries_a_parameter_v2_refuses(self):
        pm = FakeClient(envelope([POSITION_ROW], "c1"), envelope([POSITION_ROW]),
                        envelope([ACTIVITY_ROW]), envelope([BOARD_ROW]))
        await pm.get_all_positions(WALLET, size_threshold=0)
        await pm.get_trade_history(WALLET, limit=50)
        await pm.get_leaderboard(period="WEEK", order_by="VOL")
        for url, params in pm.requests:
            self.assertIn("/v2/", url)
            self.assertFalse({"offset", "market", "sizeThreshold"} & set(params), (url, params))

    async def test_leaderboard_maps_v1_names_and_follows_the_cursor(self):
        pm = FakeClient(envelope([BOARD_ROW], "next-1"), envelope([BOARD_ROW]))
        entries, cursor = await pm.get_leaderboard_page(period="MONTH", order_by="VOL", limit=50)
        self.assertEqual("next-1", cursor)
        await pm.get_leaderboard_page(period="MONTH", order_by="VOL", limit=50, cursor=cursor)
        first, second = (params for _, params in pm.requests)
        self.assertEqual({"category": "overall", "time_period": "month", "sort_by": "VOLUME",
                          "limit": 50}, first)
        self.assertEqual("next-1", second["cursor"])
        self.assertEqual([WALLET], [e.proxy_wallet for e in entries])

    async def test_one_users_row_is_an_object_and_an_unknown_user_is_null(self):
        single = {k: v for k, v in BOARD_ROW.items() if k != "rank"}
        pm = FakeClient({"data": {**single, "rank_pnl": 59}}, {"data": None})
        entry = await pm.get_leaderboard_user(WALLET)
        self.assertEqual((59, 1250.5, 19826.25), (entry.rank, entry.pnl, entry.vol))
        self.assertEqual("all", pm.requests[0][1]["time_period"])
        self.assertIsNone(await pm.get_leaderboard_user(WALLET))

    async def test_user_volume_reads_usd_over_a_window(self):
        pm = FakeClient({"data": {"volume": 19826904.98, "volume_usdc": 12798634.16,
                                  "trade_count": 13549}},
                        {"data": {"volume": 0, "volume_usdc": 0, "trade_count": 0}},
                        {"data": None})
        self.assertEqual(12798634.16, await pm.get_user_volume(WALLET, start=100, end=200))
        url, params = pm.requests[0]
        self.assertEqual("https://data-api.polymarket.com/v2/user-volume", url)
        self.assertEqual({"user": WALLET, "start": 100, "end": 200}, params)
        self.assertEqual(0.0, await pm.get_user_volume(WALLET))
        self.assertEqual({"user": WALLET}, pm.requests[1][1])
        self.assertIsNone(await pm.get_user_volume(WALLET))

    async def test_redeem_rows_also_answer_to_their_v1_names(self):
        """copy_engine and trader_stats.realized_closings read conditionId and
        usdcSize from raw redeem rows."""
        redeem = {**ACTIVITY_ROW, "type": "REDEEM", "usdc_size": 390.0}
        pm = FakeClient(envelope([redeem], "more"))
        rows, cursor = await pm.redeems_page(WALLET, limit=50)
        self.assertEqual("more", cursor)
        self.assertEqual(("0xc2", 390.0, "333"),
                         (rows[0]["conditionId"], rows[0]["usdcSize"], rows[0]["asset"]))
        self.assertEqual("REDEEM", pm.requests[0][1]["type"])


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_feed_follows_its_cursor_and_stops_on_the_last_page(self):
        pages = {None: ([LeaderEntry.from_api({**BOARD_ROW, "user_id": "0x1"})], "p2"),
                 "p2": ([LeaderEntry.from_api({**BOARD_ROW, "user_id": "0x2"})], None)}
        pm = AsyncMock()
        pm.get_leaderboard_page.side_effect = lambda **kw: pages[kw["cursor"]]
        feeds = (("ALL", "VOL", 12, True),)
        with patch.object(trader_stats, "_DISCOVERY_FEEDS", feeds), \
                patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert:
            found = await trader_stats.discover_active_wallets(object(), pm)
        self.assertEqual(2, found)
        self.assertEqual([None, "p2"], [c.kwargs["cursor"] for c in pm.get_leaderboard_page.call_args_list])
        self.assertEqual(2, upsert.await_count)
        # Board volume counts shares, so discovery writes pnl and no volume_usd.
        fields = upsert.await_args.args[2]
        self.assertIn("total_pnl", fields)
        self.assertNotIn("volume_usd", fields)

    async def test_a_failed_page_ends_its_feed_and_the_next_feed_runs(self):
        pm = AsyncMock()
        pm.get_leaderboard_page.side_effect = [
            RuntimeError("upstream"),
            ([LeaderEntry.from_api({**BOARD_ROW, "user_id": "0x3"})], None),
        ]
        feeds = (("ALL", "VOL", 12, True), ("WEEK", "PNL", 4, False))
        with patch.object(trader_stats, "_DISCOVERY_FEEDS", feeds), \
                patch.object(trader_stats, "_upsert", new=AsyncMock()):
            found = await trader_stats.discover_active_wallets(object(), pm)
        self.assertEqual(1, found)
        self.assertEqual(2, pm.get_leaderboard_page.await_count)


if __name__ == "__main__":
    unittest.main()
