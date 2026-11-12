"""Regressions for the 2026-11-10 review, platform area: startup and access
logging, the equity snapshot balance read, the by-wallet PnL query and the
Postgres boot schema.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from polymarket import errors as pm_errors

from backend import config, main
from backend.core import equity as equity_mod
from backend.core import pnl
from backend.db.database import Database, now_iso
from backend.db.models import PG_SCHEMA_SQL

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"

USER = "0x" + "1" * 40
OTHER = "0x" + "3" * 40
TRADER = "0x" + "2" * 40
SECOND_TRADER = "0x" + "4" * 40

# The call uvicorn's HTTP protocols make for every response
# (uvicorn/protocols/http/h11_impl.py and httptools_impl.py).
ACCESS_LINE = '%s - "%s %s HTTP/%s" %d'


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(path=str(Path(self.tmp.name) / "platform.db"), dsn="")
        await self.db.connect()
        await self.db.init()
        for uid in (USER, OTHER):
            await self.db.execute(
                "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
                (uid, "enc", now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    @contextlib.contextmanager
    def reads(self):
        """Note each fetchall / fetchone sent through self.db as (sql, params)."""
        seen: list[tuple[str, tuple]] = []
        fetchall, fetchone = self.db.fetchall, self.db.fetchone

        async def record_all(sql, params=()):
            seen.append((sql, tuple(params)))
            return await fetchall(sql, params)

        async def record_one(sql, params=()):
            seen.append((sql, tuple(params)))
            return await fetchone(sql, params)

        with patch.object(self.db, "fetchall", record_all), \
                patch.object(self.db, "fetchone", record_one):
            yield seen

    async def plan(self, sql: str, params: tuple) -> list[str]:
        """SQLite's plan for a statement, one line per step."""
        rows = await self.db.fetchall("EXPLAIN QUERY PLAN " + sql, params)
        return [row["detail"] for row in rows]


# ---------------------------------------------------------------------------
# LOG6: the health poll stays out of the access log
# ---------------------------------------------------------------------------
def access_record(method="GET", path="/api/health", status=200) -> logging.LogRecord:
    """An access record carrying the args uvicorn logs."""
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, ACCESS_LINE,
        ("127.0.0.1:51234", method, path, "1.1", status), None)


