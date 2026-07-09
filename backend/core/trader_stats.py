"""Trader leaderboard, consistency scoring, wallet screener, and trader_cache seeding.

The public leaderboard gives pnl/vol per trader; everything else (consistency,
win rate, open-position count) is derived locally from the trader's trade history
and current positions.

Consistency is a quality signal that rewards steady positive days and penalizes
volatility — so a flashy one-day whale ranks below a grinder. Daily realized PnL
uses average-cost accounting over the merged TRADE + REDEEM streams, with
expired (resolved-and-lost) holdings realized as losses — see
`realized_closings`. Still an approximation (basis older than the fetched
window is skipped); the leaderboard's pnl stays authoritative for lifetime
totals.

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


def _day(ts: float) -> str:
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def realized_closings(trades, redeems=(), positions=(), *,
                      positions_truncated: bool = False):
    """All realized outcomes as [(day, realized_pnl, is_win)], avg-cost basis.

    Four ways a position realizes, and ALL must count or the stats lie. The
    failure that motivated this (seen live 2026-07-05, wallet 0xe221…06f6):
    a hold-to-resolution trader showed 94% WR / zero red days / +$907k because
    only wins were visible — its 18 resolved LOSING positions (−$622k) sit in
    the wallet as `redeemable` leftovers with cur_price 0: no SELL, no REDEEM
    record (losers have nothing to claim), so they were invisible. Official
    lifetime pnl was +$286k, not +$907k.

      1. SELL             — realized vs. avg cost (the classic walk).
      2. REDEEM           — resolved & claimed (a win): payout − cost basis of
                            the condition's tokens (per-condition netting is
                            exact even for both-sides holders).
      3. resolved holding — a `redeemable` position still in the wallet
                            (resolved, not yet claimed). WIN or LOSS. Uses the
                            API's own cash_pnl (current_value − initial_value),
                            which is authoritative even when the cost basis
                            predates the fetched trade window. This is the leg
                            that was missing.
      4. expired-away     — bought, resolved, and no longer in the wallet at
                            all (not sold, not redeemed, not a current
                            position): residual cost basis realized as a loss,
                            dated to the last trade. Skipped when the positions
                            list was truncated (absence then proves nothing).
    """
    books: dict[str, list[float]] = {}          # asset -> [shares, cost_total]
    cond_assets: dict[str, set] = defaultdict(set)   # condition -> assets traded
    last_ts: dict[str, float] = {}
    events = [(t.timestamp, 0, t) for t in trades]
    for r in redeems:
        events.append((int(r.get("timestamp") or 0), 1, r))
    events.sort(key=lambda e: (e[0], e[1]))      # redeem after same-second trades
    out: list[tuple[str, float, bool]] = []
    for ts, kind, obj in events:
        if kind == 0:                            # TRADE
            b = books.setdefault(obj.asset, [0.0, 0.0])
            cond_assets[obj.condition_id].add(obj.asset)
            last_ts[obj.asset] = ts
            if obj.side == "BUY":
                b[0] += obj.size
                b[1] += obj.size * obj.price
            elif obj.side == "SELL" and b[0] > 0:
                avg = b[1] / b[0]
                sold = min(obj.size, b[0])
                b[1] -= avg * sold
                b[0] -= sold
                out.append((_day(ts), (obj.price - avg) * sold, obj.price > avg))
        else:                                    # REDEEM
            cid = str(obj.get("conditionId", ""))
            payout = float(obj.get("usdcSize") or 0.0)
            cost = 0.0
            for a in cond_assets.get(cid, ()):
                sh, c = books.get(a, (0.0, 0.0))
                cost += c
                books[a] = [0.0, 0.0]
            if cost > 0:                         # basis known -> realize
                out.append((_day(ts), payout - cost, payout > cost))

    # 3. resolved-but-held positions (the missing loss leg). cash_pnl from the
    #    API is authoritative; date to the last trade on the token (proxy for
    #    the resolution date — these are fast markets that resolve near the last
    #    buy) or now if the buys predate the fetched window.
    held_assets: set = set()
    for p in positions:
        if getattr(p, "size", 0) <= 0.01:
            continue
        if p.redeemable:
            books[p.asset] = [0.0, 0.0]          # consumed -> no expired double-count
            ts = last_ts.get(p.asset, time.time())
            out.append((_day(ts), p.cash_pnl, p.cur_price >= 0.5))
        else:
            held_assets.add(p.asset)             # genuinely open -> leave basis

    # 4. expired-away: cost basis on a token that is neither still held nor
    #    resolved-in-wallet -> realize the loss (unless the list was truncated).
    if not positions_truncated:
        for a, (sh, c) in books.items():
            if sh > 0.01 and c > 0.005 and a not in held_assets:
                out.append((_day(last_ts.get(a, 0)), -c, False))

    out.sort(key=lambda e: e[0])
    return out


def daily_realized_pnl(closings) -> dict[str, float]:
    daily: dict[str, float] = defaultdict(float)
    for day, realized, _ in closings:
        daily[day] += realized
    return dict(daily)


def win_rate_of(closings) -> float:
    wins = sum(1 for _, _, is_win in closings if is_win)
    return wins / len(closings) if closings else 0.0


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


def clean_display_name(name: str | None) -> str | None:
    """Polymarket auto-generates '0x<signer>-<timestamp>' userNames for wallets
    that never set one — treat those as no name so every UI surface falls back
    to the short address instead of a 60-char blob."""
    if not name or name.startswith("0x"):
        return None
    return name


async def _upsert(db, address: str, fields: dict) -> None:
    if "display_name" in fields:
        fields = {**fields, "display_name": clean_display_name(fields["display_name"])}
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


_PAGE_SIZE = 1000        # activity endpoint's verified single-call max
_MAX_TRADE_PAGES = 4     # up to 4000 trades — covers 90d for all but extreme whales
_MAX_REDEEM_PAGES = 2
_POSITIONS_LIMIT = 500


async def _fetch_activity_window(fetch, days: int, max_pages: int) -> tuple[list, bool]:
    """Page through a most-recent-first activity fetcher until the window is
    covered or the page budget runs out. Returns (rows, covered): covered=False
    means the wallet is so active the oldest fetched row is still inside the
    window — stats then honestly reflect partial coverage (surfaced to the UI
    via history_days)."""
    cutoff = time.time() - days * 86400
    rows: list = []
    for page in range(max_pages):
        batch = await fetch(limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)
        rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return rows, True                    # feed exhausted — full coverage
        oldest = batch[-1] if not isinstance(batch[-1], dict) else None
        oldest_ts = (oldest.timestamp if oldest is not None
                     else int(batch[-1].get("timestamp") or 0))
        if oldest_ts < cutoff:
            return rows, True
    return rows, False


async def refresh_trader_stats(address: str, db, pm) -> dict:
    """Enrich one trader: consistency, win rate, trade count, open positions, and
    the windowed screener metrics (winrate/pnl/volume/consistency/fill-exit ratio
    at 7d/30d/90d) + pnl_quality. 3-8 API calls (paginated trades + redeems +
    positions) — run for the traders shown on the board, and periodically for
    the whole cache (see `refresh_all`).

    total_pnl/volume_usd come from the leaderboard's per-user filter — the
    official lifetime numbers polymarket.com shows, fetched fresh on every
    refresh. The trade-walk approximation is only the fallback for wallets the
    leaderboard endpoint doesn't know at all.
    """
    trades, trades_covered = await _fetch_activity_window(
        lambda limit, offset: pm.get_trade_history(address, limit=limit, offset=offset),
        days=90, max_pages=_MAX_TRADE_PAGES)
    redeems, _ = await _fetch_activity_window(
        lambda limit, offset: pm.get_redeems(address, limit=limit, offset=offset),
        days=90, max_pages=_MAX_REDEEM_PAGES)
    positions = await pm.get_positions(address, size_threshold=0,
                                       limit=_POSITIONS_LIMIT)
    open_positions = sum(1 for p in positions if p.size > 0 and not p.redeemable)
    # unrealized = OPEN positions only. Resolved-but-held (`redeemable`)
    # positions are realized outcomes, not paper — counting their cash_pnl here
    # would double-book them against the realized closings below.
    unrealized = sum(p.cash_pnl for p in positions
                     if p.size > 0 and not p.redeemable)
    # truncated list -> can't prove a token is "gone", so skip expired-away
    positions_truncated = len(positions) >= _POSITIONS_LIMIT

    closings = realized_closings(trades, redeems, positions,
                                 positions_truncated=positions_truncated)
    daily_all = daily_realized_pnl(closings)
    series = [v for _, v in sorted(daily_all.items())]
    score = consistency_score(series)
    total_realized = sum(series)

    # how far back the fetched history actually reaches: 90 = the whole window
    # is covered; less = the page budget ran out first (hyper-active wallet),
    # and the UI flags any period wider than this as partial data
    if trades_covered:
        history_days = 90.0
    else:
        oldest_ts = min((t.timestamp for t in trades), default=time.time())
        history_days = round((time.time() - oldest_ts) / 86400, 1)
    cutoff_90d = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=90)).strftime("%Y-%m-%d")
    daily_90d = {day: round(v, 2) for day, v in sorted(daily_all.items())
                 if day >= cutoff_90d}

    stats = {
        "consistency_score": score,
        "win_rate": round(win_rate_of(closings), 4),
        "total_trades": len(trades),
        "open_positions": open_positions,
        "unrealized_pnl": round(unrealized, 2),
        # realized - unrealized: positive & large = gains are banked, not paper.
        # Very negative = trader is sitting on big unrealized winners that
        # haven't been proven closeable — a risk signal for a copier.
        "pnl_quality": round(total_realized - unrealized, 2),
        # per-day realized pnl for the per-card equity sparkline
        "daily_pnl_90d": json.dumps(daily_90d, separators=(",", ":")),
        "history_days": history_days,
        "stats_refreshed_at": now_iso(),
    }
    # lifetime pnl/vol: the leaderboard's per-user filter returns the official
    # numbers (what polymarket.com shows) for ANY wallet, ranked or not — use
    # them whenever available; fall back to our walk approximation only when
    # the endpoint doesn't know the wallet.
    try:
        official = await pm.get_leaderboard_user(address)
    except Exception:
        log.exception("official pnl lookup failed for %s (using approximation)", address)
        official = None
    if official is not None:
        stats["total_pnl"] = official.pnl
        stats["volume_usd"] = official.vol
        if official.user_name:
            stats["display_name"] = official.user_name
        if official.x_username:
            stats["x_username"] = official.x_username
    else:
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
    (bounded); 3-8 API calls per wallet (paginated trades + redeems +
    positions). Meant for the background loop in main.py, not per-request."""
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
