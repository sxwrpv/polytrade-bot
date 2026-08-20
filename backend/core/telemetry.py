"""Privacy-minimized product event retention."""
from __future__ import annotations

import datetime as dt

from fastapi import HTTPException


_POSTGRES_ADMISSION_LOCK = 824_705_311


async def enforce_product_event_limits(
    db,
    session_id: str,
    *,
    now: dt.datetime | None = None,
    session_limit: int = 60,
    global_limit: int = 2_000,
    max_rows: int = 100_000,
) -> None:
    """Bound abuse rate and total telemetry storage without retaining identity."""
    # PostgreSQL's default READ COMMITTED isolation does not serialize the
    # aggregate COUNT -> evict -> INSERT sequence. The endpoint calls this on a
    # connection-bound transaction, so one transaction-scoped advisory lock
    # makes admission exact across every API worker/node. SQLite write
    # transactions are already serialized with BEGIN IMMEDIATE.
    if getattr(db, "is_pg", False):
        await db.fetchval(
            "SELECT pg_advisory_xact_lock(?)", (_POSTGRES_ADMISSION_LOCK,),
        )
    current = now or dt.datetime.now(dt.timezone.utc)
    minute_cutoff = (current - dt.timedelta(minutes=1)).isoformat()
    per_session = await db.fetchval(
        "SELECT COUNT(*) FROM product_events WHERE session_id=? AND ts>=?",
        (session_id, minute_cutoff),
    )
    if int(per_session or 0) >= max(1, int(session_limit)):
        raise HTTPException(status_code=429, detail="telemetry rate limit exceeded")
    global_recent = await db.fetchval(
        "SELECT COUNT(*) FROM product_events WHERE ts>=?", (minute_cutoff,),
    )
    if int(global_recent or 0) >= max(1, int(global_limit)):
        raise HTTPException(status_code=429, detail="telemetry temporarily unavailable")

    capacity = max(1, int(max_rows))
    total = int(await db.fetchval("SELECT COUNT(*) FROM product_events") or 0)
    if total >= capacity:
        remove_count = total - capacity + 1
        await db.execute(
            "DELETE FROM product_events WHERE id IN ("
            "SELECT id FROM product_events ORDER BY ts ASC,id ASC LIMIT ?)",
            (remove_count,),
        )


async def prune_product_events(db, *, retention_days: int = 90) -> int:
    """Delete product events outside the bounded retention window.

    Product events intentionally have no account or wallet foreign key. Their
    opaque browser-tab session id is useful only for short funnel analysis.
    """
    days = max(1, min(int(retention_days), 365))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    return await db.execute("DELETE FROM product_events WHERE ts < ?", (cutoff,))
