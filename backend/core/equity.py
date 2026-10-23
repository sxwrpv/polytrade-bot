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

import asyncio
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

# The values that make two snapshots the same reading.
_VALUE_COLUMNS = ("equity", "balance", "positions_value", "realized_pnl", "unrealized_pnl")
# Under half a cent rounds to $0.00 everywhere the account is shown.
_EMPTY_USD = 0.005


def _is_empty(row) -> bool:
    """An account with no cash and nothing held. Its next reading can only
    repeat this one until money arrives, so a run of them is stored once and
    get_series draws the rest of the run from that row."""
    equity, held = row.get("equity"), row.get("positions_value")
    return (equity is not None and held is not None
            and abs(float(equity)) < _EMPTY_USD and abs(float(held)) < _EMPTY_USD)


def _same_reading(a, b) -> bool:
    return all(a.get(c) is not None and b.get(c) is not None
               and round(float(a[c]), 2) == round(float(b[c]), 2) for c in _VALUE_COLUMNS)


async def _cumulative_realized(db, user_id: str) -> float:
    val = await db.fetchval(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM copy_positions "
        "WHERE user_id = ? AND status IN ('closed', 'resolved')", (user_id,))
    return float(val or 0.0)


async def take_snapshot(db, user_id: str, client, pm) -> dict | None:
    """Read the account's current worth and persist one snapshot row.

    equity = free cash + market value of everything held (open positions plus
    resolved-but-unredeemed winners). Returns the snapshot, or None if either
    read failed (we don't store a half-known equity: a failed positions read
    used to be stored as cash-only equity, a fake crash on the chart)."""
    try:
        bal = await client.get_balance_allowance(asset_type="COLLATERAL")
        balance = bal.balance / 1e6
    except Exception:
        log.exception("snapshot: balance read failed for %s", user_id[:10])
        return None
    try:
        positions, complete = await pm.get_all_positions(user_id, size_threshold=0)
        if not complete:
            return None  # Never persist a truncated portfolio as total equity.
    except Exception:
        log.exception("snapshot: positions read failed for %s", user_id[:10])
        return None
    held = [p for p in positions if p.size > 0.01]
    positions_value = round(sum(p.current_value for p in held), 2)
    unrealized = round(sum(p.cash_pnl for p in held if not p.redeemable), 2)
    realized = round(await _cumulative_realized(db, user_id), 2)
    equity = round(balance + positions_value, 2)
    ts = now_iso()
    snapshot = {"ts": ts, "equity": equity, "balance": round(balance, 2),
                "positions_value": positions_value, "realized_pnl": realized,
                "unrealized_pnl": unrealized}
    if _is_empty(snapshot):
        # An empty account repeats its reading every pass, so only the first
        # of a run is stored; get_series carries it forward to now.
        last = await db.fetchone(
            "SELECT equity, balance, positions_value, realized_pnl, unrealized_pnl "
            "FROM equity_snapshots WHERE user_id = ? ORDER BY ts DESC LIMIT 1", (user_id,))
        if last is not None and _same_reading(last, snapshot):
            return snapshot
    await db.execute(
        "INSERT INTO equity_snapshots(user_id, ts, equity, balance, positions_value, "
        "realized_pnl, unrealized_pnl) VALUES(?,?,?,?,?,?,?)",
        (user_id, ts, equity, round(balance, 2), positions_value, realized, unrealized))
    return snapshot


async def snapshot_all(db, pm, client_for, *, concurrency: int = 1) -> int:
    """Snapshot every user that has a wallet. `client_for(user_row)` returns an
    authenticated CLOB client (cached upstream). Best-effort per user — one
    failure never blocks the rest. Up to `concurrency` users at once: each
    snapshot is two upstream round trips, so a sequential pass stops fitting
    inside the snapshot interval once there are a few hundred users."""
    users = await db.fetchall("SELECT * FROM users")
    gate = asyncio.Semaphore(max(1, int(concurrency)))
    done = 0

    async def one(user) -> None:
        nonlocal done
        async with gate:
            try:
                client = await client_for(user)
                if await take_snapshot(db, user["id"], client, pm) is not None:
                    done += 1
            except Exception:
                log.exception("snapshot failed for %s", user["id"][:10])

    await asyncio.gather(*(one(user) for user in users))
    return done


def _epoch(ts: str) -> float:
    try:
        return dt.datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


