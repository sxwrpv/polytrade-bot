"""Privacy and storage contracts for product telemetry."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api.routes_telemetry import TelemetryEvent, record_event
from backend.core.telemetry import enforce_product_event_limits, prune_product_events
from backend.db.database import Database
from backend.db.models import PG_SCHEMA_SQL, SCHEMA_SQL, TABLES
from backend.main import app


SESSION = str(uuid.uuid4())


class ProductTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=str(Path(self.tmp.name) / "telemetry.db"), dsn="")
        await self.db.connect()
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    def test_schema_is_identity_free_on_sqlite_and_postgres(self):
        for schema in (SCHEMA_SQL, PG_SCHEMA_SQL):
            block = schema.split("CREATE TABLE IF NOT EXISTS product_events", 1)[1].split(");", 1)[0]
            self.assertIn("session_id", block)
            self.assertIn("event_name", block)
            self.assertNotIn("user_id", block)
            self.assertNotIn("wallet", block.lower())
            self.assertNotIn("ip_address", block.lower())
        self.assertIn("product_events", TABLES)

    def test_strict_event_and_property_validation(self):
        valid = TelemetryEvent(
            session_id=SESSION,
            event_name="period_changed",
            properties={"period": "7d", "source": "screener"},
        )
        self.assertEqual("7d", valid.properties["period"])
        bad_cases = [
            {"session_id": "not-a-uuid", "event_name": "period_changed", "properties": {"period": "7d"}},
            {"session_id": SESSION, "event_name": "wallet_dumped", "properties": {}},
            {"session_id": SESSION, "event_name": "period_changed", "properties": {"period": "all"}},
            {"session_id": SESSION, "event_name": "period_changed", "properties": {"period": "7d", "wallet": "0x" + "a" * 40}},
            {"session_id": SESSION, "event_name": "close_confirmed", "properties": {"duration_ms": 9_000_001}},
        ]
        for payload in bad_cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    TelemetryEvent(**payload)

    async def test_persisted_event_has_no_authenticated_identity_or_query(self):
        body = TelemetryEvent(
            session_id=SESSION,
            event_name="screener_search_submitted",
            properties={"query_kind": "address", "period": "30d", "active_filters": True},
        )
        result = await record_event(body, user={"id": "0x" + "f" * 40}, db=self.db)
        self.assertEqual({"accepted": True}, result)
        row = await self.db.fetchone("SELECT * FROM product_events")
        self.assertEqual(SESSION, row["session_id"])
        self.assertEqual("screener_search_submitted", row["event_name"])
        self.assertEqual(
            {"active_filters": True, "period": "30d", "query_kind": "address"},
            json.loads(row["properties_json"]),
        )
        self.assertNotIn("user_id", row)
        self.assertNotIn("0xffff", json.dumps(row).lower())

    async def test_retention_prunes_only_rows_older_than_90_days(self):
        old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        fresh = (datetime.now(timezone.utc) - timedelta(days=89)).isoformat()
        for event_id, ts in (("old", old), ("fresh", fresh)):
            await self.db.execute(
                "INSERT INTO product_events(id,session_id,event_name,properties_json,ts) VALUES(?,?,?,?,?)",
                (event_id, SESSION, "wallet_analysis_opened", "{}", ts),
            )
        removed = await prune_product_events(self.db, retention_days=90)
        self.assertEqual(1, removed)
        self.assertEqual(["fresh"], [r["id"] for r in await self.db.fetchall("SELECT id FROM product_events")])

    async def test_event_limits_rate_limit_and_keep_table_capacity_bounded(self):
        now = datetime.now(timezone.utc)
        for index in range(2):
            await self.db.execute(
                "INSERT INTO product_events(id,session_id,event_name,properties_json,ts) VALUES(?,?,?,?,?)",
                (f"recent-{index}", SESSION, "wallet_analysis_opened", "{}", now.isoformat()),
            )
        with self.assertRaises(HTTPException) as context:
            await enforce_product_event_limits(
                self.db, SESSION, now=now, session_limit=2, global_limit=20, max_rows=20,
            )
        self.assertEqual(429, context.exception.status_code)

        await self.db.execute("DELETE FROM product_events")
        old_ts = (now - timedelta(days=1)).isoformat()
        for index in range(3):
            await self.db.execute(
                "INSERT INTO product_events(id,session_id,event_name,properties_json,ts) VALUES(?,?,?,?,?)",
                (f"old-{index}", str(uuid.uuid4()), "wallet_analysis_opened", "{}", old_ts),
            )
        await enforce_product_event_limits(
            self.db, SESSION, now=now, session_limit=20, global_limit=20, max_rows=3,
        )
        self.assertEqual(2, await self.db.fetchval("SELECT COUNT(*) FROM product_events"))

    async def test_postgres_limits_take_transaction_scoped_advisory_lock_first(self):
        class FakePostgresTransaction:
            is_pg = True

            def __init__(self):
                self.calls = []

            async def fetchval(self, sql, params=()):
                self.calls.append(("fetchval", sql, params))
                return 0

            async def execute(self, sql, params=()):
                self.calls.append(("execute", sql, params))
                return 0

        tx = FakePostgresTransaction()
        await enforce_product_event_limits(tx, SESSION)
        self.assertEqual("SELECT pg_advisory_xact_lock(?)", tx.calls[0][1])
        self.assertEqual((824_705_311,), tx.calls[0][2])
        self.assertIn("session_id", tx.calls[1][1])

    def test_supabase_migration_locks_telemetry_behind_rls(self):
        migration = (Path(__file__).parents[1] / "supabase/migrations/0009_product_events.sql").read_text()
        self.assertIn("public.product_events", migration)
        self.assertIn("ENABLE ROW LEVEL SECURITY", migration)
        self.assertIn("REVOKE ALL", migration)
        self.assertIn("DROP POLICY IF EXISTS no_api_access", migration)
        self.assertIn("TO anon, authenticated USING (false) WITH CHECK (false)", migration)
        self.assertIn("session_id, ts", migration)

    def test_retention_runs_before_serving_and_then_hourly(self):
        import inspect
        from backend import main
        lifespan_source = inspect.getsource(main.lifespan)
        loop_source = inspect.getsource(main._telemetry_retention_loop)
        self.assertIn("await telemetry.prune_product_events(db, retention_days=90)", lifespan_source)
        self.assertIn("while not stop.is_set()", loop_source)
        self.assertIn("prune_product_events", loop_source)
        self.assertIn("wait_for(stop.wait()", loop_source)
        self.assertIn("60 * 60", loop_source)

    def test_route_requires_authentication_dependency(self):
        import inspect
        source = inspect.getsource(record_event)
        self.assertIn("Depends(get_current_user)", source)
        self.assertIn("Depends(get_db)", source)

        response = TestClient(app).post("/api/telemetry/events", json={
            "session_id": SESSION,
            "event_name": "wallet_analysis_opened",
            "properties": {"period": "30d", "source": "screener"},
        })
        self.assertEqual(401, response.status_code)
        self.assertEqual("missing session token", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