class HealthAccessLogTests(unittest.TestCase):
    def setUp(self):
        self.filter = main._HealthAccessFilter()

    def test_a_successful_health_poll_is_dropped(self):
        for status in (200, 204, 299, "200"):
            with self.subTest(status=status):
                self.assertFalse(self.filter.filter(access_record(status=status)))
        self.assertFalse(self.filter.filter(access_record(path="/api/health?probe=1")))

    def test_a_failing_health_check_still_logs(self):
        for status in (199, 301, 404, 500, 503):
            with self.subTest(status=status):
                self.assertTrue(self.filter.filter(access_record(status=status)))

    def test_every_other_request_still_logs(self):
        requests = (
            ("GET", "/api/ready"), ("GET", "/api/version"), ("GET", "/"),
            ("GET", "/api/healthz"), ("GET", "/api/health/deep"),
            ("GET", "/screener/api/health"), ("GET", "/api/user/me?next=/api/health"),
            ("HEAD", "/api/health"), ("POST", "/api/health"),
        )
        for method, path in requests:
            with self.subTest(method=method, path=path):
                self.assertTrue(self.filter.filter(access_record(method, path)))

    def test_a_record_of_another_shape_passes(self):
        shapes = (
            None,
            (),
            ("three", "args", "only"),
            ({"status": 200},),                                  # mapping args
            ("127.0.0.1:1", "GET", "/api/health", "1.1", "OK"),  # status without a number
            ("127.0.0.1:1", "GET", "/api/health", "1.1", None),
            ("127.0.0.1:1", "GET", b"/api/health", "1.1", 200),
            ("127.0.0.1:1", "GET", None, "1.1", 200),
            ("127.0.0.1:1", "GET", "/api/health", "1.1", 200, "extra"),
        )
        for args in shapes:
            with self.subTest(args=args):
                record = logging.LogRecord(
                    "uvicorn.access", logging.INFO, __file__, 0, "message", args, None)
                self.assertTrue(self.filter.filter(record))

    def test_the_filter_sits_on_the_uvicorn_access_logger(self):
        installed = [f for f in logging.getLogger("uvicorn.access").filters
                     if isinstance(f, main._HealthAccessFilter)]
        self.assertEqual(1, len(installed))

    def test_the_logger_drops_the_poll_and_keeps_the_rest(self):
        access = logging.getLogger("uvicorn.access")
        with self.assertLogs("uvicorn.access", level="INFO") as logs:
            access.info(ACCESS_LINE, "127.0.0.1:1", "GET", "/api/health", "1.1", 200)
            access.info(ACCESS_LINE, "127.0.0.1:1", "GET", "/api/ready", "1.1", 200)
            access.info(ACCESS_LINE, "127.0.0.1:1", "GET", "/api/health", "1.1", 503)
        self.assertEqual(
            ['127.0.0.1:1 - "GET /api/ready HTTP/1.1" 200',
             '127.0.0.1:1 - "GET /api/health HTTP/1.1" 503'],
            [record.getMessage() for record in logs.records])

    def test_the_filter_survives_uvicorn_logging_setup(self):
        """The image starts `uvicorn backend.main:app` (Dockerfile CMD): uvicorn
        configures logging, then imports the app. A child process repeats that
        order, writes three access lines through uvicorn's own handler and
        formatter, configures logging once more as uvicorn.run(app) would, and
        writes them again."""
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn('CMD ["uvicorn", "backend.main:app"', dockerfile)
        self.assertNotIn("--no-access-log", dockerfile)
        script = f'''
import io, logging
import uvicorn

config = uvicorn.Config("backend.main:app", host="0.0.0.0", port=8080, workers=1,
                        proxy_headers=True)
config.load()
access = logging.getLogger("uvicorn.access")


def written():
    out = io.StringIO()
    access.handlers[0].setStream(out)
    line = {ACCESS_LINE!r}
    access.info(line, "127.0.0.1:1", "GET", "/api/health", "1.1", 200)
    access.info(line, "127.0.0.1:1", "GET", "/api/ready", "1.1", 200)
    access.info(line, "127.0.0.1:1", "GET", "/api/health", "1.1", 503)
    return out.getvalue()


print(written(), end="")
print("--")
config.configure_logging()
print(written(), end="")
'''
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=120)
        self.assertEqual(0, done.returncode, done.stderr)
        runs = done.stdout.split("--\n")
        self.assertEqual(2, len(runs), done.stdout)
        for run in runs:
            lines = run.splitlines()
            self.assertEqual(2, len(lines), run)
            self.assertIn('"GET /api/ready HTTP/1.1" 200', lines[0])
            self.assertIn('"GET /api/health HTTP/1.1" 503', lines[1])


