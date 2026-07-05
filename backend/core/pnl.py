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
    val = await db.fetchval(
        "SELECT COALESCE(SUM(pnl), 0) FROM trade_events "
        "WHERE user_id = ? AND pnl IS NOT NULL AND ts >= ?",
        (user_id, _cutoff_iso(days)))
    return float(val or 0.0)


async def get_pnl_stats(user_id: str, db, pm=None) -> dict:
    closed = await db.fetchall(
        "SELECT realized_pnl FROM copy_positions "
        "WHERE user_id = ? AND status IN ('closed', 'resolved')", (user_id,))
    realized = [float(r["realized_pnl"] or 0.0) for r in closed]
    total_realized = sum(realized)
    wins = sum(1 for x in realized if x > 0)

    unrealized = 0.0
    if pm is not None:
        positions = await pm.get_positions(user_id, size_threshold=0)
        unrealized = sum(p.cash_pnl for p in positions)

    return {
        "total_pnl": round(total_realized + unrealized, 2),
        "realized_pnl": round(total_realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "pnl_7d": round(await _realized_since(db, user_id, 7), 2),
        "pnl_30d": round(await _realized_since(db, user_id, 30), 2),
        "win_rate": round(wins / len(realized), 4) if realized else 0.0,
        "total_trades": len(realized),
        "best_trade": round(max(realized), 2) if realized else 0.0,
        "worst_trade": round(min(realized), 2) if realized else 0.0,
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
    rows = await db.fetchall(
        "SELECT p.trader_address, c.display_name, "
        "COALESCE(SUM(p.realized_pnl), 0) AS realized_pnl, "
        "COUNT(*) AS closed_trades, "
        "SUM(CASE WHEN p.realized_pnl > 0 THEN 1 ELSE 0 END) AS wins "
        "FROM copy_positions p "
        "LEFT JOIN trader_cache c ON c.address = p.trader_address "
        "WHERE p.user_id = ? AND p.status IN ('closed', 'resolved') "
        "GROUP BY p.trader_address ORDER BY realized_pnl DESC",
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
