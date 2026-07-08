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