# ---------------------------------------------------------------------------
# LOG4: an unset POLYGON_RPC_URL is said once at startup
# ---------------------------------------------------------------------------
class _IdleEngine:
    """Stands in for CopyEngine: keeps what it was built with and waits."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def run(self, stop):
        await stop.wait()

    async def aclose(self):
        pass


class ClaimEvidenceStartupLogTests(unittest.TestCase):
    def boot(self, rpc_url: str, *, onchain_detection: bool = False,
             detector_error: Exception | None = None):
        """Start the app with the copy engine on. Returns what the engine was
        built with and everything logged meanwhile."""
        from backend.core import ctf, detection, leader_cash
        self.detector_cls = MagicMock(side_effect=detector_error)
        with patch.object(config, "POLYGON_RPC_URL", rpc_url), \
                patch.object(config, "ONCHAIN_DETECTION", onchain_detection), \
                patch.object(main, "CopyEngine", _IdleEngine), \
                patch.object(leader_cash, "LeaderCashReader", MagicMock()), \
                patch.object(detection, "OnChainDetector", self.detector_cls), \
                patch.object(ctf, "CtfReader", MagicMock()), \
                patch.dict(os.environ, {"COPY_ENGINE_AUTOSTART": "1"}), \
                self.assertLogs(level="INFO") as logs:
            try:
                with TestClient(main.app):
                    built = main.app.state.engine.kwargs
            finally:
                # app.state outlives the lifespan, and later boots run engine-off.
                if hasattr(main.app.state, "engine"):
                    del main.app.state.engine
        return built, logs

    def test_an_unset_rpc_url_is_warned_about_with_its_consequence(self):
        built, logs = self.boot("")
        self.assertIsNone(built["ctf"])
        warnings = [r for r in logs.records
                    if r.name == "main" and "POLYGON_RPC_URL" in r.getMessage()]
        self.assertEqual(1, len(warnings))
        self.assertEqual(logging.WARNING, warnings[0].levelno)
        message = warnings[0].getMessage()
        self.assertIn("uncertain BUY claims cannot settle on-chain", message)
        self.assertIn("until it is set", message)

    def test_a_set_rpc_url_brings_no_warning_and_stays_out_of_the_log(self):
        built, logs = self.boot("https://rpc.example.invalid/v2/key-0123456789")
        self.assertIsNotNone(built["ctf"])
        text = "\n".join(logs.output)
        self.assertNotIn("POLYGON_RPC_URL", text)
        self.assertNotIn("rpc.example.invalid", text)
        self.assertNotIn("key-0123456789", text)
        self.assertIn("uncertain BUY claims settle from on-chain balances", text)


# ---------------------------------------------------------------------------
# ONCHAIN_DETECTION: the RPC URL by itself turns on claim evidence only
# ---------------------------------------------------------------------------
class TradeDetectionFlagTests(unittest.TestCase):
    boot = ClaimEvidenceStartupLogTests.boot
    URL = "https://rpc.example.invalid/v2/key-0123456789"

    def test_a_set_rpc_url_leaves_leader_trades_on_the_activity_poll(self):
        built, logs = self.boot(self.URL)
        # detector=None is the engine's default, the activity poll.
        self.assertIsNone(built["detector"])
        self.assertIsNotNone(built["ctf"])
        self.detector_cls.assert_not_called()
        text = "\n".join(logs.output)
        self.assertNotIn("using on-chain OrderFilled detector", text)
        self.assertIn("leader trades are read from the activity poll", text)

    def test_the_flag_with_a_url_reads_leader_trades_on_chain(self):
        built, logs = self.boot(self.URL, onchain_detection=True)
        self.detector_cls.assert_called_once_with(self.URL)
        self.assertIs(self.detector_cls.return_value, built["detector"])
        self.assertIsNotNone(built["ctf"])
        self.assertIn("using on-chain OrderFilled detector", "\n".join(logs.output))

    def test_the_flag_without_a_url_is_warned_about_and_changes_nothing(self):
        built, logs = self.boot("", onchain_detection=True)
        self.assertIsNone(built["detector"])
        self.assertIsNone(built["ctf"])
        self.detector_cls.assert_not_called()
        warnings = [r.getMessage() for r in logs.records
                    if r.name == "main" and r.levelno == logging.WARNING
                    and "ONCHAIN_DETECTION" in r.getMessage()]
        self.assertEqual(1, len(warnings))
        self.assertIn("leader trades stay on the activity poll", warnings[0])

    def test_a_detector_that_fails_to_start_keeps_the_poll_and_claim_evidence(self):
        built, _ = self.boot(self.URL, onchain_detection=True,
                             detector_error=RuntimeError("no chain"))
        self.detector_cls.assert_called_once_with(self.URL)
        self.assertIsNone(built["detector"])
        self.assertIsNotNone(built["ctf"])

    def test_an_engine_built_without_a_detector_polls_the_activity_feed(self):
        from backend.core import detection
        from backend.core.copy_engine import CopyEngine
        engine = CopyEngine(SimpleNamespace(), SimpleNamespace(), detector=None)
        self.assertIsInstance(engine.detector, detection.ActivityPollDetector)

    def test_the_flag_is_on_only_when_the_environment_sets_it_to_one(self):
        for value, expected in ((None, "False"), ("0", "False"), ("1", "True"), ("yes", "False")):
            with self.subTest(value=value):
                env = {k: v for k, v in os.environ.items() if k != "ONCHAIN_DETECTION"}
                env["PYTHONPATH"] = str(ROOT)
                if value is not None:
                    env["ONCHAIN_DETECTION"] = value
                done = subprocess.run(
                    [sys.executable, "-c",
                     "from backend import config; print(config.ONCHAIN_DETECTION)"],
                    capture_output=True, text=True, cwd=ROOT, env=env, timeout=60)
                self.assertEqual(0, done.returncode, done.stderr)
                self.assertEqual(expected, done.stdout.strip())


# ---------------------------------------------------------------------------
# LOG5: a balance read that failed in transit is one WARNING line
# ---------------------------------------------------------------------------
def balance_client(*, balance=None, error=None):
    read = (AsyncMock(side_effect=error) if error is not None
            else AsyncMock(return_value=SimpleNamespace(balance=balance)))
    return SimpleNamespace(get_balance_allowance=read)


def empty_wallet():
    return SimpleNamespace(get_all_positions=AsyncMock(return_value=([], True)))


class SnapshotBalanceLogTests(_DbCase):
    async def stored(self) -> list[tuple[str, float]]:
        rows = await self.db.fetchall(
            "SELECT user_id, equity FROM equity_snapshots ORDER BY id")
        return [(r["user_id"], r["equity"]) for r in rows]

    async def test_a_transport_failure_is_one_warning_line(self):
        request = httpx.Request("GET", "https://clob.example.invalid/balance-allowance")
        failures = (
            # What the SDK raises for any httpx error (polymarket/clients/_transport.py).
            pm_errors.TransportError("Request failed"),
            httpx.ReadTimeout("", request=request),
            httpx.ConnectError("All connection attempts failed", request=request),
            httpx.RemoteProtocolError("<ConnectionTerminated error_code:0>"),
            asyncio.TimeoutError(),
        )
        for error in failures:
            with self.subTest(error=type(error).__name__):
                pm = empty_wallet()
                with self.assertLogs("equity", level="WARNING") as logs:
                    snap = await equity_mod.take_snapshot(
                        self.db, USER, balance_client(error=error), pm)
                self.assertIsNone(snap)
                self.assertEqual(1, len(logs.records))
                record = logs.records[0]
                self.assertEqual(logging.WARNING, record.levelno)
                self.assertIsNone(record.exc_info)
                self.assertEqual(
                    f"snapshot: balance read failed for {USER[:10]} ({type(error).__name__})",
                    record.getMessage())
                pm.get_all_positions.assert_not_awaited()
        self.assertEqual([], await self.stored())

    async def test_any_other_failure_keeps_the_error_and_its_traceback(self):
        clients = (
            balance_client(error=RuntimeError("boom")),
            balance_client(error=pm_errors.RequestRejectedError("server error", status=500)),
            balance_client(balance=None),      # a reply without a balance
        )
        for client in clients:
            pm = empty_wallet()
            with self.assertLogs("equity", level="WARNING") as logs:
                snap = await equity_mod.take_snapshot(self.db, USER, client, pm)
            self.assertIsNone(snap)
            self.assertEqual([logging.ERROR], [r.levelno for r in logs.records])
            self.assertIsNotNone(logs.records[0].exc_info)
            pm.get_all_positions.assert_not_awaited()
        self.assertEqual([], await self.stored())

    async def test_a_pass_still_records_and_counts_the_other_users(self):
        clients = {
            USER: balance_client(error=pm_errors.TransportError("Request failed")),
            OTHER: balance_client(balance=25_000_000),
        }

        async def client_for(user):
            return clients[user["id"]]

        with self.assertLogs("equity", level="WARNING") as logs:
            done = await equity_mod.snapshot_all(self.db, empty_wallet(), client_for)
        self.assertEqual(1, done)
        self.assertEqual([logging.WARNING], [r.levelno for r in logs.records])
        self.assertEqual([(OTHER, 25.0)], await self.stored())


# ---------------------------------------------------------------------------
# D4: the by-wallet breakdown reads only the caller's events
# ---------------------------------------------------------------------------
# get_pnl_by_wallet's query before the review: every user's events were
# grouped, then joined to the caller's positions.
UNSCOPED_BY_WALLET_SQL = (
    "SELECT p.trader_address, c.display_name, "
    "SUM(CASE WHEN COALESCE(e.cnt, 0) > 0 THEN e.total "
    "    ELSE COALESCE(p.realized_pnl, 0) END) AS realized_pnl, "
    "SUM(CASE WHEN p.status IN ('closed','resolved') THEN 1 ELSE 0 END) AS closed_trades, "
    "SUM(CASE WHEN p.status IN ('closed','resolved') AND "
    "    (CASE WHEN COALESCE(e.cnt, 0) > 0 THEN e.total "
    "     ELSE COALESCE(p.realized_pnl, 0) END) > 0 THEN 1 ELSE 0 END) AS wins "
    "FROM copy_positions p "
    "LEFT JOIN (SELECT position_id, COUNT(pnl) AS cnt, COALESCE(SUM(pnl), 0) AS total "
    "           FROM trade_events WHERE pnl IS NOT NULL GROUP BY position_id) e "
    "  ON e.position_id = p.id "
    "LEFT JOIN trader_cache c ON c.address = p.trader_address "
    "WHERE p.user_id = ? AND (p.status IN ('closed', 'resolved') OR COALESCE(e.cnt, 0) > 0) "
    "GROUP BY p.trader_address, c.display_name ORDER BY realized_pnl DESC"
)


class PnlByWalletScopeTests(_DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.db.execute(
            "INSERT INTO trader_cache(address, display_name, last_refreshed) VALUES(?,?,?)",
            (TRADER, "Leader", now_iso()))

    async def add_position(self, user, pid, trader, status, realized, events):
        closed_at = now_iso() if status in ("closed", "resolved") else None
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,"
            "token_id,market_title,outcome,shares,entry_price,notional_usd,status,"
            "realized_pnl,opened_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, user, trader, "condition", f"token-{pid}", "Market", "YES",
             10.0, 0.5, 5.0, status, realized, now_iso(), closed_at))
        for i, (event_type, event_pnl) in enumerate(events):
            await self.db.execute(
                "INSERT INTO trade_events(id,user_id,position_id,event_type,pnl,ts) "
                "VALUES(?,?,?,?,?,?)",
                (f"{pid}-e{i}", user, pid, event_type, event_pnl, now_iso()))

    async def seed_user(self):
        # closed with a partial exit: the events hold 2.0 + 3.0
        await self.add_position(USER, "u1", TRADER, "closed", 3.0,
                                [("partial", 2.0), ("close", 3.0)])
        # open, with banked partial-exit PnL
        await self.add_position(USER, "u2", TRADER, "open", None, [("partial", 1.5)])
        # closed before events carried PnL
        await self.add_position(USER, "u3", SECOND_TRADER, "closed", 4.0, [])
        # resolved at a loss, its only event the open
        await self.add_position(USER, "u4", SECOND_TRADER, "resolved", -1.0, [("open", None)])
        # open and untouched
        await self.add_position(USER, "u5", SECOND_TRADER, "open", None, [("open", None)])

    async def seed_other(self):
        # the second user copies the same trader, with far larger numbers
        await self.add_position(OTHER, "o1", TRADER, "closed", 100.0, [("close", 100.0)])
        await self.add_position(OTHER, "o2", TRADER, "open", None, [("partial", 50.0)])
        await self.add_position(OTHER, "o3", TRADER, "closed", 7.0, [])

    async def unscoped(self, user: str) -> list[dict]:
        out = []
        for r in await self.db.fetchall(UNSCOPED_BY_WALLET_SQL, (user,)):
            trades, wins = int(r["closed_trades"] or 0), int(r["wins"] or 0)
            out.append({
                "trader_address": r["trader_address"],
                "display_name": r["display_name"],
                "realized_pnl": round(float(r["realized_pnl"] or 0.0), 2),
                "closed_trades": trades,
                "win_rate": round(wins / trades, 4) if trades else 0.0,
            })
        return out

    async def test_each_user_gets_their_own_breakdown(self):
        await self.seed_user()
        await self.seed_other()
        self.assertEqual(
            [{"trader_address": TRADER, "display_name": "Leader",
              "realized_pnl": 6.5, "closed_trades": 1, "win_rate": 1.0},
             {"trader_address": SECOND_TRADER, "display_name": None,
              "realized_pnl": 3.0, "closed_trades": 2, "win_rate": 0.5}],
            await pnl.get_pnl_by_wallet(USER, self.db))
        self.assertEqual(
            [{"trader_address": TRADER, "display_name": "Leader",
              "realized_pnl": 157.0, "closed_trades": 2, "win_rate": 1.0}],
            await pnl.get_pnl_by_wallet(OTHER, self.db))

    async def test_another_users_events_leave_a_breakdown_unchanged(self):
        await self.seed_user()
        alone = await pnl.get_pnl_by_wallet(USER, self.db)
        await self.seed_other()
        self.assertEqual(alone, await pnl.get_pnl_by_wallet(USER, self.db))

    async def test_the_scoped_query_returns_what_the_unscoped_one_did(self):
        await self.seed_user()
        await self.seed_other()
        for user in (USER, OTHER):
            with self.subTest(user=user[:6]):
                self.assertEqual(await self.unscoped(user),
                                 await pnl.get_pnl_by_wallet(user, self.db))

    async def test_the_events_are_read_through_the_callers_index(self):
        """Before the review the plan walked every user's events."""
        await self.seed_user()
        await self.seed_other()
        with self.reads() as seen:
            await pnl.get_pnl_by_wallet(USER, self.db)
        (sql, params), = seen
        self.assertEqual((USER, USER), params)
        plan = await self.plan(sql, params)
        # SQLite before 3.36 writes "SEARCH TABLE trade_events".
        searched = re.compile(
            r"SEARCH (TABLE )?trade_events USING INDEX idx_trade_events_user_ts \(user_id=\?\)")
        self.assertEqual(1, len([step for step in plan if searched.fullmatch(step)]), plan)
        self.assertEqual(
            [], [step for step in plan if re.match(r"SCAN (TABLE )?trade_events\b", step)])

    async def test_each_breakdown_sums_to_that_users_headline(self):
        await self.seed_user()
        await self.seed_other()
        for user, expected in ((USER, 9.5), (OTHER, 157.0)):
            with self.subTest(user=user[:6]):
                stats = await pnl.get_pnl_stats(user, self.db)
                by_wallet = await pnl.get_pnl_by_wallet(user, self.db)
                self.assertEqual(expected, stats["realized_pnl"])
                self.assertEqual(expected, round(sum(w["realized_pnl"] for w in by_wallet), 2))


