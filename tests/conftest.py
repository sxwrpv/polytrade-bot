"""Hermetic test environment.

backend.config calls load_dotenv() at import, so without this the suite read
the developer's own .env: tests that need ENCRYPTION_SECRET or
TELEGRAM_BOT_TOKEN passed only on a machine holding real secrets, and a
DATABASE_URL there would have pointed Database() at a live Postgres.

DB_PATH is pinned for the same reason. backend.config reads it once, at first
import, so a test that sets it later (test_readiness does) is ignored in a
full run, and the app it boots opened ./copybot.db -- on a developer machine,
their real local database.

This runs before any test module imports backend.*. PYTHON_DOTENV_DISABLED
switches .env loading off (python-dotenv >= 1.2); the explicit values below
win on any version, because load_dotenv never overrides a variable that is
already set.
"""
import atexit
import os
import shutil
import tempfile

_scratch = tempfile.mkdtemp(prefix="polytrade-tests-")
atexit.register(shutil.rmtree, _scratch, ignore_errors=True)

os.environ["PYTHON_DOTENV_DISABLED"] = "1"

os.environ.update({
    "ENCRYPTION_SECRET": "test-encryption-secret",
    "TELEGRAM_BOT_TOKEN": "test-bot-token",
    # Empty means SQLite at DB_PATH; never a real Postgres from a local .env.
    "DATABASE_URL": "",
    "DB_PATH": os.path.join(_scratch, "copybot.db"),
    "POLYGON_RPC_URL": "",
    "COPY_ENGINE_AUTOSTART": "0",
    "STATS_REFRESH_AUTOSTART": "0",
    "EQUITY_SNAPSHOT_AUTOSTART": "0",
    "SEED_ON_START": "0",
})
