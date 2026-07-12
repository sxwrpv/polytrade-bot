"""User PnL — equity curve and period stats for a copytrader's own history.

Realized PnL and the equity curve come from the DB (copy_positions /
trade_events written by the copy engine). Unrealized PnL for still-open positions
is read live from the data API (the user's wallet positions carry cashPnl), so we
never recompute current value ourselves — pass a PolymarketClient to include it.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict


def _cutoff_iso(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()


async def _realized_since(db, user_id: str, days: int) -> float:
    cutoff = _cutoff_iso(days)
    val = await db.fetchval(
        "SELECT COALESCE(SUM(pnl), 0) FROM trade_events "
        "WHERE user_id = ? AND pnl IS NOT NULL AND ts >= ?",
        (user_id, cutoff))
    # Legacy closed rows predate PnL event recording — date them by closed_at
    # so the windowed numbers cover the same ground as the all-time total.
    legacy = await db.fetchval(
        "SELECT COALESCE(SUM(p.realized_pnl), 0) FROM copy_positions p "
        "WHERE p.user_id = ? AND p.status IN ('closed', 'resolved') "
        "AND p.closed_at >= ? AND NOT EXISTS("
        "SELECT 1 FROM trade_events e WHERE e.position_id = p.id AND e.pnl IS NOT NULL)",
        (user_id, cutoff))
    return float(val or 0.0) + float(legacy or 0.0)


async def get_pnl_stats(user_id: str, db, pm=None) -> dict:
    closed = await db.fetchall(
        "SELECT p.realized_pnl, "
        "(SELECT COUNT(e.pnl) FROM trade_events e WHERE e.position_id=p.id) AS pnl_events, "
        "(SELECT COALESCE(SUM(e.pnl),0) FROM trade_events e WHERE e.position_id=p.id) AS event_pnl "
        "FROM copy_positions p WHERE p.user_id = ? AND p.status IN ('closed', 'resolved')",
        (user_id,))
    # Event PnL includes partial exits plus the final close/resolve. Legacy rows
    # may predate event recording, so fall back to the stored row total only when
    # no PnL event exists for that position.
    per_position = [
        float(r["event_pnl"] if int(r["pnl_events"] or 0) else (r["realized_pnl"] or 0.0))
        for r in closed
    ]
    wins = sum(1 for x in per_position if x > 0)
    # Total realized counts EVERY booked PnL event — including partial exits on
    # positions that are still open — plus legacy closed rows that predate event
    # recording. This keeps realized_pnl consistent with pnl_7d/pnl_30d (event-
    # based), the by-wallet breakdown, and the equity curve; per_position above
    # stays completed-positions-only for the win/best/worst stats.
    event_total = float(await db.fetchval(
        "SELECT COALESCE(SUM(pnl), 0) FROM trade_events "
        "WHERE user_id = ? AND pnl IS NOT NULL", (user_id,)) or 0.0)
    legacy_total = float(await db.fetchval(
        "SELECT COALESCE(SUM(p.realized_pnl), 0) FROM copy_positions p "
        "WHERE p.user_id = ? AND p.status IN ('closed', 'resolved') AND NOT EXISTS("
        "SELECT 1 FROM trade_events e WHERE e.position_id = p.id AND e.pnl IS NOT NULL)",
        (user_id,)) or 0.0)
    total_realized = event_total + legacy_total

    unrealized = 0.0
    if pm is not None:
        positions = await pm.get_positions(user_id, size_threshold=0)
        # OPEN positions only. Resolved-but-held (`redeemable`) positions are
        # realized outcomes already booked as closed rows — counting their
        # cash_pnl here too would double-book them into total_pnl.
        unrealized = sum(p.cash_pnl for p in positions
                         if p.size > 0 and not p.redeemable)

    return {
        "total_pnl": round(total_realized + unrealized, 2),
        "realized_pnl": round(total_realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "pnl_7d": round(await _realized_since(db, user_id, 7), 2),
        "pnl_30d": round(await _realized_since(db, user_id, 30), 2),
        "win_rate": round(wins / len(per_position), 4) if per_position else 0.0,
        "total_trades": len(per_position),
        "best_trade": round(max(per_position), 2) if per_position else 0.0,
        "worst_trade": round(min(per_position), 2) if per_position else 0.0,
    }


async def get_equity_curve(user_id: str, db, period: str = "30d") -> list[dict]:
    days = {"7d": 7, "30d": 30, "all": 3650}.get(period, 30)
    rows = await db.fetchall(
        "SELECT ts, pnl FROM trade_events "
        "WHERE user_id = ? AND pnl IS NOT NULL AND ts >= ? ORDER BY ts",
        (user_id, _cutoff_iso(days)))
    daily: dict[str, float] = defaultdict(float)
    for r in rows:
        daily[r["ts"][:10]] += float(r["pnl"] or 0.0)   # ISO date prefix
    cum = 0.0
    out = []
    for day in sorted(daily):
        cum += daily[day]
        out.append({"date": day, "pnl": round(daily[day], 2),
                    "cumulative_pnl": round(cum, 2)})
    return out


async def get_pnl_by_wallet(user_id: str, db) -> list[dict]:
    """Realized PnL grouped by copied trader — answers "which of my copied
    wallets is actually making money" (User > Performance > breakdown)."""
    # Per-position PnL uses the same event-based accounting as get_pnl_stats
    # (partial exits included; row realized_pnl only as the legacy fallback), so
    # the breakdown always sums to the headline realized number. Open positions
    # with booked partial-exit PnL contribute too, but only completed positions
    # count toward closed_trades / win rate.
    rows = await db.fetchall(
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
        # c.display_name grouped too: 1:1 with trader_address, and Postgres
        # (unlike SQLite) rejects a bare non-aggregated column in SELECT.
        "GROUP BY p.trader_address, c.display_name ORDER BY realized_pnl DESC",
        (user_id,))
    out = []
    for r in rows:
        trades = int(r["closed_trades"] or 0)
        wins = int(r["wins"] or 0)
        out.append({
            "trader_address": r["trader_address"],
            "display_name": r["display_name"],
            "realized_pnl": round(float(r["realized_pnl"] or 0.0), 2),
            "closed_trades": trades,
            "win_rate": round(wins / trades, 4) if trades else 0.0,
        })
    return out
