"""Release C3 tests for safe fetched-TRADE-history screener filtering."""
from __future__ import annotations

import inspect
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from backend.api import routes_traders
from backend.core import trader_stats


class HistoryDaysScreenerFilterTests(unittest.IsolatedAsyncioTestCase):
    def test_history_days_is_the_only_new_numeric_coverage_whitelist_entry(self):
        self.assertIn("history_days", trader_stats._FILTERABLE_COLUMNS)
        parsed = trader_stats.parse_screener_filters({
            "history_days_min": "30",
            "history_days_max": "90.5",
            "stats_refreshed_at_min": "1",
            "address_min": "1",
        })
        self.assertEqual({
            "history_days_min": ("history_days", ">=", 30.0),
            "history_days_max": ("history_days", "<=", 90.5),
        }, parsed)

    def test_history_days_rejects_non_numeric_and_non_finite_values(self):
        for raw in ("", "abc", "NaN", "Infinity", "-Infinity", None):
            with self.subTest(raw=raw):
                parsed = trader_stats.parse_screener_filters({"history_days_min": raw})
                self.assertNotIn("history_days_min", parsed)
        for value in trader_stats.parse_screener_filters({"history_days_min": "30"}).values():
            self.assertTrue(math.isfinite(value[2]))

    async def test_unknown_sort_falls_back_to_period_pnl_not_experimental_score(self):
        db = SimpleNamespace(fetchall=AsyncMock(return_value=[]))
        await trader_stats.get_leaderboard(db, "not-a-sort")
        sql, _ = db.fetchall.await_args.args
        self.assertIn(
            "ORDER BY CASE WHEN pnl_30d IS NULL THEN 1 ELSE 0 END ASC, "
            "pnl_30d DESC, address ASC",
            sql,
        )
        self.assertNotIn("consistency_score", sql)

    async def test_selected_sort_is_null_last_cross_backend_with_address_tiebreak(self):
        db = SimpleNamespace(fetchall=AsyncMock(return_value=[]))
        await trader_stats.get_leaderboard(db, "volume_7d")
        sql, _ = db.fetchall.await_args.args
        self.assertIn(
            "ORDER BY CASE WHEN volume_7d IS NULL THEN 1 ELSE 0 END ASC, "
            "volume_7d DESC, address ASC",
            sql,
        )
        self.assertNotIn("NULLS LAST", sql)

    async def test_history_days_filter_is_parameterized_and_combined_in_sql(self):
        db = SimpleNamespace(fetchall=AsyncMock(return_value=[]))
        filters = trader_stats.parse_screener_filters({
            "history_days_min": "30",
            "pnl_30d_min": "100",
        })

        await trader_stats.get_leaderboard(
            db, "pnl_30d", 50, 0, filters, search="Alice"
        )

        sql, params = db.fetchall.await_args.args
        self.assertIn("history_days >= ?", sql)
        self.assertIn("pnl_30d >= ?", sql)
        self.assertIn(
            "ORDER BY CASE WHEN pnl_30d IS NULL THEN 1 ELSE 0 END ASC, "
            "pnl_30d DESC, address ASC",
            sql,
        )
        self.assertNotIn("history_days >= 30", sql)
        self.assertEqual([30.0, 100.0, "%alice%", "%alice%", "%alice%", 50, 0], params)

    def test_backend_and_route_defaults_are_pnl_30d(self):
        self.assertEqual(
            inspect.signature(trader_stats.get_leaderboard).parameters["sort_by"].default,
            "pnl_30d",
        )
        self.assertEqual(
            inspect.signature(routes_traders.leaderboard).parameters["sort"].default,
            "pnl_30d",
        )
        self.assertIn("Default sort: `pnl_30d`", routes_traders.leaderboard.__doc__ or "")

    def test_history_days_docs_define_filter_only_and_partial_scope(self):
        docs = (Path(__file__).parents[1] / "docs/screener-metric-contract.md").read_text()
        history_section = docs.split("### `history_days`", 1)[1].split("### `stats_refreshed_at`", 1)[0]
        self.assertIn("numeric minimum/maximum filtering is supported", history_section)
        self.assertIn("sorting is not supported", history_section)
        self.assertIn("`history_days >= selected period`", history_section)
        self.assertIn("null and shorter-coverage rows are excluded", history_section)
        self.assertIn("does not certify REDEEM or positions completeness", history_section)


if __name__ == "__main__":
    unittest.main()
