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
import logging
from contextlib import asynccontextmanager
from typing import Any, Iterable, Sequence

import aiosqlite

from backend.config import (
    DATABASE_URL, DB_PATH, DB_POOL_MAX_INACTIVE_SECONDS, DB_POOL_MAX_SIZE,
    DB_POOL_MIN_SIZE,
)
from backend.db.models import MIGRATIONS, PG_SCHEMA_SQL, SCHEMA_SQL

log = logging.getLogger("database")

# A pooled Postgres connection can be closed underneath us — by Supabase's
# pooler recycling it, by an idle timeout, or by a network blip. asyncpg
# surfaces that as ConnectionDoesNotExistError on the NEXT use, and the pool
# hands the dead connection out again.
#
# Seen once in production (2026-09-02 06:35:36, "loop _detect_tick failed"),
# recovered on the next cycle. One retry turns that lost cycle into a
# non-event.
#
# Reads only. A write or a transaction may have COMMITTED before the
# connection died, and re-running it could double an order claim or a trade
# event, so those still propagate and let the caller's own reconciliation
# decide.
def _is_stale_connection(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in ("ConnectionDoesNotExistError", "InterfaceError",
                "ConnectionResetError", "ConnectionFailureError"):
        return True
    return isinstance(exc, (ConnectionResetError, BrokenPipeError))


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
        # Availability, for /api/ready. Counters only — no query text, no
        # parameters, nothing that could carry an address or a secret.
        self.last_success_at: str | None = None
        self.last_failure_at: str | None = None
        self.last_failure_kind: str | None = None
        self.stale_connection_retries = 0
        self.consecutive_failures = 0

    def _note_ok(self) -> None:
        self.last_success_at = now_iso()
        self.consecutive_failures = 0

    def _note_failure(self, exc: BaseException) -> None:
        self.last_failure_at = now_iso()
        self.last_failure_kind = type(exc).__name__
        self.consecutive_failures += 1

    async def _read(self, run):
        """Run an idempotent read, retrying once past a dead pooled connection.

        `run` is re-invoked from scratch, so it must not have side effects.
        """
        try:
            out = await run()
        except Exception as exc:
            if not (self.is_pg and _is_stale_connection(exc)):
                self._note_failure(exc)
                raise
            self.stale_connection_retries += 1
            log.warning("stale pooled connection (%s) — retrying the read once",
                        type(exc).__name__)
            try:
                out = await run()
            except Exception as retry_exc:
                self._note_failure(retry_exc)
                raise
        self._note_ok()
        return out

    def availability(self) -> dict:
        """Non-identifying snapshot for the readiness endpoint."""
        return {
            "backend": "postgres" if self.is_pg else "sqlite",
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_failure_kind": self.last_failure_kind,
            "consecutive_failures": self.consecutive_failures,
            "stale_connection_retries": self.stale_connection_retries,
        }

    async def connect(self) -> None:
        if self.is_pg:
            import asyncpg
            # statement_cache_size=0 keeps us compatible with Supabase's
            # transaction pooler (pgbouncer) if the DSN points at :6543.
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=DB_POOL_MIN_SIZE,
                max_size=DB_POOL_MAX_SIZE,
                statement_cache_size=0,
                # Retire idle connections before the pooler or the network
                # does it for us, which is what produced the one observed
                # ConnectionDoesNotExistError.
                max_inactive_connection_lifetime=DB_POOL_MAX_INACTIVE_SECONDS,
            )
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

    async def claim_wallet_creation(self, telegram_user_id: int, claim_token: str,
                                    *, stale_before: str) -> bool:
        """Acquire the durable pre-side-effect fence for one Telegram identity.

        A fresh ``claimed`` row belongs to a live request. Only an aged claim
        that never advanced to ``side_effect_started`` may be taken over. Every
        later state is fail-closed and requires operator reconciliation.
        """
        now = now_iso()
        async with self.transaction(write=True) as tx:
            inserted = await tx.execute(
                "INSERT INTO wallet_creation_claims(telegram_user_id,claim_token,state,"
                "claimed_at,updated_at) VALUES(?,?,'claimed',?,?) "
                "ON CONFLICT(telegram_user_id) DO NOTHING",
                (telegram_user_id, claim_token, now, now),
            )
            if inserted == 1:
                return True
            recovered = await tx.execute(
                "UPDATE wallet_creation_claims SET claim_token=?,claimed_at=?,updated_at=?,"
                "last_error=NULL WHERE telegram_user_id=? AND state='claimed' "
                "AND updated_at < ?",
                (claim_token, now, now, telegram_user_id, stale_before),
            )
            return recovered == 1

    async def acquire_wallet_creation_lease(
            self, telegram_user_id: int, owner: str, *, stale_before: str,
            lease_expires_at: str) -> dict | None:
        """Acquire exclusive work ownership, including safe signer resumes.

        ``claimed`` has no durable signer and is recoverable only when stale.
        ``side_effect_started`` is recoverable only when its prior owner
        explicitly released it after an SDK call returned a caught failure.
        Expiry is informational: an owner may still have opaque, unfenceable
        external work running. The durable signer is never replaced.
        """
        now = now_iso()
        async with self.transaction(write=True) as tx:
            inserted = await tx.execute(
                "INSERT INTO wallet_creation_claims(telegram_user_id,claim_token,state,"
                "claimed_at,updated_at,lease_owner,lease_expires_at) "
                "VALUES(?,?,'claimed',?,?,?,?) ON CONFLICT(telegram_user_id) DO NOTHING",
                (telegram_user_id, owner, now, now, owner, lease_expires_at),
            )
            if inserted != 1:
                select_sql = "SELECT * FROM wallet_creation_claims WHERE telegram_user_id=?" + (
                    " FOR UPDATE" if self.is_pg else "")
                claim = await tx.fetchone(select_sql, (telegram_user_id,))
                if not claim:
                    return None
                changed = 0
                if claim["state"] == "claimed" and claim["updated_at"] < stale_before:
                    changed = await tx.execute(
                        "UPDATE wallet_creation_claims SET claim_token=?,claimed_at=?,updated_at=?,"
                        "last_error=NULL,lease_owner=?,lease_expires_at=? "
                        "WHERE telegram_user_id=? AND state='claimed' AND updated_at < ?",
                        (owner, now, now, owner, lease_expires_at,
                         telegram_user_id, stale_before),
                    )
                elif (claim["state"] == "side_effect_started" and
                      claim.get("signer_address") and claim.get("private_key_enc") and
                      not claim.get("lease_owner")):
                    changed = await tx.execute(
                        "UPDATE wallet_creation_claims SET lease_owner=?,lease_expires_at=?,"
                        "updated_at=? WHERE telegram_user_id=? AND state='side_effect_started' "
                        "AND lease_owner IS NULL",
                        (owner, lease_expires_at, now, telegram_user_id),
                    )
                if changed != 1:
                    return None
            return await tx.fetchone(
                "SELECT * FROM wallet_creation_claims WHERE telegram_user_id=? AND lease_owner=?",
                (telegram_user_id, owner),
            )

    async def prepare_wallet_creation_signer(
            self, telegram_user_id: int, owner: str, signer_address: str,
            private_key_enc: str) -> dict | None:
        """Persist the sole signer before any opaque SDK call is permitted."""
        async with self.transaction(write=True) as tx:
            changed = await tx.execute(
                "UPDATE wallet_creation_claims SET state='side_effect_started',"
                "signer_address=?,private_key_enc=?,updated_at=? "
                "WHERE telegram_user_id=? AND state='claimed' AND lease_owner=? "
                "AND signer_address IS NULL AND private_key_enc IS NULL",
                (signer_address, private_key_enc, now_iso(), telegram_user_id, owner),
            )
            if changed != 1:
                return None
            return await tx.fetchone(
                "SELECT * FROM wallet_creation_claims WHERE telegram_user_id=? AND lease_owner=?",
                (telegram_user_id, owner),
            )

    async def release_wallet_creation_after_sdk_failure(
            self, telegram_user_id: int, owner: str) -> bool:
        """Explicitly permit exact-signer resume after a caught SDK failure."""
        changed = await self.execute(
            "UPDATE wallet_creation_claims SET lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
            "WHERE telegram_user_id=? AND lease_owner=? AND state='side_effect_started'",
            (now_iso(), telegram_user_id, owner),
        )
        return changed == 1

    async def abandon_wallet_preparation(self, telegram_user_id: int, owner: str) -> bool:
        """Abandon pre-SDK preparation without making a durable signer resumable.

        The signer-persist UPDATE may have committed even if its acknowledgement
        was lost. Inspect and mutate in one transaction: delete only a still-
        ``claimed`` row. A prepared row retains its owner because unknown state
        requires operator reconciliation.
        """
        async with self.transaction(write=True) as tx:
            select_sql = "SELECT state FROM wallet_creation_claims WHERE telegram_user_id=?" + (
                " FOR UPDATE" if self.is_pg else "")
            claim = await tx.fetchone(select_sql, (telegram_user_id,))
            if not claim:
                return False
            if claim["state"] != "claimed":
                return False
            changed = await tx.execute(
                "DELETE FROM wallet_creation_claims WHERE telegram_user_id=? "
                "AND lease_owner=? AND state='claimed'",
                (telegram_user_id, owner),
            )
            return changed == 1

    async def record_wallet_creation_error(self, telegram_user_id: int,
                                           claim_token: str, error: str) -> bool:
        """Annotate a retained post-boundary fence with non-secret diagnostics."""
        changed = await self.execute(
            "UPDATE wallet_creation_claims SET last_error=?,updated_at=? "
            "WHERE telegram_user_id=? AND lease_owner=? AND state='side_effect_started'",
            (error[:500], now_iso(), telegram_user_id, claim_token),
        )
        return changed == 1

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
        async def run():
            if self.is_pg:
                row = await self._pool.fetchrow(_to_pg(sql), *params)
                return dict(row) if row is not None else None
            async with self._sqlite_lock:
                async with self._conn.execute(sql, params) as cur:
                    row = await cur.fetchone()
                    return dict(row) if row is not None else None
        return await self._read(run)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        async def run():
            if self.is_pg:
                rows = await self._pool.fetch(_to_pg(sql), *params)
                return [dict(r) for r in rows]
            async with self._sqlite_lock:
                async with self._conn.execute(sql, params) as cur:
                    rows = await cur.fetchall()
                    return [dict(r) for r in rows]
        return await self._read(run)

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        async def run():
            if self.is_pg:
                return await self._pool.fetchval(_to_pg(sql), *params)
            async with self._sqlite_lock:
                async with self._conn.execute(sql, params) as cur:
                    row = await cur.fetchone()
                    return row[0] if row is not None else None
        return await self._read(run)
