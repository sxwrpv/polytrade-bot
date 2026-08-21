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
            "stats_refreshed_at, last_refreshed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (WALLET, "Alpha", "alpha_x", 1, 1234.5, 0.62, 90000.0, 12.0, 4, 30.0,
             now_iso(), now_iso()))
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

    def test_authenticated_screener_keeps_requiring_a_session(self):
        for path in ("/api/traders/leaderboard", f"/api/traders/{WALLET}",
                     "/api/traders/following"):
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

    def test_response_fields_are_an_explicit_allowlist(self):
        wallet = self.client.get(f"/api/public/screener/wallets/{WALLET}").json()

        self.assertEqual({
            "address", "display_name", "x_username", "verified",
            "period", "period_days", "pnl", "win_rate", "volume",
            "active_positions", "history_days", "history_partial",
            "stats_refreshed_at",
        }, set(wallet))


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