def _point(r) -> dict:
    return {
        "ts": r["ts"],
        "equity": round(float(r["equity"] or 0.0), 2),
        "balance": round(float(r["balance"] or 0.0), 2),
        # total PnL at that instant = realized to date + open-position mark
        "pnl": round(float(r["realized_pnl"] or 0.0) + float(r["unrealized_pnl"] or 0.0), 2),
    }


async def get_series(db, user_id: str, period: str = "7d", *,
                     now: dt.datetime | None = None) -> list[dict]:
    """Downsampled equity/PnL series for the chart. One point per time bucket
    (last snapshot in the bucket wins), so 30d/all stay light and readable
    while 7d keeps full 5-min resolution.

    An empty account stores only the first snapshot of a run (take_snapshot),
    so every bucket after an empty point, up to now, repeats that point. The
    chart draws the same line the per-pass rows drew. A bucket after a point
    with money in it stays a gap, as before: a missing reading there means the
    read failed, and the value is unknown."""
    days, bucket = _BUCKETS.get(period, _BUCKETS["7d"])
    reference = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (reference - dt.timedelta(days=days)).isoformat()
    columns = "ts, equity, balance, positions_value, realized_pnl, unrealized_pnl"
    rows = await db.fetchall(
        f"SELECT {columns} FROM equity_snapshots "
        "WHERE user_id = ? AND ts >= ? ORDER BY ts", (user_id, cutoff))
    # An empty run that began before the window fills the window from its start.
    before = await db.fetchone(
        f"SELECT {columns} FROM equity_snapshots "
        "WHERE user_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1", (user_id, cutoff))
    by_bucket: dict[int, tuple[dict, bool]] = {}
    for r in rows:
        by_bucket[int(_epoch(r["ts"]) // bucket)] = (_point(r), _is_empty(r))

    def repeat(point: dict, key: int) -> dict:
        start = dt.datetime.fromtimestamp(key * bucket, dt.timezone.utc)
        return {**point, "ts": start.isoformat()}

    series: list[dict] = []
    carry = _point(before) if before is not None and _is_empty(before) else None
    position = int(_epoch(cutoff) // bucket)
    for key in sorted(by_bucket):
        point, empty = by_bucket[key]
        if carry is not None:
            series.extend(repeat(carry, k) for k in range(position, key))
        series.append(point)
        carry = point if empty else None
        position = key + 1
    if carry is not None:
        series.extend(repeat(carry, k) for k in range(position, int(reference.timestamp() // bucket) + 1))
    return series


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


async def prune_flat_snapshots(db, *, scan_limit: int = COMPACTION_SCAN_LIMIT) -> int:
    """Delete each empty-account snapshot that repeats the snapshot before it,
    keeping the first row of every run. These are the rows take_snapshot no
    longer writes, left from before it skipped them; get_series redraws the
    run from its first row, so the chart does not change.

    Bounded by `scan_limit` per call and safe to repeat: a removed row's
    successor then follows an identical row too, so later passes finish the
    run, and a run's first row never qualifies."""
    rows = await db.fetchall(
        "SELECT id FROM ("
        " SELECT id, equity, balance, positions_value, realized_pnl, unrealized_pnl,"
        "  LAG(id) OVER w AS prev_id, LAG(equity) OVER w AS prev_equity,"
        "  LAG(balance) OVER w AS prev_balance, LAG(positions_value) OVER w AS prev_held,"
        "  LAG(realized_pnl) OVER w AS prev_realized, LAG(unrealized_pnl) OVER w AS prev_unrealized"
        " FROM equity_snapshots WINDOW w AS (PARTITION BY user_id ORDER BY ts, id)"
        ") runs "
        "WHERE prev_id IS NOT NULL AND ABS(equity) < ? AND ABS(positions_value) < ? "
        "AND equity = prev_equity AND balance = prev_balance AND positions_value = prev_held "
        "AND realized_pnl = prev_realized AND unrealized_pnl = prev_unrealized "
        "ORDER BY id LIMIT ?", (_EMPTY_USD, _EMPTY_USD, scan_limit))
    doomed = [r["id"] for r in rows]
    for i in range(0, len(doomed), _DELETE_CHUNK):
        chunk = doomed[i:i + _DELETE_CHUNK]
        marks = ",".join("?" for _ in chunk)
        await db.execute(f"DELETE FROM equity_snapshots WHERE id IN ({marks})", tuple(chunk))
    return len(doomed)