# ---------------------------------------------------------------------------
# D2, D10, D6: the Postgres boot schema
# ---------------------------------------------------------------------------
def index_definition(sql: str, name: str) -> str:
    """`table(column,column)` of CREATE INDEX `name`, without a schema prefix
    or spacing."""
    found = re.search(
        rf"CREATE INDEX IF NOT EXISTS {name}\s+ON\s+(?:public\.)?(\w+)\s*\(([^)]*)\)", sql)
    if found is None:
        raise AssertionError(f"{name} is not created")
    columns = ",".join(part.strip() for part in found.group(2).split(","))
    return f"{found.group(1)}({columns})"


class BootSchemaIndexTests(_DbCase):
    def test_equity_snapshots_index_is_in_the_boot_schema_and_a_migration(self):
        migration = (MIGRATIONS / "0015_equity_snapshots_index.sql").read_text()
        for sql in (PG_SCHEMA_SQL, migration):
            self.assertEqual("equity_snapshots(user_id,ts)",
                             index_definition(sql, "idx_equity_snapshots_user_ts"))
        statements = [s for s in re.sub(r"--[^\n]*", "", migration).split(";") if s.strip()]
        self.assertEqual(
            ["CREATE INDEX IF NOT EXISTS idx_equity_snapshots_user_ts "
             "ON public.equity_snapshots(user_id, ts)"],
            [" ".join(s.split()) for s in statements])

    async def test_sqlite_has_the_same_equity_snapshots_index(self):
        columns = await self.db.fetchall("PRAGMA index_info('idx_equity_snapshots_user_ts')")
        self.assertEqual(["user_id", "ts"],
                         [c["name"] for c in sorted(columns, key=lambda c: c["seqno"])])

    async def test_every_per_user_snapshot_read_can_use_the_index(self):
        """The column order is the one the readers filter and sort by: each
        per-user statement get_series and take_snapshot run is planned as a
        search on (user_id, ts)."""
        with self.reads() as seen:
            await equity_mod.get_series(self.db, USER, "7d")
            # An empty account looks up its latest row before storing one.
            await equity_mod.take_snapshot(
                self.db, USER, balance_client(balance=0), empty_wallet())
        reads = [(sql, params) for sql, params in seen if "FROM equity_snapshots" in sql]
        self.assertEqual(3, len(reads))
        for sql, params in reads:
            detail = " ".join(await self.plan(sql, params))
            self.assertIn("USING INDEX idx_equity_snapshots_user_ts (user_id=?", detail, sql)

    def test_indexes_the_migrations_create_are_in_the_boot_schema(self):
        for name, filename in (
                ("idx_product_events_session_ts", "0009_product_events.sql"),
                ("idx_user_consents_telegram", "0007_onboarding_invariants.sql")):
            with self.subTest(index=name):
                migration = (MIGRATIONS / filename).read_text()
                self.assertEqual(index_definition(migration, name),
                                 index_definition(PG_SCHEMA_SQL, name))

    def test_each_added_index_follows_its_table_in_the_boot_script(self):
        for name, table in (
                ("idx_equity_snapshots_user_ts", "equity_snapshots"),
                ("idx_product_events_session_ts", "product_events"),
                ("idx_user_consents_telegram", "user_consents")):
            with self.subTest(index=name):
                self.assertEqual(1, PG_SCHEMA_SQL.count(f"CREATE INDEX IF NOT EXISTS {name}"))
                self.assertLess(
                    PG_SCHEMA_SQL.index(f"CREATE TABLE IF NOT EXISTS {table} ("),
                    PG_SCHEMA_SQL.index(f"CREATE INDEX IF NOT EXISTS {name}"))


