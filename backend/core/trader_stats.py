"""Trader leaderboard, consistency scoring, wallet screener, and trader_cache seeding.

The public leaderboard gives pnl/vol per trader; everything else (consistency,
win rate, open-position count) is derived locally from the trader's trade history
and current positions.

Consistency is a quality signal that rewards steady positive days and penalizes
volatility — so a flashy one-day whale ranks below a grinder. Daily realized PnL
is approximated from trades via average-cost accounting (positions held to
on-chain resolution aren't in the TRADE feed, so this is a proxy, not the
authoritative total — the leaderboard's pnl is authoritative for total).

Wallet screener (see UX_AND_WALLET_SCREENER_PLAN.md): win rate / pnl / volume /
consistency (green vs red days) / fill-exit ratio are all precomputed per trader
for three windows (7d/30d/90d) and cached in `trader_cache` on a schedule (see
`refresh_all` + the background loop in main.py). The screener endpoint filters
those cached columns directly — no live API calls per request — so combining
any number of filters simultaneously stays instant regardless of load.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import statistics
import time
from collections import defaultdict

from backend.db.database import now_iso

log = logging.getLogger("trader_stats")

_PERIODS = {"7d": 7, "30d": 30, "90d": 90}


def consistency_score(daily_pnl_series: list[float]) -> float:
    """0..1. Rewards win rate + risk-adjusted return + average daily PnL."""
    if len(daily_pnl_series) < 7:
        return 0.0
    n = len(daily_pnl_series)
    win_rate = sum(1 for x in daily_pnl_series if x > 0) / n
    mean = statistics.mean(daily_pnl_series)
    std = statistics.stdev(daily_pnl_series) if n > 1 else 1.0
    sharpe = (mean / std) if std > 0 else 0.0
    sharpe_norm = min(max(sharpe / 3.0, 0.0), 1.0)
    mean_norm = min(max(mean / 100.0, 0.0), 1.0)   # $100/day reference
    return round(win_rate * 0.4 + sharpe_norm * 0.4 + mean_norm * 0.2, 4)


def assign_tier(score: float) -> str:
    if score >= 0.75:
        return "diamond"
    if score >= 0.55:
        return "gold"
    if score >= 0.35:
        return "silver"
    return "bronze"


def _avg_cost_walk(trades):
    """Yield (day, realized_pnl, is_win) per closing SELL via average-cost basis."""
    books: dict[str, list[float]] = {}   # asset -> [shares, cost_total]
    for t in sorted(trades, key=lambda x: x.timestamp):
        day = dt.datetime.utcfromtimestamp(t.timestamp).strftime("%Y-%m-%d")
        b = books.setdefault(t.asset, [0.0, 0.0])
        if t.side == "BUY":
            b[0] += t.size
            b[1] += t.size * t.price
        elif t.side == "SELL" and b[0] > 0:
            avg = b[1] / b[0]
            sold = min(t.size, b[0])
            realized = (t.price - avg) * sold
            b[1] -= avg * sold
            b[0] -= sold
            yield day, realized, t.price > avg


def daily_realized_pnl(trades) -> dict[str, float]:
    daily: dict[str, float] = defaultdict(float)
    for day, realized, _ in _avg_cost_walk(trades):
        daily[day] += realized
    return dict(daily)


def trade_win_rate(trades) -> float:
    wins = total = 0
    for _, _, is_win in _avg_cost_walk(trades):
        total += 1
        wins += 1 if is_win else 0
    return wins / total if total else 0.0


def _period_metrics(closings: list[tuple], trades: list, days: int) -> dict:
    """Windowed screener metrics for one period, given the FULL-history closings
    walk (so avg-cost basis stays correct even for positions opened before the
    window) filtered down to the window, and the full trade list (for volume /
    fill / exit counts, which don't need cost-basis continuity).

    `fill_exit_ratio` = exits / fills * 100 (%): how much of what a trader opens
    within the window they actually close out again, vs. hold to resolution or
    let ride. Low % + high `pnl_quality` skew towards unrealized/paper gains;
    high % means the trader actively locks in outcomes.
    """
    cutoff_ts = time.time() - days * 86400
    cutoff_day = dt.datetime.utcfromtimestamp(cutoff_ts).strftime("%Y-%m-%d")

    in_window = [c for c in closings if c[0] >= cutoff_day]
    total_closes = len(in_window)
    wins = sum(1 for _, _, is_win in in_window if is_win)
    winrate = wins / total_closes if total_closes else 0.0
    pnl = sum(r for _, r, _ in in_window)

    daily: dict[str, float] = defaultdict(float)
    for day, r, _ in in_window:
        daily[day] += r
    green_days = sum(1 for v in daily.values() if v > 0)
    red_days = sum(1 for v in daily.values() if v < 0)
    consistency_ratio = green_days / (green_days + red_days) if (green_days + red_days) else 0.0

    recent = [t for t in trades if t.timestamp >= cutoff_ts]
    volume = sum(t.usd_size for t in recent)
    fills = sum(1 for t in recent if t.side == "BUY")
    exits = sum(1 for t in recent if t.side == "SELL")
    fill_exit_ratio = round(exits / fills * 100, 2) if fills else 0.0

    return {
        "winrate": round(winrate, 4),
        "pnl": round(pnl, 2),
        "volume": round(volume, 2),
        "green_days": green_days,
        "red_days": red_days,
        "consistency_ratio": round(consistency_ratio, 4),
        "fills": fills,
        "exits": exits,
        "fill_exit_ratio": fill_exit_ratio,
    }


async def _upsert(db, address: str, fields: dict) -> None:
    fields = {**fields, "last_refreshed": now_iso()}
    cols = ["address", *fields]
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in fields)
    await db.execute(
        f"INSERT INTO trader_cache({','.join(cols)}) VALUES({placeholders}) "
        f"ON CONFLICT(address) DO UPDATE SET {updates}",
        [address, *fields.values()],
    )


async def seed_from_leaderboard(db, pm, *, period="MONTH", order_by="PNL", limit=50) -> int:
    """Upsert pnl/vol/profile for the top traders (one API call). Cheap; run often."""
    entries = await pm.get_leaderboard(period=period, order_by=order_by, limit=limit)
    for e in entries:
        await _upsert(db, e.proxy_wallet, {
            "display_name": e.user_name,
            "profile_image": e.profile_image,
            "x_username": e.x_username,
            "verified": int(e.verified),
            "total_pnl": e.pnl,
            "volume_usd": e.vol,
        })
    return len(entries)


# Discovery feeds, deepest first. The leaderboard API pages at 50 rows and
# paginates far past rank 1000 (verified live 2026-07-02: MONTH×VOL offset 1900
# still returns wallets doing ~$70k/month). ALL-period feeds carry the
# LIFETIME pnl/vol, which is what total_pnl/volume_usd mean — the recency
# feeds (MONTH/WEEK/DAY) only contribute profile fields for wallets that are
# active right now but not big enough all-time.
_PAGE = 50
_DISCOVERY_FEEDS = (
    # (period, order_by, pages, carries_authoritative_totals)
    ("ALL",   "VOL", 12, True),
    ("ALL",   "PNL", 8,  True),
    ("MONTH", "VOL", 12, False),
    ("MONTH", "PNL", 8,  False),
    ("WEEK",  "VOL", 6,  False),
    ("WEEK",  "PNL", 4,  False),
    ("DAY",   "VOL", 4,  False),
)


async def discover_active_wallets(db, pm, *, target: int = 2000) -> int:
    """Crawl the public leaderboard feeds (period × ordering, paginated) and
    upsert every wallet found — this is what makes the screener cover the whole
    active-trader population instead of one top-25 page. Dedupes across feeds;
    stops early once `target` unique wallets have been seen this pass. Costs
    ~target/50 API calls; windowed stats are filled in later by `refresh_all`.
    """
    seen: set[str] = set()
    for period, order_by, pages, authoritative in _DISCOVERY_FEEDS:
        for page in range(pages):
            if len(seen) >= target:
                return len(seen)
            try:
                entries = await pm.get_leaderboard(
                    period=period, order_by=order_by, limit=_PAGE, offset=page * _PAGE)
            except Exception:
                log.exception("discovery page failed (%s %s p%d) — continuing",
                              period, order_by, page)
                continue
            if not entries:
                break                       # feed exhausted
            for e in entries:
                if e.proxy_wallet in seen:
                    continue
                seen.add(e.proxy_wallet)
                fields = {
                    "display_name": e.user_name,
                    "profile_image": e.profile_image,
                    "x_username": e.x_username,
                    "verified": int(e.verified),
                }
                if authoritative:           # lifetime numbers only from ALL feeds
                    fields["total_pnl"] = e.pnl
                    fields["volume_usd"] = e.vol
                await _upsert(db, e.proxy_wallet, fields)
    return len(seen)


async def refresh_trader_stats(address: str, db, pm) -> dict:
    """Enrich one trader: consistency, win rate, trade count, open positions, and
    the windowed screener metrics (winrate/pnl/volume/consistency/fill-exit ratio
    at 7d/30d/90d) + pnl_quality. Two API calls (trades + positions) — run for
    the traders shown on the board, and periodically for the whole cache (see
    `refresh_all`).

    total_pnl/volume_usd from the official leaderboard are authoritative (see
    module docstring) and are only filled in here if still unset — e.g. a
    manually-followed trader outside the seeded top-N. Never overwritten with
    this trade-window approximation, which would otherwise silently replace a
    whale's real lifetime PnL with whatever their last 500 trades sum to.
    """
    # 1000 = the endpoint's verified single-call max — for hyper-active whales
    # this may still cover fewer than 90 days; the windowed stats and daily
    # series honestly reflect whatever the window covers.
    trades = await pm.get_trade_history(address, limit=1000)
    positions = await pm.get_positions(address, size_threshold=0)
    open_positions = sum(1 for p in positions if p.size > 0 and not p.redeemable)
    unrealized = sum(p.cash_pnl for p in positions)

    closings = list(_avg_cost_walk(trades))
    series = [v for _, v in sorted(daily_realized_pnl(trades).items())]
    score = consistency_score(series)
    total_realized = sum(series)

    daily_all = daily_realized_pnl(trades)
    cutoff_90d = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=90)).strftime("%Y-%m-%d")
    daily_90d = {day: round(v, 2) for day, v in sorted(daily_all.items())
                 if day >= cutoff_90d}

    stats = {
        "consistency_score": score,
        "win_rate": round(trade_win_rate(trades), 4),
        "total_trades": len(trades),
        "open_positions": open_positions,
        "unrealized_pnl": round(unrealized, 2),
        # realized - unrealized: positive & large = gains are banked, not paper.
        # Very negative = trader is sitting on big unrealized winners that
        # haven't been proven closeable — a risk signal for a copier.
        "pnl_quality": round(total_realized - unrealized, 2),
        # per-day realized pnl for the per-card equity sparkline
        "daily_pnl_90d": json.dumps(daily_90d, separators=(",", ":")),
        "stats_refreshed_at": now_iso(),
    }
    existing = await db.fetchone(
        "SELECT total_pnl, volume_usd FROM trader_cache WHERE address = ?", (address,))
    if existing is None or existing["total_pnl"] is None:
        stats["total_pnl"] = round(total_realized + unrealized, 2)
    if existing is None or existing["volume_usd"] is None:
        stats["volume_usd"] = round(sum(t.usd_size for t in trades), 2)
    for period_key, days in _PERIODS.items():
        m = _period_metrics(closings, trades, days)
        stats[f"winrate_{period_key}"] = m["winrate"]
        stats[f"pnl_{period_key}"] = m["pnl"]
        stats[f"volume_{period_key}"] = m["volume"]
        stats[f"green_days_{period_key}"] = m["green_days"]
        stats[f"red_days_{period_key}"] = m["red_days"]
        stats[f"consistency_ratio_{period_key}"] = m["consistency_ratio"]
        stats[f"fills_{period_key}"] = m["fills"]
        stats[f"exits_{period_key}"] = m["exits"]
        stats[f"fill_exit_ratio_{period_key}"] = m["fill_exit_ratio"]

    await _upsert(db, address, stats)
    row = await db.fetchone("SELECT * FROM trader_cache WHERE address = ?", (address,))
    return {**row, "tier": assign_tier(score)}


async def refresh_all(db, pm, *, limit: int = 200, concurrency: int = 8) -> int:
    """Recompute windowed stats for a batch of cached traders, prioritizing
    (1) wallets that have never been enriched, then (2) the stalest — so the
    refresh loop ROTATES through the whole discovered population instead of
    re-polishing the same top-N forever (the bug that kept every wallet
    outside the top 100 permanently statless). Batches run concurrently
    (bounded); 2 API calls per wallet. Meant for the background loop in
    main.py, not per-request."""
    rows = await db.fetchall(
        "SELECT address FROM trader_cache "
        "ORDER BY (stats_refreshed_at IS NULL) DESC, stats_refreshed_at ASC "
        "LIMIT ?", (limit,))
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(address: str) -> None:
        nonlocal done
        async with sem:
            try:
                await refresh_trader_stats(address, db, pm)
                done += 1
            except Exception:
                log.exception("windowed stats refresh failed for %s", address)

    await asyncio.gather(*(one(r["address"]) for r in rows))
    return done


_SORT_COLS = {
    "consistency": "consistency_score",
    "pnl": "total_pnl",
    "winrate": "win_rate",
    "volume": "volume_usd",
    "pnl_quality": "pnl_quality",
    "pnl_7d": "pnl_7d",
    "pnl_30d": "pnl_30d",
    "pnl_90d": "pnl_90d",
    "winrate_7d": "winrate_7d",
    "winrate_30d": "winrate_30d",
    "winrate_90d": "winrate_90d",
    "volume_7d": "volume_7d",
    "volume_30d": "volume_30d",
    "volume_90d": "volume_90d",
    "fill_exit_ratio_7d": "fill_exit_ratio_7d",
    "fill_exit_ratio_30d": "fill_exit_ratio_30d",
    "fill_exit_ratio_90d": "fill_exit_ratio_90d",
}

# Whitelist of columns the screener is allowed to filter on — every windowed
# metric plus the original all-time ones. Query params are `<column>_min` /
# `<column>_max`; anything not in this set is ignored (defense against
# injection AND against filtering on arbitrary/internal columns).
_FILTERABLE_COLUMNS = frozenset({
    "winrate_7d", "winrate_30d", "winrate_90d",
    "pnl_7d", "pnl_30d", "pnl_90d",
    "volume_7d", "volume_30d", "volume_90d",
    "consistency_ratio_7d", "consistency_ratio_30d", "consistency_ratio_90d",
    "fill_exit_ratio_7d", "fill_exit_ratio_30d", "fill_exit_ratio_90d",
    "pnl_quality", "total_pnl", "win_rate", "volume_usd", "consistency_score",
    "open_positions",
})


def parse_screener_filters(query_params) -> dict[str, tuple[str, str, float]]:
    """Extract whitelisted `<col>_min` / `<col>_max` filters from a mapping of
    raw query params (e.g. FastAPI's `Request.query_params`). Returns
    {param_key: (column, sql_op, value)} — ready to be AND'd together by
    `get_leaderboard`. Unknown keys and unparseable values are silently
    dropped rather than erroring, so unrelated query params (sort, limit, ...)
    can share the same query string."""
    out: dict[str, tuple[str, str, float]] = {}
    for key, raw in dict(query_params).items():
        for suffix, op in (("_min", ">="), ("_max", "<=")):
            if key.endswith(suffix):
                col = key[: -len(suffix)]
                if col in _FILTERABLE_COLUMNS:
                    try:
                        out[key] = (col, op, float(raw))
                    except (TypeError, ValueError):
                        pass
                break
    return out


async def get_leaderboard(
    db,
    sort_by: str = "consistency",
    limit: int = 50,
    offset: int = 0,
    filters: dict[str, tuple[str, str, float]] | None = None,
    search: str | None = None,
) -> list[dict]:
    """Leaderboard / wallet screener. `filters` (see `parse_screener_filters`)
    combine with AND — pass as many as you like simultaneously; this is a
    single indexed query over precomputed columns, so cost doesn't grow with
    the number of active filters. `search` substring-matches the wallet
    address, display name, or X username (case-insensitive, parameterized)."""
    col = _SORT_COLS.get(sort_by, "consistency_score")   # whitelist (no injection)
    clauses: list[str] = []
    params: list = []
    if filters:
        clauses += [f"{fcol} {op} ?" for fcol, op, _ in filters.values()]
        params += [val for _, _, val in filters.values()]
    if search and search.strip():
        term = f"%{search.strip()}%"
        clauses.append("(address LIKE ? OR display_name LIKE ? OR x_username LIKE ?)")
        params += [term, term, term]
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = await db.fetchall(
        f"SELECT * FROM trader_cache {where_sql} ORDER BY {col} DESC LIMIT ? OFFSET ?",
        [*params, limit, offset])
    for r in rows:
        r["tier"] = assign_tier(r.get("consistency_score") or 0.0)
    return rows
