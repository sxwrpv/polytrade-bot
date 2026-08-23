"""Contracts for the public, read-only wallet screener API.

The authenticated screener at /api/traders/* is deliberately session-gated:
leaving it open handed out unmetered database reads, and on /{address} three to
eight upstream calls plus cache writes, to anyone who found the URL. The
standalone screener therefore gets its own namespace with a much narrower
promise — precomputed cache columns only, no upstream fan-out, no writes — and
these tests hold that line.
"""
from __future__ import annotations

import inspect
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api import routes_public_screener
from backend.api.deps import get_db
from backend.db.database import Database, now_iso
from backend.main import app


WALLET = "0x" + "a1" * 20
OTHER = "0x" + "b2" * 20
UNCACHED = "0x" + "c3" * 20


class PublicScreenerTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=str(Path(self.tmp.name) / "screener.db"), dsn="")
        await self.db.connect()
        await self.db.init()
        await self.db.execute(
            "INSERT INTO trader_cache(address, display_name, x_username, verified, "
            "pnl_30d, winrate_30d, volume_30d, pnl_7d, open_positions, history_days, "
            "consistency_ratio_30d, fill_exit_ratio_30d, "
            "stats_refreshed_at, last_refreshed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (WALLET, "Alpha", "alpha_x", 1, 1234.5, 0.62, 90000.0, 12.0, 4, 30.0,
             0.75, 90.0, now_iso(), now_iso()))
        await self.db.execute(
            "INSERT INTO trader_cache(address, display_name, pnl_30d, history_days, "
            "last_refreshed) VALUES(?,?,?,?,?)",
            (OTHER, "Partial", None, 6.0, now_iso()))
        app.dependency_overrides[get_db] = lambda: self.db
        routes_public_screener.reset_rate_limits()
        self.client = TestClient(app)

    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_db, None)
        routes_public_screener.reset_rate_limits()
        await self.db.close()
        self.tmp.cleanup()


class PublicAccessTests(PublicScreenerTestBase):
    def test_wallet_list_is_readable_without_a_session(self):
        response = self.client.get("/api/public/screener/wallets?period=30d")

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("30d", body["period"])
        self.assertTrue(any(w["address"] == WALLET for w in body["wallets"]))

    def test_single_wallet_is_readable_without_a_session(self):
        response = self.client.get(f"/api/public/screener/wallets/{WALLET}")

        self.assertEqual(200, response.status_code)
        self.assertEqual(WALLET, response.json()["address"])

    def test_authenticated_trader_routes_keep_requiring_a_session(self):
        for path in (f"/api/traders/{WALLET}", "/api/traders/following"):
            with self.subTest(path=path):
                self.assertEqual(401, self.client.get(path).status_code)


