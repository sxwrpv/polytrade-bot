"""Readiness must be able to say what /api/health could not.

Between 30 Aug and 2 Sep, /api/health returned 200 on all 9,768 checks while
the copy engine placed zero orders, ran on the fallback detector because
POLYGON_RPC_URL was blank, and had its hardening pass throw on every boot.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from backend.core import health
from backend.core.health import Heartbeats, UpstreamCounters


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.hb = Heartbeats()

    def test_a_registered_loop_that_never_ran_is_stale(self):
        """The engine being absent must not read as 'fine, no news'."""
        self.hb.register("detect_tick", 2.0)
        snap = self.hb.snapshot()["detect_tick"]
        self.assertTrue(snap["stale"])
        self.assertIsNone(snap["last_success_age_seconds"])
        self.assertTrue(snap["registered"])

    def test_a_fresh_pass_is_not_stale(self):
        self.hb.register("detect_tick", 2.0)
        self.hb.mark("detect_tick")
        snap = self.hb.snapshot()["detect_tick"]
        self.assertFalse(snap["stale"])
        self.assertEqual(snap["successes"], 1)

    def test_staleness_budget_scales_with_the_loop_interval(self):
        self.hb.register("detect_tick", 2.0)          # fast loop
        self.hb.register("screener_refresh", 900.0)   # slow crawler
        snap = self.hb.snapshot()
        self.assertLess(snap["detect_tick"]["staleness_budget_seconds"],
                        snap["screener_refresh"]["staleness_budget_seconds"])

    def test_a_fast_loop_goes_stale_once_its_budget_passes(self):
        self.hb.register("detect_tick", 2.0)
        self.hb.mark("detect_tick")
        self.hb._marks["detect_tick"] = time.time() - 10_000
        self.assertTrue(self.hb.snapshot()["detect_tick"]["stale"])

    def test_counts_accumulate(self):
        self.hb.register("reconcile_tick", 5.0)
        for _ in range(3):
            self.hb.mark("reconcile_tick")
        self.assertEqual(self.hb.snapshot()["reconcile_tick"]["successes"], 3)


class UpstreamCounterTests(unittest.TestCase):
    def test_outcomes_are_counted_and_rated(self):
        c = UpstreamCounters()
        c.record(ok=True)
        c.record(rate_limited=True)
        c.record(server_error=True)
        c.record(transport_error=True)
        c.record(retry=True)
        snap = c.snapshot()
        self.assertEqual(snap["requests"], 1)
        self.assertEqual(snap["rate_limited"], 1)
        self.assertEqual(snap["server_errors"], 1)
        self.assertEqual(snap["transport_errors"], 1)
        self.assertEqual(snap["retries"], 1)
        self.assertGreater(snap["rate_limited_per_hour"], 0)


class ReadinessEndpointTests(unittest.TestCase):
    """Exercised against the real app, with the engine deliberately absent."""

    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["DB_PATH"] = cls.db_path
        os.environ["SEED_ON_START"] = "0"
        os.environ["COPY_ENGINE_AUTOSTART"] = "0"
        os.environ["STATS_REFRESH_AUTOSTART"] = "0"
        os.environ["EQUITY_SNAPSHOT_AUTOSTART"] = "0"
        os.environ.setdefault("ENCRYPTION_SECRET", "test-secret-for-readiness")
        from backend.main import app
        cls.app = app
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        os.unlink(cls.db_path)

    def test_health_is_unchanged(self):
        """Container healthchecks depend on this shape."""
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_ready_reports_the_database_and_the_loops(self):
        body = self.client.get("/api/ready").json()
        self.assertIn(body["status"], {"healthy", "degraded", "unhealthy"})
        self.assertTrue(body["checks"]["database"]["ok"])
        self.assertIn("loops", body["checks"])
        self.assertIn("upstream", body["checks"])

    def test_ready_reports_an_absent_copy_worker(self):
        """The engine is off in this fixture; readiness must say so rather
        than reporting a green process."""
        body = self.client.get("/api/ready").json()
        self.assertFalse(body["checks"]["copy_worker"]["present"])
        self.assertIsNone(body["checks"]["copy_worker"]["instance"])

    def test_ready_counts_claims_and_stuck_positions(self):
        checks = self.client.get("/api/ready").json()["checks"]
        self.assertEqual(checks["uncertain_claims"], 0)
        self.assertEqual(checks["positions_closing"], 0)
        self.assertEqual(checks["positions_reconciliation_required"], 0)

    def test_an_expected_but_missing_engine_is_unhealthy_with_503(self):
        os.environ["COPY_ENGINE_AUTOSTART"] = "1"
        try:
            r = self.client.get("/api/ready")
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json()["status"], "unhealthy")
        finally:
            os.environ["COPY_ENGINE_AUTOSTART"] = "0"

    def test_ready_leaks_no_wallet_or_secret(self):
        """It is reachable without a session, so it must carry no identifier."""
        raw = self.client.get("/api/ready").text
        self.assertNotIn("0x", raw)
        for forbidden in ("private_key", "api_token", "ENCRYPTION", "postgres://",
                          "postgresql://", "telegram"):
            self.assertNotIn(forbidden, raw)

    def test_version_reports_the_build(self):
        body = self.client.get("/api/version").json()
        self.assertIn("revision", body)
        self.assertIn("data_mode", body)
        self.assertEqual(body["data_mode"], "live")

    def test_version_needs_no_session(self):
        self.assertEqual(self.client.get("/api/version").status_code, 200)


if __name__ == "__main__":
    unittest.main()