class ClaimBackfillTests(unittest.TestCase):
    """The boot script's claim backfill, run on SQLite with an md5() that
    returns what Postgres' does."""

    @staticmethod
    def backfill() -> str:
        found = re.search(r"UPDATE copy_open_claims SET[^;]*;", PG_SCHEMA_SQL)
        if found is None:
            raise AssertionError("the claim backfill is gone from the boot schema")
        return found.group(0)

    @staticmethod
    def legacy_claims() -> sqlite3.Connection:
        """copy_open_claims as migration 0003 created it: claim_id and
        updated_at still nullable."""
        con = sqlite3.connect(":memory:")
        con.create_function("md5", 1, lambda text: hashlib.md5(text.encode()).hexdigest())
        con.execute(
            "CREATE TABLE copy_open_claims (user_id TEXT NOT NULL, token_id TEXT NOT NULL, "
            "claim_id TEXT, claimed_at TEXT NOT NULL, updated_at TEXT, "
            "PRIMARY KEY(user_id, token_id))")
        con.executemany("INSERT INTO copy_open_claims VALUES(?,?,?,?,?)", [
            ("u1", "t1", "claim-1", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
            ("u1", "t2", None, "2026-01-03T00:00:00+00:00", "2026-01-04T00:00:00+00:00"),
            ("u2", "t1", "claim-3", "2026-01-05T00:00:00+00:00", None),
            ("u2", "t3", None, "2026-01-06T00:00:00+00:00", None),
        ])
        return con

    @staticmethod
    def rows(con: sqlite3.Connection) -> list[tuple]:
        return con.execute(
            "SELECT user_id, token_id, claim_id, claimed_at, updated_at "
            "FROM copy_open_claims ORDER BY user_id, token_id").fetchall()

    def test_the_scoped_backfill_leaves_the_table_as_the_unscoped_one_did(self):
        scoped = self.backfill()
        self.assertIn("WHERE claim_id IS NULL OR updated_at IS NULL", scoped)
        unscoped = scoped.split("WHERE")[0]
        a, b = self.legacy_claims(), self.legacy_claims()
        touched = (a.execute(scoped).rowcount, b.execute(unscoped).rowcount)
        self.assertEqual((3, 4), touched)
        self.assertEqual(self.rows(b), self.rows(a))
        self.assertEqual(
            [("u1", "t1", "claim-1", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
             ("u1", "t2", hashlib.md5(b"u1:t2").hexdigest(),
              "2026-01-03T00:00:00+00:00", "2026-01-04T00:00:00+00:00"),
             ("u2", "t1", "claim-3", "2026-01-05T00:00:00+00:00", "2026-01-05T00:00:00+00:00"),
             ("u2", "t3", hashlib.md5(b"u2:t3").hexdigest(),
              "2026-01-06T00:00:00+00:00", "2026-01-06T00:00:00+00:00")],
            self.rows(a))

    def test_a_backfilled_table_is_left_untouched_on_the_next_start(self):
        scoped = self.backfill()
        con = self.legacy_claims()
        con.execute(scoped)
        before = self.rows(con)
        self.assertEqual(0, con.execute(scoped).rowcount)
        self.assertEqual(4, con.execute(scoped.split("WHERE")[0]).rowcount)
        self.assertEqual(before, self.rows(con))

    def test_the_backfill_still_runs_before_the_columns_become_required(self):
        backfill = PG_SCHEMA_SQL.index(self.backfill())
        for column in ("claim_id", "updated_at"):
            self.assertLess(backfill, PG_SCHEMA_SQL.index(
                f"ALTER TABLE copy_open_claims ALTER COLUMN {column} SET NOT NULL"))


if __name__ == "__main__":
    unittest.main()
