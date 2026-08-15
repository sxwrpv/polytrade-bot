"""Behavioral contract tests for wallet-screener metric metadata."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.core import trader_stats
from tests.fixtures import trader_activity as activity


WINDOWS = ("7d", "30d", "90d")
WINDOWED = {
    f"{stem}_{window}"
    for stem in (
        "winrate", "pnl", "volume", "green_days", "red_days",
        "consistency_ratio", "fills", "exits", "fill_exit_ratio",
    )
    for window in WINDOWS
}
EXPECTED_METRICS = {
    "total_pnl",
    "volume_usd",
    "win_rate",
    "open_positions",
    "consistency_score",
    "pnl_quality",
    "daily_pnl_90d",
    "history_days",
    "stats_refreshed_at",
    *WINDOWED,
}
REQUIRED_FIELDS = {
    "formula",
    "source_endpoint",
    "time_window",
    "row_page_limits",
    "refresh_cadence",
    "null_behavior",
    "partial_behavior",
    "legacy_transition",
    "provenance",
    "safe_for_sorting",
    "safe_for_filtering",
    "label",
    "tooltip",
}


class ScreenerMetricContractTests(unittest.TestCase):
    def contract(self):
        contract = getattr(trader_stats, "SCREENER_METRIC_CONTRACT", None)
        self.assertIsInstance(
            contract, dict,
            "trader_stats.py must export SCREENER_METRIC_CONTRACT",
        )
        return contract

    def test_contract_covers_every_current_screener_metric(self):
        self.assertEqual(EXPECTED_METRICS, set(self.contract()))

    def test_every_metric_has_complete_machine_readable_provenance(self):
        for metric, metadata in self.contract().items():
            with self.subTest(metric=metric):
                self.assertEqual(set(), REQUIRED_FIELDS - set(metadata))
                for field in REQUIRED_FIELDS - {
                    "safe_for_sorting", "safe_for_filtering"
                }:
                    self.assertTrue(metadata[field], f"{metric}.{field} is empty")
                self.assertIs(type(metadata["safe_for_sorting"]), bool)
                self.assertIs(type(metadata["safe_for_filtering"]), bool)
                self.assertIn(
                    metadata["provenance"],
                    {"official", "reconstructed", "official_with_reconstructed_fallback"},
                )

    def test_limited_sources_never_claim_lifetime_or_all_time_coverage(self):
        contract = self.contract()
        limited = EXPECTED_METRICS - {"total_pnl"}
        for metric in limited:
            with self.subTest(metric=metric):
                claims = " ".join(
                    str(contract[metric][field]).lower()
                    for field in ("formula", "time_window", "tooltip")
                )
                self.assertNotIn("lifetime", claims)
                self.assertNotIn("all-time", claims)
                self.assertNotIn("all time", claims)

    def test_missing_values_are_unavailable_not_numeric_zero(self):
        for metric, metadata in self.contract().items():
            with self.subTest(metric=metric):
                behavior = metadata["null_behavior"]
                self.assertEqual("null", behavior["value"])
                self.assertTrue(behavior["reason"])
                self.assertNotEqual(0, behavior["value"])

    def test_null_contract_factually_records_current_unsafe_consumers(self):
        reason = self.contract()["total_pnl"]["null_behavior"]["reason"].lower()
        self.assertIn("cache", reason)
        self.assertIn("tradercard", reason)
        self.assertIn("tier", reason)
        self.assertIn("zero", reason)
        self.assertIn("unsafe", reason)

    def test_contract_records_real_fetch_budgets_and_refresh_schedule(self):
        contract = self.contract()
        for metric in EXPECTED_METRICS:
            with self.subTest(metric=metric):
                cadence = contract[metric]["refresh_cadence"]
                self.assertEqual(900, cadence["default_interval_seconds"])
                self.assertEqual(200, cadence["default_batch_size"])
                self.assertTrue(cadence["stale_first_rotation"])
                self.assertTrue(cadence["scheduled_enabled_by_default"])
                self.assertTrue(cadence["runs_immediately_on_startup"])
                self.assertEqual("TRADER_STATS_REFRESH_SECONDS", cadence["interval_env_var"])
                self.assertEqual("TRADER_STATS_REFRESH_LIMIT", cadence["batch_size_env_var"])
                self.assertTrue(cadence["interval_and_batch_configurable"])
                self.assertEqual("STATS_REFRESH_AUTOSTART", cadence["autostart_env_var"])
                self.assertEqual("0", cadence["disable_value"])
                self.assertEqual("GET /traders/{address}", cadence["on_demand_refresh_endpoint"])
                self.assertTrue(cadence["on_demand_refreshes_immediately"])

        # Limits are metric-specific: metadata must not imply that an endpoint
        # contributes to a metric merely because the refresh also calls it.
        for metric, metadata in contract.items():
            endpoints = json.dumps(metadata["source_endpoint"])
            limits = metadata["row_page_limits"]
            with self.subTest(metric=metric, detail="source limits"):
                if "type=TRADE" in endpoints:
                    self.assertEqual(1000, limits["activity_page_size"])
                    self.assertEqual(4, limits["trade_max_pages"])
                if "type=REDEEM" in endpoints:
                    self.assertEqual(1000, limits["activity_page_size"])
                    self.assertEqual(2, limits["redeem_max_pages"])
                if "/positions" in endpoints:
                    self.assertEqual(500, limits["positions_limit"])

        self.assertNotIn("type=REDEEM", json.dumps(contract["open_positions"]["source_endpoint"]))
        self.assertNotIn("type=REDEEM", json.dumps(contract["volume_30d"]["source_endpoint"]))
        self.assertNotIn("/positions", json.dumps(contract["fills_7d"]["source_endpoint"]))

    def test_total_pnl_sources_are_structured_exact_endpoints_with_matching_limits(self):
        metadata = self.contract()["total_pnl"]
        self.assertEqual(
            (
                "GET https://data-api.polymarket.com/v1/leaderboard?category=OVERALL&timePeriod=ALL&orderBy=PNL&user={address}",
                "GET https://data-api.polymarket.com/activity?user={address}&type=TRADE",
                "GET https://data-api.polymarket.com/activity?user={address}&type=REDEEM",
                "GET https://data-api.polymarket.com/positions?user={address}",
            ),
            metadata["source_endpoint"],
        )
        self.assertEqual(
            {
                "official_requests": 1,
                "official_request_row_limit": None,
                "official_rows_used": 1,
                "activity_page_size": 1000,
                "trade_max_pages": 4,
                "redeem_max_pages": 2,
                "positions_limit": 500,
            },
            metadata["row_page_limits"],
        )

    def test_partial_behavior_names_only_the_metric_sources_that_can_truncate(self):
        contract = self.contract()
        self.assertEqual(["positions"], contract["open_positions"]["partial_behavior"]["truncation_risks"])
        self.assertEqual(["TRADE"], contract["volume_30d"]["partial_behavior"]["truncation_risks"])
        self.assertEqual(
            ["TRADE", "REDEEM", "positions"],
            contract["winrate_30d"]["partial_behavior"]["truncation_risks"],
        )
        self.assertEqual([], contract["stats_refreshed_at"]["partial_behavior"]["truncation_risks"])

        for metric, metadata in contract.items():
            endpoints = json.dumps(metadata["source_endpoint"])
            risks = metadata["partial_behavior"]["truncation_risks"]
            with self.subTest(metric=metric):
                self.assertEqual("type=TRADE" in endpoints, "TRADE" in risks)
                self.assertEqual("type=REDEEM" in endpoints, "REDEEM" in risks)
                self.assertEqual("/positions" in endpoints, "positions" in risks)

    def test_sort_and_filter_safety_matches_the_implemented_whitelists(self):
        sortable = set(trader_stats._SORT_COLS.values())
        filterable = set(trader_stats._FILTERABLE_COLUMNS)
        for metric, metadata in self.contract().items():
            with self.subTest(metric=metric):
                self.assertEqual(metric in sortable, metadata["safe_for_sorting"])
                self.assertEqual(metric in filterable, metadata["safe_for_filtering"])

    def test_metric_entries_do_not_share_mutable_nested_metadata(self):
        first = self.contract()["win_rate"]
        second = self.contract()["pnl_quality"]
        for field in ("row_page_limits", "refresh_cadence", "null_behavior", "partial_behavior"):
            with self.subTest(field=field):
                self.assertIsNot(first[field], second[field])

    def test_contract_discloses_legacy_zero_transition(self):
        transition = self.contract()["win_rate"]["legacy_transition"]
        self.assertIn("indistinguishable", transition.lower())
        self.assertIn("refreshed", transition.lower())

    def test_contract_contains_no_copyability_score(self):
        serialized = json.dumps(self.contract()).lower()
        self.assertNotIn("copyability", serialized)
        self.assertNotIn("copyability_score", self.contract())

    def test_official_request_has_no_explicit_row_limit_and_uses_first_row(self):
        for metric in ("total_pnl", "volume_usd"):
            metadata = self.contract()[metric]
            with self.subTest(metric=metric):
                self.assertIsNone(metadata["row_page_limits"]["official_request_row_limit"])
                self.assertEqual(1, metadata["row_page_limits"]["official_rows_used"])
                self.assertIn("first", metadata["formula"].lower())

    def test_contract_exactly_describes_realized_closing_classification(self):
        text = " ".join(
            self.contract()["win_rate"][field]
            for field in ("formula", "time_window", "tooltip")
        ).lower()
        for fact in (
            "known basis", "held shares > 0", "min(sell size, held shares)",
            "sell price > average cost", "known cost > 0", "size > 0.01",
            "cur_price >= 0.5",
            "shares > 0.01", "cost > 0.005", "positions list is not truncated",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, text)

    def test_consistency_contract_documents_zero_variance_branch(self):
        formula = self.contract()["consistency_score"]["formula"].lower()
        self.assertIn("sample stdev == 0", formula)
        self.assertIn("component is 0", formula)

    def test_daily_pnl_contract_documents_inclusive_cutoff(self):
        metadata = self.contract()["daily_pnl_90d"]
        text = f'{metadata["formula"]} {metadata["time_window"]}'.lower()
        self.assertIn("inclusive", text)
        self.assertIn("91", text)

    def test_human_docs_record_current_consumer_limitations_and_exact_boundaries(self):
        docs = (Path(__file__).parents[1] / "docs/screener-metric-contract.md").read_text().lower()
        for fact in (
            "tradercard", "tier", "unsafe", "no explicit row limit", "first returned row",
            "min(sell size, held shares)", "known cost", "size > 0.01",
            "cur_price >= 0.5", "shares > 0.01", "cost > 0.005",
            "positions list is not truncated", "91 utc date labels",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, docs)

    def test_human_docs_truthfully_describe_fallback_failure_and_cached_provenance(self):
        docs = (Path(__file__).parents[1] / "docs/screener-metric-contract.md").read_text().lower()
        for fact in (
            "official lookup fails", "preserves", "does not reconstruct",
            "unknown/legacy provenance", "stats_refresh_autostart=0",
            "get /traders/{address}",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, docs)


class MissingMetricBehaviorTests(unittest.TestCase):
    def test_win_rate_without_closings_is_unavailable_not_zero(self):
        self.assertIsNone(trader_stats.win_rate_of([]))

    def test_undefined_windowed_ratios_are_unavailable_not_zero(self):
        metrics = trader_stats._period_metrics([], [], 7)
        self.assertIsNone(metrics["winrate"])
        self.assertIsNone(metrics["consistency_ratio"])
        self.assertIsNone(metrics["fill_exit_ratio"])


class LeaderboardSeedProvenanceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def entry():
        return SimpleNamespace(
            proxy_wallet="0xabc", user_name="alice", profile_image="img",
            x_username="alice_x", verified=True, pnl=123.0, vol=456.0,
        )

    async def test_default_month_seed_never_caches_period_totals_as_all_totals(self):
        pm = SimpleNamespace(get_leaderboard=AsyncMock(return_value=[self.entry()]))
        with patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert:
            count = await trader_stats.seed_from_leaderboard(object(), pm)

        self.assertEqual(1, count)
        pm.get_leaderboard.assert_awaited_once_with(
            period="MONTH", order_by="PNL", limit=50,
        )
        fields = upsert.await_args.args[2]
        self.assertNotIn("total_pnl", fields)
        self.assertNotIn("volume_usd", fields)

    async def test_all_seed_may_cache_official_all_period_totals(self):
        pm = SimpleNamespace(get_leaderboard=AsyncMock(return_value=[self.entry()]))
        with patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert:
            await trader_stats.seed_from_leaderboard(object(), pm, period="ALL")

        fields = upsert.await_args.args[2]
        self.assertEqual(123.0, fields["total_pnl"])
        self.assertEqual(456.0, fields["volume_usd"])


class OfficialLookupFallbackTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def pm(official_result):
        pm = SimpleNamespace(
            get_trade_history=AsyncMock(), get_redeems=AsyncMock(),
            get_positions=AsyncMock(return_value=[]),
            get_leaderboard_user=AsyncMock(),
        )
        if isinstance(official_result, Exception):
            pm.get_leaderboard_user.side_effect = official_result
        else:
            pm.get_leaderboard_user.return_value = official_result
        return pm

    async def refresh_fields(self, official_result, existing):
        async def fetchone(sql, _params):
            if "SELECT *" in sql:
                return {"address": "0xabc"}
            return existing

        db = SimpleNamespace(fetchone=AsyncMock(side_effect=fetchone))
        pm = self.pm(official_result)
        with (
            patch.object(trader_stats, "_fetch_activity_window", new=AsyncMock(side_effect=[([], True), ([], True)])),
            patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert,
        ):
            await trader_stats.refresh_trader_stats("0xabc", db, pm)
        return upsert.await_args.args[2]

    async def test_official_lookup_exception_preserves_totals_even_when_cache_is_null(self):
        fields = await self.refresh_fields(RuntimeError("upstream unavailable"), None)
        self.assertNotIn("total_pnl", fields)
        self.assertNotIn("volume_usd", fields)

    async def test_explicit_no_row_allows_reconstructed_fallback_for_missing_totals(self):
        fields = await self.refresh_fields(None, None)
        self.assertEqual(0.0, fields["total_pnl"])
        self.assertEqual(0.0, fields["volume_usd"])

    async def test_explicit_no_row_preserves_existing_values_of_unknown_provenance(self):
        fields = await self.refresh_fields(None, {"total_pnl": 12.0, "volume_usd": 34.0})
        self.assertNotIn("total_pnl", fields)
        self.assertNotIn("volume_usd", fields)


class DeterministicMetricFixtureTests(unittest.TestCase):
    def period_metrics(self, closings, trades, days=7):
        with patch.object(trader_stats.time, "time", return_value=activity.NOW_TS):
            return trader_stats._period_metrics(closings, trades, days)

    def test_sell_buy_event_ratio_is_not_a_capital_close_rate(self):
        trades = activity.ONE_LARGE_BUY_SEVERAL_SMALL_SELLS
        metrics = self.period_metrics([], trades)
        self.assertEqual(1, metrics["fills"])
        self.assertEqual(4, metrics["exits"])
        self.assertEqual(400.0, metrics["fill_exit_ratio"])
        capital_close_rate = sum(t.size for t in trades if t.side == "SELL") / trades[0].size
        self.assertEqual(0.004, capital_close_rate)
        self.assertNotEqual(capital_close_rate * 100, metrics["fill_exit_ratio"])
        contract = trader_stats.SCREENER_METRIC_CONTRACT["fill_exit_ratio_7d"]
        self.assertIn("row count", contract["formula"].lower())
        self.assertIn("not an order, position, share, or capital close rate", contract["tooltip"].lower())
        period_docs = " ".join((trader_stats._period_metrics.__doc__ or "").lower().split())
        self.assertIn("sell activity-row count / buy activity-row count", period_docs)
        self.assertIn("not a share, capital, or position close rate", period_docs)
        self.assertNotIn("how much of what a trader opens", period_docs)

    def test_sell_with_basis_older_than_fetched_history_is_not_fabricated(self):
        closings = trader_stats.realized_closings(
            activity.OLD_BUY_BASIS_OUTSIDE_FETCH, positions=[], positions_truncated=True,
        )
        self.assertEqual([], closings)
        self.assertIsNone(trader_stats.win_rate_of(closings))

    def test_partial_sell_realizes_only_sold_shares_at_average_cost(self):
        closings = trader_stats.realized_closings(
            activity.PARTIAL_SELL_TRADES,
            positions=[activity.PARTIAL_SELL_REMAINDER],
        )
        self.assertEqual(activity.day_ago(4), closings[0][0])
        self.assertAlmostEqual(0.8, closings[0][1])
        self.assertTrue(closings[0][2])

    def test_redeem_uses_known_condition_cost_basis(self):
        closings = trader_stats.realized_closings(
            activity.REDEEM_TRADES, activity.REDEEM_EVENTS, positions=[],
        )
        self.assertEqual([(activity.day_ago(6), 7.0, True)], closings)

    def test_resolved_holding_without_true_date_is_not_invented_into_windows(self):
        with patch.object(trader_stats.time, "time", return_value=activity.NOW_TS):
            closings = trader_stats.realized_closings(
                [], positions=[activity.RESOLVED_HOLDING_WITHOUT_RESOLUTION_DATE],
            )
        self.assertEqual([(None, 12, True)], closings)
        self.assertEqual(1.0, trader_stats.win_rate_of(closings))
        self.assertEqual({}, trader_stats.daily_realized_pnl(closings))
        metrics = self.period_metrics(closings, [])
        self.assertEqual(0.0, metrics["pnl"])
        self.assertIsNone(metrics["winrate"])

    def test_consistency_requires_seven_observed_days(self):
        six = [pnl for _, pnl, _ in activity.SIX_PROFITABLE_CLOSING_DAYS]
        seven = [pnl for _, pnl, _ in activity.SEVEN_PROFITABLE_CLOSING_DAYS]
        self.assertEqual(0.0, trader_stats.consistency_score(six))
        self.assertGreater(trader_stats.consistency_score(seven), 0.0)

    def test_flat_days_are_neither_green_nor_red(self):
        metrics = self.period_metrics(activity.FLAT_AND_DIRECTIONAL_DAYS, [])
        self.assertEqual(1, metrics["green_days"])
        self.assertEqual(1, metrics["red_days"])
        self.assertEqual(0.5, metrics["consistency_ratio"])

    def test_partial_coverage_does_not_turn_30d_or_90d_into_all_time(self):
        history_days = 20.0
        self.assertGreaterEqual(history_days, 7)
        self.assertLess(history_days, 30)
        for window in ("30d", "90d"):
            for stem in ("pnl", "winrate", "volume"):
                metadata = trader_stats.SCREENER_METRIC_CONTRACT[f"{stem}_{window}"]
                claims = " ".join(str(metadata[k]).lower()
                                  for k in ("formula", "time_window", "label", "tooltip"))
                self.assertEqual("partial", metadata["partial_behavior"]["status"])
                self.assertNotIn("all-time", claims)
                self.assertNotIn("all time", claims)
                self.assertNotIn("lifetime", claims)

    def test_30d_and_90d_boundaries_exclude_older_fetched_events(self):
        trades = activity.OLDER_AND_BOUNDARY_TRADES
        closings = trader_stats.realized_closings(
            trades, positions=[], positions_truncated=True,
        )

        metrics_30d = self.period_metrics(closings, trades, days=30)
        self.assertEqual(4.0, metrics_30d["pnl"])
        self.assertEqual(1.0, metrics_30d["winrate"])
        self.assertEqual(16.0, metrics_30d["volume"])

        metrics_90d = self.period_metrics(closings, trades, days=90)
        self.assertEqual(4.0, metrics_90d["pnl"])
        self.assertEqual(0.75, metrics_90d["winrate"])
        self.assertEqual(36.0, metrics_90d["volume"])


class DeterministicRefreshFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def refresh_fields(self, *, official, trades, redeems=(), positions=(),
                             trades_covered=True):
        async def fetchone(sql, _params):
            if "SELECT *" in sql:
                return {"address": "0xfixture"}
            return None

        db = SimpleNamespace(fetchone=AsyncMock(side_effect=fetchone))
        pm = SimpleNamespace(
            get_positions=AsyncMock(return_value=list(positions)),
            get_leaderboard_user=AsyncMock(return_value=official),
        )
        with (
            patch.object(
                trader_stats, "_fetch_activity_window",
                new=AsyncMock(side_effect=[
                    (list(trades), trades_covered), (list(redeems), True),
                ]),
            ),
            patch.object(trader_stats.time, "time", return_value=activity.NOW_TS),
            patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert,
        ):
            await trader_stats.refresh_trader_stats("0xfixture", db, pm)
        return upsert.await_args.args[2]

    async def test_official_leaderboard_pnl_wins_over_local_reconstruction(self):
        fields = await self.refresh_fields(
            official=activity.OFFICIAL_LEADERBOARD_PNL,
            trades=activity.PARTIAL_SELL_TRADES,
            positions=[activity.PARTIAL_SELL_REMAINDER],
        )
        self.assertEqual(activity.OFFICIAL_LEADERBOARD_PNL.pnl, fields["total_pnl"])
        self.assertEqual(activity.OFFICIAL_LEADERBOARD_PNL.vol, fields["volume_usd"])

    async def test_absent_official_pnl_uses_only_fetch_bounded_reconstruction(self):
        trades = [
            *activity.OLD_BUY_BASIS_OUTSIDE_FETCH,
            *activity.PARTIAL_SELL_TRADES,
            *activity.REDEEM_TRADES,
        ]
        fields = await self.refresh_fields(
            official=activity.OFFICIAL_PNL_ABSENT, trades=trades,
            redeems=activity.REDEEM_EVENTS,
            positions=[activity.PARTIAL_SELL_REMAINDER],
        )
        self.assertEqual(7.8, fields["total_pnl"])
        self.assertEqual(round(sum(t.usd_size for t in trades), 2), fields["volume_usd"])
        self.assertNotEqual(activity.OFFICIAL_LEADERBOARD_PNL.pnl, fields["total_pnl"])

    async def test_undated_resolved_holding_stays_in_fallback_but_out_of_windows(self):
        fields = await self.refresh_fields(
            official=activity.OFFICIAL_PNL_ABSENT,
            trades=[],
            positions=[activity.RESOLVED_HOLDING_WITHOUT_RESOLUTION_DATE],
        )
        self.assertEqual(12.0, fields["total_pnl"])
        self.assertEqual(1.0, fields["win_rate"])
        for window in ("7d", "30d", "90d"):
            self.assertEqual(0.0, fields[f"pnl_{window}"])
            self.assertIsNone(fields[f"winrate_{window}"])

    async def test_history_days_exposes_page_budget_limited_30d_and_90d_coverage(self):
        fields = await self.refresh_fields(
            official=activity.OFFICIAL_PNL_ABSENT,
            trades=activity.PARTIAL_30D_90D_TRADES,
            trades_covered=False,
        )
        self.assertEqual(20.0, fields["history_days"])

    async def test_fetched_aggregate_can_include_events_older_than_rolling_windows(self):
        fields = await self.refresh_fields(
            official=activity.OFFICIAL_PNL_ABSENT,
            trades=activity.OLDER_AND_BOUNDARY_TRADES,
        )
        self.assertEqual(2.0, fields["total_pnl"])
        self.assertEqual(0.6, fields["win_rate"])
        self.assertEqual(50.0, fields["volume_usd"])
        self.assertEqual(4.0, fields["pnl_30d"])
        self.assertEqual(1.0, fields["winrate_30d"])
        self.assertEqual(16.0, fields["volume_30d"])
        self.assertEqual(4.0, fields["pnl_90d"])
        self.assertEqual(0.75, fields["winrate_90d"])
        self.assertEqual(36.0, fields["volume_90d"])


class ScreenerMetricDocsTests(unittest.TestCase):
    def test_undated_resolved_holding_is_aggregate_only(self):
        docs = (Path(__file__).parents[1] / "docs/screener-metric-contract.md").read_text().lower()
        self.assertIn("missing fetched trade timestamp", docs)
        self.assertIn("remains undated", docs)
        self.assertIn("excluded from daily and rolling windows", docs)
        self.assertIn("aggregate observed/fallback pnl and win rate", docs)


if __name__ == "__main__":
    unittest.main()
