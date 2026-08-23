"""Account equity snapshots — the data behind the Performance line chart.

The old equity curve was built from realized `trade_events`, so it only moved
when a position closed — one point per closed trade, flat and sparse. This
records the WHOLE account (cash + live position value) at a fixed cadence, so
the chart moves with the market like a real equity curve.

A single snapshot stream is taken at the finest cadence (default every 5 min);
wider windows downsample it at query time:
  7d  -> ~5-minute points   (native)
  30d -> 30-minute buckets
  all -> 4-hour buckets

Storage is compacted on the same boundaries. Once a row is old enough that no
window still renders it at 5-minute resolution, keeping it at 5-minute
resolution only costs disk. See COMPACTION_TIERS.
"""
from __future__ import annotations

import datetime as dt
import logging

from backend.db.database import now_iso

log = logging.getLogger("equity")

# period -> (window_days, bucket_seconds) for query-time downsampling
_BUCKETS = {
    "7d": (7, 300),        # 5 min
    "30d": (30, 1800),     # 30 min
    "all": (3650, 14400),  # 4 h
}

# (older_than_days, bucket_seconds) — storage compaction, coarsest tier last.
#
# Each tier is set to the finest resolution any window still SHOWS at that age,
# so compaction is invisible in the UI:
#
#   0-7 days    every window that reaches here uses 5-min points  -> keep raw
#   7-30 days   only 30d (30-min) and all (4-h) reach here        -> 30-min
#   30+ days    only all (4-h) reaches here                       -> 4-h
#
# A row older than 30 days is never rendered at finer than 4-hour resolution by
# any caller, so collapsing it to one row per 4 hours loses nothing a reader
# could have seen. Change a bucket in _BUCKETS and the matching tier here has to
# move with it, or the chart starts asking for detail that has been discarded —
# test_equity_compaction pins that relationship.
COMPACTION_TIERS = (
    (7, 1800),
    (30, 14400),
)

# Bound on one pass, so a long-neglected database is compacted over several
# runs instead of one statement that holds a write lock for minutes.
COMPACTION_SCAN_LIMIT = 50_000
_DELETE_CHUNK = 400


async def _cumulative_realized(db, user_id: str) -> float:
    val = await db.fetchval(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM copy_positions "
        "WHERE user_id = ? AND status IN ('closed', 'resolved')", (user_id,))
    return float(val or 0.0)


async def take_snapshot(db, user_id: str, client, pm) -> dict | None:
    """Read the account's current worth and persist one snapshot row.

    equity = free cash + market value of everything held (open positions plus
    resolved-but-unredeemed winners). Returns the snapshot, or None if the
    balance read failed (we don't store a half-known equity)."""
    try:
        bal = await client.get_balance_allowance(asset_type="COLLATERAL")
        balance = bal.balance / 1e6
    except Exception:
        log.exception("snapshot: balance read failed for %s", user_id[:10])
        return None
    try:
        positions = await pm.get_positions(user_id, size_threshold=0)
    except Exception:
        positions = []
    held = [p for p in positions if p.size > 0.01]
    positions_value = round(sum(p.current_value for p in held), 2)
    unrealized = round(sum(p.cash_pnl for p in held if not p.redeemable), 2)
    realized = round(await _cumulative_realized(db, user_id), 2)
    equity = round(balance + positions_value, 2)
    ts = now_iso()
    await db.execute(
        "INSERT INTO equity_snapshots(user_id, ts, equity, balance, positions_value, "
        "realized_pnl, unrealized_pnl) VALUES(?,?,?,?,?,?,?)",
        (user_id, ts, equity, round(balance, 2), positions_value, realized, unrealized))
    return {"ts": ts, "equity": equity, "balance": round(balance, 2),
            "positions_value": positions_value, "realized_pnl": realized,
            "unrealized_pnl": unrealized}


async def snapshot_all(db, pm, client_for) -> int:
    """Snapshot every user that has a wallet. `client_for(user_row)` returns an
    authenticated CLOB client (cached upstream). Best-effort per user — one
    failure never blocks the rest."""
    users = await db.fetchall("SELECT * FROM users")
    done = 0
    for user in users:
        try:
            client = await client_for(user)
            if await take_snapshot(db, user["id"], client, pm) is not None:
                done += 1
        except Exception:
            log.exception("snapshot failed for %s", user["id"][:10])
    return done


def _epoch(ts: str) -> float:
    try:
        return dt.datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


# Retention tiers: (min_age_days, max_age_days|None, bucket_seconds). Snapshots
# in each age band are thinned to one row per bucket — exactly the resolution
# the matching chart renders, so pruning never changes a chart. Snapshots
# younger than the first tier's min_age are kept untouched (7d chart = full
# 5-min resolution).
_RETENTION = (
    (7, 30, 1800),      # 7–30d old  -> 30-min buckets (30d chart)
    (30, None, 14400),  # >30d old   -> 4-hour buckets (all chart)
)