class ReadOnlyTests(PublicScreenerTestBase):
    async def test_public_reads_never_write_to_the_database(self):
        before = await self.db.fetchval("SELECT last_refreshed FROM trader_cache WHERE address=?",
                                        (WALLET,))
        self.client.get("/api/public/screener/wallets")
        self.client.get(f"/api/public/screener/wallets/{WALLET}")
        self.client.get(f"/api/public/screener/wallets/{UNCACHED}")
        after = await self.db.fetchval("SELECT last_refreshed FROM trader_cache WHERE address=?",
                                       (WALLET,))

        self.assertEqual(before, after)
        self.assertEqual(2, await self.db.fetchval("SELECT COUNT(*) FROM trader_cache"))

    def test_public_routes_never_reach_upstream_polymarket(self):
        # The contract is structural, not textual: no route on this router may
        # depend on the Polymarket client, and the module must not import any
        # helper that fans out upstream or writes the cache.
        import ast

        tree = ast.parse(Path(routes_public_screener.__file__).read_text())
        imported = {
            alias.name.split(".")[-1]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        for forbidden in ("get_pm", "trader_stats", "polymarket", "PolymarketClient",
                          "wallet", "execution"):
            self.assertNotIn(forbidden, imported, forbidden)

        for route in routes_public_screener.router.routes:
            params = inspect.signature(route.endpoint).parameters
            for name, param in params.items():
                default = param.default
                dependency = getattr(default, "dependency", None)
                if dependency is None:
                    continue
                self.assertIs(
                    dependency, get_db,
                    f"{route.path} depends on {dependency!r} via {name}")

    def test_an_uncached_wallet_reports_absence_rather_than_fetching_it(self):
        response = self.client.get(f"/api/public/screener/wallets/{UNCACHED}")

        self.assertEqual(404, response.status_code)
        detail = response.json()["detail"]
        self.assertIn("not", detail.lower())
        # It must not invent a zeroed wallet to fill the gap.
        self.assertNotIn("0.0", detail)


class TruthfulnessTests(PublicScreenerTestBase):
    def test_missing_metrics_stay_null_and_are_never_coerced_to_zero(self):
        wallet = self.client.get(f"/api/public/screener/wallets/{OTHER}").json()

        self.assertIsNone(wallet["pnl"])
        self.assertIsNone(wallet["win_rate"])
        self.assertIsNone(wallet["volume"])
        # open_positions is only meaningful once stats were actually computed.
        self.assertIsNone(wallet["active_positions"])

    def test_history_coverage_and_provenance_travel_with_every_wallet(self):
        body = self.client.get("/api/public/screener/wallets?period=30d").json()
        wallet = next(w for w in body["wallets"] if w["address"] == WALLET)
        partial = next(w for w in body["wallets"] if w["address"] == OTHER)

        self.assertEqual(30, wallet["period_days"])
        self.assertEqual(30.0, wallet["history_days"])
        self.assertFalse(wallet["history_partial"])
        self.assertTrue(partial["history_partial"])
        self.assertIsNotNone(wallet["stats_refreshed_at"])
        self.assertIsNone(partial["stats_refreshed_at"])
        self.assertIn("source", body["provenance"])

    def test_metrics_are_period_aware(self):
        seven = self.client.get(f"/api/public/screener/wallets/{WALLET}?period=7d").json()
        thirty = self.client.get(f"/api/public/screener/wallets/{WALLET}?period=30d").json()

        self.assertEqual(12.0, seven["pnl"])
        self.assertEqual(7, seven["period_days"])
        self.assertEqual(1234.5, thirty["pnl"])

    def test_an_unsupported_period_is_rejected_rather_than_silently_defaulted(self):
        self.assertEqual(422, self.client.get(
            "/api/public/screener/wallets?period=1y").status_code)

    def test_no_opaque_composite_score_is_published(self):
        wallet = self.client.get(f"/api/public/screener/wallets/{WALLET}").json()

        for opaque in ("copyability", "score", "consistency_score", "tier", "rating", "grade"):
            self.assertNotIn(opaque, wallet, opaque)


class PrivacyTests(PublicScreenerTestBase):
    async def test_no_polytrade_user_state_is_exposed(self):
        await self.db.execute(
            "INSERT INTO users(id, telegram_user_id, display_name, private_key_enc, created_at) "
            "VALUES(?,?,?,?,?)",
            ("0x" + "d4" * 20, 987654, "A PolyTrade user", "enc", now_iso()))
        await self.db.execute(
            "INSERT INTO followed_traders(id, user_id, trader_address, created_at) "
            "VALUES(?,?,?,?)", ("f1", "0x" + "d4" * 20, WALLET, now_iso()))

        payload = self.client.get("/api/public/screener/wallets").text
        payload += self.client.get(f"/api/public/screener/wallets/{WALLET}").text

        for leak in ("d4d4d4", "987654", "private_key", "a polytrade user", "followers",
                     "follower_count", "is_following", "balance", "telegram"):
            self.assertNotIn(leak, payload.lower(), leak)

    async def test_daily_series_is_trimmed_to_the_requested_window(self):
        """A 7d response must not carry 90 days of points. Labelling one
        window's data with another window's heading is the failure mode."""
        from datetime import date, timedelta
        today = date.today()
        blob = {(today - timedelta(days=n)).isoformat(): float(n) for n in range(90)}
        import json as _json
        await self.db.execute(
            "UPDATE trader_cache SET daily_pnl_90d = ? WHERE address = ?",
            (_json.dumps(blob), WALLET))

        for period, expected in (("7d", 7), ("30d", 30), ("90d", 90)):
            got = self.client.get(
                f"/api/public/screener/wallets/{WALLET}?period={period}").json()["daily_pnl"]
            self.assertEqual(expected, len(got), period)
            self.assertEqual(sorted(p["date"] for p in got), [p["date"] for p in got],
                             "series must be date-ordered")

    async def test_daily_series_is_absent_not_empty_when_uncomputed(self):
        """None and [] mean different things: not computed yet, versus computed
        and genuinely had no closing days. The wallet must not claim the second
        when the first is true."""
        await self.db.execute(
            "UPDATE trader_cache SET daily_pnl_90d = NULL WHERE address = ?", (WALLET,))
        wallet = self.client.get(f"/api/public/screener/wallets/{WALLET}").json()
        self.assertIsNone(wallet["daily_pnl"])

    async def test_daily_series_survives_a_corrupt_blob(self):
        """A bad cache row must degrade to "unavailable", never 500 a public
        endpoint."""
        for junk in ("not json", "[1,2,3]", ""):
            await self.db.execute(
                "UPDATE trader_cache SET daily_pnl_90d = ? WHERE address = ?", (junk, WALLET))
            r = self.client.get(f"/api/public/screener/wallets/{WALLET}")
            self.assertEqual(200, r.status_code, junk)
            self.assertIsNone(r.json()["daily_pnl"], junk)

    def test_response_fields_are_an_explicit_allowlist(self):
        wallet = self.client.get(f"/api/public/screener/wallets/{WALLET}").json()

        self.assertEqual({
            "address", "display_name", "x_username", "verified",
            "period", "period_days", "pnl", "win_rate", "volume",
            "active_positions", "history_days", "history_partial",
            "consistency_ratio", "fill_exit_ratio", "stats_refreshed_at",
            # Added 2026-08-23 so the analysis panel can draw the wallet's
            # curve without a second request. It is derived from the already
            # cached daily_pnl_90d blob, so it costs no upstream call.
            "daily_pnl",
        }, set(wallet))


class AdvancedFilterTests(PublicScreenerTestBase):
    """The two filters carried over from the retired in-app screener.

    Both metrics were already computed, stored and indexed; only the public
    surface lacked a way to filter on them.
    """

    def addresses(self, query: str) -> list[str]:
        response = self.client.get(f"/api/public/screener/wallets?{query}")
        self.assertEqual(200, response.status_code, response.text)
        return [w["address"] for w in response.json()["wallets"]]

    def test_positive_close_day_ratio_filters_on_the_stored_fraction(self):
        # Stored as 0.75. The wire contract is the fraction, not the percent.
        self.assertIn(WALLET, self.addresses("period=30d&consistency_ratio_min=0.7"))
        self.assertNotIn(WALLET, self.addresses("period=30d&consistency_ratio_min=0.8"))

    def test_sell_buy_event_count_filters_on_a_percentage_band(self):
        # Stored as 90.0, meaning 90 SELL rows per 100 BUY rows.
        self.assertIn(WALLET, self.addresses(
            "period=30d&fill_exit_ratio_min=50&fill_exit_ratio_max=150"))
        self.assertNotIn(WALLET, self.addresses("period=30d&fill_exit_ratio_min=95"))
        self.assertNotIn(WALLET, self.addresses("period=30d&fill_exit_ratio_max=85"))

    def test_a_wallet_without_the_metric_is_excluded_rather_than_treated_as_zero(self):
        # OTHER has null for both. A null must not satisfy ">= 0".
        self.assertNotIn(OTHER, self.addresses("period=30d&consistency_ratio_min=0"))
        self.assertNotIn(OTHER, self.addresses("period=30d&fill_exit_ratio_min=0"))
        # Unfiltered, it is still listed — absence of a metric is not exclusion.
        self.assertIn(OTHER, self.addresses("period=30d"))

    def test_the_filters_are_period_aware(self):
        # The metrics were only ever written for the 30d window here, so the
        # same threshold must not match through a different period's column.
        self.assertNotIn(WALLET, self.addresses("period=7d&consistency_ratio_min=0.7"))
        self.assertNotIn(WALLET, self.addresses("period=7d&fill_exit_ratio_min=50"))

    def test_an_out_of_range_ratio_is_rejected_rather_than_clamped(self):
        # consistency_ratio is a 0..1 fraction; 70 is a percent typo, and
        # silently clamping it would return a list that ignores the filter.
        for query in ("consistency_ratio_min=70", "consistency_ratio_min=-1",
                      "fill_exit_ratio_min=-5"):
            with self.subTest(query=query):
                self.assertEqual(
                    422, self.client.get(f"/api/public/screener/wallets?{query}").status_code)

    def test_a_minimum_above_its_maximum_is_rejected(self):
        response = self.client.get(
            "/api/public/screener/wallets?fill_exit_ratio_min=300&fill_exit_ratio_max=100")

        self.assertEqual(422, response.status_code)
        self.assertIn("minimum", response.json()["detail"].lower())
        self.assertIn("maximum", response.json()["detail"].lower())

    def test_both_metrics_are_published_so_a_threshold_can_be_checked(self):
        wallet = self.client.get(f"/api/public/screener/wallets/{WALLET}").json()

        self.assertEqual(0.75, wallet["consistency_ratio"])
        self.assertEqual(90.0, wallet["fill_exit_ratio"])
        other = self.client.get(f"/api/public/screener/wallets/{OTHER}").json()
        self.assertIsNone(other["consistency_ratio"])
        self.assertIsNone(other["fill_exit_ratio"])

    def test_provenance_says_what_the_exit_fill_ratio_is_not(self):
        limitations = " ".join(
            self.client.get("/api/public/screener/provenance").json()["limitations"]).lower()

        self.assertIn("not an order, position, share, or capital close rate", limitations)
        self.assertIn("exactly zero", limitations)

    def test_filtering_still_reads_only_the_cache(self):
        # The advanced filters must not have introduced a write path.
        before = self.client.get(f"/api/public/screener/wallets/{WALLET}").json()
        self.addresses("period=30d&consistency_ratio_min=0.1&fill_exit_ratio_min=1")
        after = self.client.get(f"/api/public/screener/wallets/{WALLET}").json()

        self.assertEqual(before, after)


class WireContractTests(PublicScreenerTestBase):
    def test_every_key_the_screener_sends_is_a_parameter_this_route_accepts(self):
        """A renamed parameter fails silently: FastAPI ignores the unknown key
        and returns an unfiltered list that still looks correct. Nothing else
        in either suite would catch that, so pin the contract here."""
        model = (Path(__file__).parents[1]
                 / "frontend/src/screener/screenerModel.js").read_text()
        emitted = set(re.findall(r"query\.([a-z_]+)\s*=", model))
        emitted |= {"period", "sort", "limit"}  # set in the object literal
        accepted = set(inspect.signature(routes_public_screener.public_wallets).parameters)

        self.assertTrue(emitted, "no query keys parsed out of the screener model")
        self.assertLessEqual(emitted, accepted, emitted - accepted)

    def test_the_filters_actually_narrow_the_result(self):
        """Guards the same failure from the other side: an ignored parameter
        would leave the wallet in the list rather than filtering it out."""
        unfiltered = self.client.get("/api/public/screener/wallets?period=30d").json()
        self.assertIn(WALLET, [w["address"] for w in unfiltered["wallets"]])

        for query in ("consistency_ratio_min=0.9", "fill_exit_ratio_min=99",
                      "fill_exit_ratio_max=10"):
            with self.subTest(query=query):
                body = self.client.get(
                    f"/api/public/screener/wallets?period=30d&{query}").json()
                self.assertNotIn(WALLET, [w["address"] for w in body["wallets"]])


class PaginationTests(PublicScreenerTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for index in range(5):
            address = "0x" + f"{index + 16:02x}" * 20
            await self.db.execute(
                "INSERT INTO trader_cache(address, display_name, pnl_30d, history_days, "
                "last_refreshed) VALUES(?,?,?,?,?)",
                (address, f"Wallet {index}", float(100 - index), 30.0, now_iso()),
            )

    def test_list_reports_truthful_page_metadata(self):
        first = self.client.get(
            "/api/public/screener/wallets?period=30d&sort=pnl&limit=3&offset=0").json()
        second = self.client.get(
            "/api/public/screener/wallets?period=30d&sort=pnl&limit=3&offset=3").json()
        final = self.client.get(
            "/api/public/screener/wallets?period=30d&sort=pnl&limit=3&offset=6").json()

        self.assertEqual(7, first["total"])
        self.assertEqual(3, first["count"])
        self.assertEqual(3, first["limit"])
        self.assertEqual(0, first["offset"])
        self.assertTrue(first["has_more"])

        self.assertEqual(7, second["total"])
        self.assertEqual(3, second["count"])
        self.assertEqual(3, second["offset"])
        self.assertTrue(second["has_more"])

        self.assertEqual(7, final["total"])
        self.assertEqual(1, final["count"])
        self.assertEqual(6, final["offset"])
        self.assertFalse(final["has_more"])

        reached = {
            wallet["address"]
            for page in (first, second, final)
            for wallet in page["wallets"]
        }
        self.assertEqual(7, len(reached))

    def test_total_obeys_the_active_filters(self):
        body = self.client.get(
            "/api/public/screener/wallets?period=30d&limit=2&pnl_min=100").json()

        self.assertEqual(2, body["total"])
        self.assertEqual(2, body["count"])
        self.assertFalse(body["has_more"])

    def test_nonempty_page_and_total_come_from_one_query_snapshot(self):
        original_fetchval = self.db.fetchval

        async def unexpected_separate_count(*_args, **_kwargs):
            raise AssertionError("nonempty pages must not run a separate COUNT query")

        self.db.fetchval = unexpected_separate_count
        try:
            response = self.client.get(
                "/api/public/screener/wallets?period=30d&limit=2&offset=0")
        finally:
            self.db.fetchval = original_fetchval

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, response.json()["count"])
        self.assertEqual(7, response.json()["total"])


class RateLimitTests(PublicScreenerTestBase):
    def test_repeated_anonymous_requests_are_rate_limited_per_client(self):
        limit = routes_public_screener.PUBLIC_RATE_LIMIT
        codes = [self.client.get("/api/public/screener/wallets").status_code
                 for _ in range(limit + 5)]

        self.assertEqual(200, codes[0])
        self.assertIn(429, codes)
        self.assertEqual("429", str(codes[-1]))

    def test_the_limiter_does_not_store_wallet_addresses(self):
        self.client.get(f"/api/public/screener/wallets/{WALLET}")

        buckets = routes_public_screener.rate_limit_keys()
        for key in buckets:
            self.assertNotIn(WALLET.lower(), str(key).lower())


class TelemetryPrivacyTests(unittest.TestCase):
    def test_public_screener_adds_no_wallet_carrying_telemetry_events(self):
        from backend.api import routes_telemetry

        for allowed in routes_telemetry._EVENT_PROPERTIES.values():
            for prop in allowed:
                self.assertNotIn("address", prop)
                self.assertNotIn("wallet", prop)
