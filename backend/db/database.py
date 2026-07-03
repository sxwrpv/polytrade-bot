"""Async SQLite access (aiosqlite).

A single shared connection serves both the FastAPI handlers and the CopyEngine
background task; aiosqlite serializes calls through its own worker thread, and
WAL mode lets reads proceed during writes. For this scale that is sufficient —
no pool needed.

Usage:
    db = Database()
    await db.connect()
    await db.init()          # create tables (idempotent)
    ...
    await db.close()
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Sequence

import aiosqlite

from backend.config import DB_PATH
from backend.db.models import MIGRATIONS, SCHEMA_SQL


def now_iso() -> str:
    """UTC timestamp string for *_at / ts columns."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or DB_PATH
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected — call await connect() first")
        return self._conn

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.commit()

    async def init(self) -> None:
        """Create all tables and indexes, then apply column migrations (idempotent)."""
        await self.conn.executescript(SCHEMA_SQL)
        for stmt in MIGRATIONS:
            try:
                await self.conn.execute(stmt)
            except aiosqlite.OperationalError:
                pass  # column already exists
        await self.conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- query helpers (rows returned as plain dicts) ----------------------
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.rowcount

    async def try_transition(self, position_id: str, from_status: str, to_status: str) -> bool:
        """Atomically flip copy_positions.status if it still matches from_status.

        Used to "claim" a position before placing an exit order, so two
        concurrent close attempts (e.g. a manual close racing the engine's own
        close/resolve) can't both submit a SELL for the same shares — only the
        caller that wins the UPDATE proceeds. Returns True iff this call
        performed the transition.
        """
        rowcount = await self.execute(
            "UPDATE copy_positions SET status = ? WHERE id = ? AND status = ?",
            (to_status, position_id, from_status))
        return rowcount > 0

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        await self.conn.executemany(sql, rows)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return row[0] if row is not None else None