async def prune_snapshots(db) -> int:
    """Thin equity_snapshots to the resolution the charts actually draw and
    delete the rest — bounds storage without altering any chart. Keeps the
    latest snapshot in each (user, time-bucket); drops the redundant middle."""
    now = dt.datetime.now(dt.timezone.utc)
    to_delete: list[int] = []
    for min_age, max_age, bucket in _RETENTION:
        hi = (now - dt.timedelta(days=min_age)).isoformat()          # older than this
        lo = ((now - dt.timedelta(days=max_age)).isoformat()
              if max_age is not None else None)
        if lo is not None:
            rows = await db.fetchall(
                "SELECT id, user_id, ts FROM equity_snapshots WHERE ts < ? AND ts >= ? "
                "ORDER BY ts", (hi, lo))
        else:
            rows = await db.fetchall(
                "SELECT id, user_id, ts FROM equity_snapshots WHERE ts < ? ORDER BY ts",
                (hi,))
        keep: dict[tuple, int] = {}   # (user, bucket_key) -> id to keep (latest wins)
        for r in rows:
            key = (r["user_id"], int(_epoch(r["ts"]) // bucket))
            keep[key] = r["id"]        # rows are ts-ordered, so last seen = latest
        keep_ids = set(keep.values())
        to_delete += [r["id"] for r in rows if r["id"] not in keep_ids]
    for i in range(0, len(to_delete), 500):   # chunk to keep the SQL param list sane
        chunk = to_delete[i:i + 500]
        await db.execute(
            f"DELETE FROM equity_snapshots WHERE id IN ({','.join('?' * len(chunk))})",
            chunk)
    return len(to_delete)


async def get_series(db, user_id: str, period: str = "7d") -> list[dict]:
    """Downsampled equity/PnL series for the chart. One point per time bucket
    (last snapshot in the bucket wins), so 30d/all stay light and readable
    while 7d keeps full 5-min resolution."""
    days, bucket = _BUCKETS.get(period, _BUCKETS["7d"])
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    rows = await db.fetchall(
        "SELECT ts, equity, balance, realized_pnl, unrealized_pnl FROM equity_snapshots "
        "WHERE user_id = ? AND ts >= ? ORDER BY ts", (user_id, cutoff))
    by_bucket: dict[int, dict] = {}
    for r in rows:
        key = int(_epoch(r["ts"]) // bucket)
        by_bucket[key] = {
            "ts": r["ts"],
            "equity": round(float(r["equity"] or 0.0), 2),
            "balance": round(float(r["balance"] or 0.0), 2),
            # total PnL at that instant = realized to date + open-position mark
            "pnl": round(float(r["realized_pnl"] or 0.0) + float(r["unrealized_pnl"] or 0.0), 2),
        }
    return [by_bucket[k] for k in sorted(by_bucket)]


async def compact_snapshots(
    db,
    *,
    tiers: tuple[tuple[int, int], ...] = COMPACTION_TIERS,
    scan_limit: int = COMPACTION_SCAN_LIMIT,
    now: dt.datetime | None = None,
) -> int:
    """Collapse aged snapshots to one row per bucket. Returns rows deleted.

    The survivor is the LAST row in each bucket, which is exactly what
    `get_series` already renders for that bucket — so a compacted series draws
    the identical line it drew before, just without the rows nobody could see.

    Bucketing happens in Python rather than SQL because `ts` is stored as ISO
    text and the epoch conversion differs between SQLite and Postgres; doing it
    here keeps one implementation and makes it directly testable.

    Safe to run repeatedly: a compacted range has one row per bucket and so
    yields nothing to delete on the next pass.
    """
    reference = now or dt.datetime.now(dt.timezone.utc)
    total = 0

    # Coarsest tier first. A row older than the 30-day boundary is handled by
    # the 4-hour tier and must not then be re-examined by the 30-minute one,
    # which would keep a survivor per 30-minute bucket and undo the coarser pass.
    ordered = sorted(tiers, key=lambda t: t[0], reverse=True)
    previous_days: int | None = None

    for days, bucket in ordered:
        cutoff = (reference - dt.timedelta(days=days)).isoformat()
        params: list = [cutoff]
        sql = ("SELECT id, user_id, ts FROM equity_snapshots WHERE ts < ?")
        if previous_days is not None:
            # Lower bound: this tier owns only the band above the coarser one.
            floor = (reference - dt.timedelta(days=previous_days)).isoformat()
            sql += " AND ts >= ?"
            params.append(floor)
        sql += " ORDER BY user_id, ts LIMIT ?"
        params.append(scan_limit)

        rows = await db.fetchall(sql, tuple(params))
        previous_days = days
        if not rows:
            continue

        # Last row in each (user, bucket) survives; everything before it goes.
        survivors: dict[tuple[str, int], int] = {}
        seen: list[tuple[tuple[str, int], int]] = []
        for r in rows:
            key = (r["user_id"], int(_epoch(r["ts"]) // bucket))
            survivors[key] = r["id"]
            seen.append((key, r["id"]))

        doomed = [rid for key, rid in seen if survivors[key] != rid]
        for i in range(0, len(doomed), _DELETE_CHUNK):
            chunk = doomed[i:i + _DELETE_CHUNK]
            marks = ",".join("?" for _ in chunk)
            await db.execute(
                f"DELETE FROM equity_snapshots WHERE id IN ({marks})", tuple(chunk))
            total += len(chunk)

    return total
