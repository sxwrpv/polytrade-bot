"""Async database access — Postgres (asyncpg) or SQLite (aiosqlite).

Backend selected at connect time: Postgres when a DSN is configured
(config.DATABASE_URL / passed `dsn=`), SQLite otherwise. Every call site is
backend-agnostic — SQL is written with `?` placeholders and rows come back as
plain dicts; the Postgres path translates `?` -> `$1,$2,...` and normalizes
Records to dicts, so nothing above this module changes when you flip backends.

SQLite: one shared aiosqlite connection (serialized worker thread, WAL) — fine
for a single node. Postgres: an asyncpg pool (safe for the concurrent API +
CopyEngine access that a single asyncpg connection is not).

Usage:
    db = Database()          # Postgres if DATABASE_URL set, else SQLite
    await db.connect()
    await db.init()          # create tables (idempotent)
    ...
    await db.close()
"""
from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from typing import Any, Iterable, Sequence

import aiosqlite

from backend.config import DATABASE_URL, DB_PATH
from backend.db.models import MIGRATIONS, PG_SCHEMA_SQL, SCHEMA_SQL


def now_iso() -> str:
    """UTC timestamp string for *_at / ts columns."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _to_pg(sql: str) -> str:
    """Translate SQLite `?` positional placeholders to Postgres `$1,$2,...`.

    Safe here because the codebase never puts a literal `?` inside SQL string
    content — every `?` is a bind placeholder (verified). Kept intentionally
    dumb: a left-to-right scan, no parsing.
    """
    out: list[str] = []
    n = 0
    for ch in sql:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


class _Transaction:
    """Connection-bound query helpers; writes commit only with the context."""

    def __init__(self, con, is_pg: bool) -> None:
        self.con = con
        self.is_pg = is_pg

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        if self.is_pg:
            import asyncpg
            try:
                status = await self.con.execute(_to_pg(sql), *params)
            except asyncpg.UniqueViolationError as e:
                raise aiosqlite.IntegrityError(str(e)) from e
            try:
                return int(status.split()[-1])
            except (ValueError, IndexError, AttributeError):
                return 0
        cur = await self.con.execute(sql, params)
        return cur.rowcount

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        if self.is_pg:
            row = await self.con.fetchrow(_to_pg(sql), *params)
        else:
            async with self.con.execute(sql, params) as cur:
                row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        if self.is_pg:
            rows = await self.con.fetch(_to_pg(sql), *params)
        else:
            async with self.con.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.is_pg:
            return await self.con.fetchval(_to_pg(sql), *params)
        async with self.con.execute(sql, params) as cur:
            row = await cur.fetchone()
        return row[0] if row is not None else None


class Database:
    def __init__(self, path: str | None = None, dsn: str | None = None) -> None:
        # explicit dsn wins; else DATABASE_URL; SQLite when neither is set
        self.dsn = dsn if dsn is not None else (DATABASE_URL or None)
        self.is_pg = bool(self.dsn)
        self.path = path or DB_PATH
        self._conn: aiosqlite.Connection | None = None   # sqlite
        self._pool = None                                # asyncpg.Pool
        self._sqlite_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.is_pg:
            import asyncpg
            # statement_cache_size=0 keeps us compatible with Supabase's
            # transaction pooler (pgbouncer) if the DSN points at :6543.
            self._pool = await asyncpg.create_pool(
                self.dsn, min_size=1, max_size=10, statement_cache_size=0)
            return
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._conn.commit()

    async def init(self) -> None:
        """Create all tables and indexes (idempotent)."""
        if self.is_pg:
            # asyncpg runs multi-statement scripts via the simple protocol when
            # there are no bind args — the whole schema goes in one call.
            async with self._pool.acquire() as con:
                await con.execute(PG_SCHEMA_SQL)
            return
        await self._conn.executescript(SCHEMA_SQL)
        for stmt in MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except aiosqlite.OperationalError:
                pass  # column already exists
        await self._conn.commit()

    async def close(self) -> None:
        if self.is_pg:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None
            return
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self, *, write: bool = False):
        """Yield connection-bound helpers inside one atomic transaction.

        Postgres callers may use ``FOR UPDATE``. SQLite write transactions use
        ``BEGIN IMMEDIATE`` so separate processes/connections serialize before
        reading aggregate risk.
        """
        if self.is_pg:
            async with self._pool.acquire() as con:
                async with con.transaction():
                    yield _Transaction(con, True)
            return
        async with self._sqlite_lock:
            await self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield _Transaction(self._conn, False)
            except BaseException:
                await self._conn.rollback()
                raise
            else:
                await self._conn.commit()

    # --- query helpers (rows returned as plain dicts) ----------------------
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write; return affected row count."""
        if self.is_pg:
            import asyncpg
            try:
                status = await self._pool.execute(_to_pg(sql), *params)
            except asyncpg.UniqueViolationError as e:
                # keep the existing `except aiosqlite.IntegrityError` sites working
                raise aiosqlite.IntegrityError(str(e)) from e
            # status is a command tag like "UPDATE 3" / "INSERT 0 1" / "DELETE 2"
            try:
                return int(status.split()[-1])
            except (ValueError, IndexError, AttributeError):
                return 0
        async with self._sqlite_lock:
            cur = await self._conn.execute(sql, params)
            await self._conn.commit()
            return cur.rowcount

    async def claim_managed_sell(self, user_id: str, token_id: str,
                                 position_id: str) -> bool:
        """Serialize a SELL claim with BUY reservations on the per-user DB lock.

        The user row is the Postgres worker lock; SQLite's BEGIN IMMEDIATE is the
        cross-connection equivalent.  The active BUY-claim check and
        open->closing transition therefore cannot pass a resize reservation.
        """
        async with self.transaction(write=True) as tx:
            user_sql = "SELECT id FROM users WHERE id=?" + (
                " FOR UPDATE" if self.is_pg else "")
            if not await tx.fetchone(user_sql, (user_id,)):
                return False
            active_buy = await tx.fetchone(
                "SELECT token_id FROM copy_open_claims WHERE user_id=? AND token_id=? "
                "AND state IN ('reserved','submitting','uncertain')",
                (user_id, token_id))
            if active_buy:
                return False
            changed = await tx.execute(
                "UPDATE copy_positions SET status='closing', closing_at=? "
                "WHERE id=? AND user_id=? AND token_id=? AND status='open'",
                (now_iso(), position_id, user_id, token_id))
            return changed == 1

    async def try_transition(self, position_id: str, from_status: str, to_status: str) -> bool:
        """Atomically flip copy_positions.status if it still matches from_status.

        Used to "claim" a position before placing an exit order, so two
        concurrent close attempts (e.g. a manual close racing the engine's own
        close/resolve) can't both submit a SELL for the same shares — only the
        caller that wins the UPDATE proceeds. Returns True iff this call
        performed the transition.
        """
        if to_status == "closing":
            # stamp the fence so stuck-closing recovery can age-gate safely
            rowcount = await self.execute(
                "UPDATE copy_positions SET status = ?, closing_at = ? "
                "WHERE id = ? AND status = ?",
                (to_status, now_iso(), position_id, from_status))
        else:
            rowcount = await self.execute(
                "UPDATE copy_positions SET status = ? WHERE id = ? AND status = ?",
                (to_status, position_id, from_status))
        return rowcount > 0

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = list(rows)
        if self.is_pg:
            import asyncpg
            try:
                await self._pool.executemany(_to_pg(sql), rows)
            except asyncpg.UniqueViolationError as e:
                raise aiosqlite.IntegrityError(str(e)) from e
            return
        async with self._sqlite_lock:
            await self._conn.executemany(sql, rows)
            await self._conn.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        if self.is_pg:
            row = await self._pool.fetchrow(_to_pg(sql), *params)
            return dict(row) if row is not None else None
        async with self._sqlite_lock:
            async with self._conn.execute(sql, params) as cur:
                row = await cur.fetchone()
                return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        if self.is_pg:
            rows = await self._pool.fetch(_to_pg(sql), *params)
            return [dict(r) for r in rows]
        async with self._sqlite_lock:
            async with self._conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.is_pg:
            return await self._pool.fetchval(_to_pg(sql), *params)
        async with self._sqlite_lock:
            async with self._conn.execute(sql, params) as cur:
                row = await cur.fetchone()
                return row[0] if row is not None else None
