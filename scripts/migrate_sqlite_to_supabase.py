"""Copy all rows from the local SQLite DB into a Postgres/Supabase database.

One-shot cutover helper. Reads every table from copybot.db and bulk-inserts
into the Postgres DB at DATABASE_URL (or --dsn), in FK-dependency order, with
ON CONFLICT DO NOTHING so it's safe to re-run. It does NOT delete anything on
either side; the SQLite file stays untouched as your rollback.

Usage:
    DATABASE_URL='postgresql://postgres:<PW>@db.<ref>.supabase.co:5432/postgres' \
        .venv/bin/python -m scripts.migrate_sqlite_to_supabase
    # or: .venv/bin/python -m scripts.migrate_sqlite_to_supabase --dsn '<url>' --sqlite copybot.db

Run the schema first (supabase/migrations/0001_init.sql, or just boot the app
once against the DSN so Database.init() creates it).
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3

# Parent → child order so foreign keys resolve. equity_snapshots.id is an
# IDENTITY column in Postgres, so it's excluded and re-generated on insert.
_TABLES = [
    ("users", None),
    ("trader_cache", None),
    ("followed_traders", None),
    ("copy_positions", None),
    ("trade_events", None),
    ("equity_snapshots", {"id"}),   # skip the auto-identity PK
]


async def migrate(sqlite_path: str, dsn: str) -> None:
    import asyncpg

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, statement_cache_size=0)
    try:
        for table, skip in _TABLES:
            skip = skip or set()
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"{table:18} 0 rows")
                continue
            cols = [c for c in rows[0].keys() if c not in skip]
            ph = ",".join(f"${i + 1}" for i in range(len(cols)))
            sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) "
                   f"ON CONFLICT DO NOTHING")
            data = [tuple(r[c] for c in cols) for r in rows]
            async with pool.acquire() as con:
                await con.executemany(sql, data)
            print(f"{table:18} {len(rows)} rows -> inserted")
    finally:
        await pool.close()
        src.close()


def main() -> None:
    import os

    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default="copybot.db")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""))
    args = ap.parse_args()
    if not args.dsn:
        raise SystemExit("set DATABASE_URL or pass --dsn")
    asyncio.run(migrate(args.sqlite, args.dsn))
    print("done — SQLite left untouched as rollback.")


if __name__ == "__main__":
    main()
