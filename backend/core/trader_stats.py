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
import math
import statistics
import time
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from typing import TYPE_CHECKING, Any

from backend.db.database import now_iso

if TYPE_CHECKING:
    from backend.core.polymarket import Position, Trade

log = logging.getLogger("trader_stats")

_PERIODS = {"7d": 7, "30d": 30, "90d": 90}

# Canonical, machine-readable meaning of every metric currently presented by
# the wallet screener. Keep this beside the calculations: UI copy and external
# documentation may consume it, but neither is allowed to give a metric a
# stronger provenance than the implementation below actually provides.
_TRADE_ENDPOINT = "GET https://data-api.polymarket.com/activity?user={address}&type=TRADE"
_REDEEM_ENDPOINT = "GET https://data-api.polymarket.com/activity?user={address}&type=REDEEM"
_POSITIONS_ENDPOINT = "GET https://data-api.polymarket.com/positions?user={address}"
_LEADERBOARD_USER_ENDPOINT = (
    "GET https://data-api.polymarket.com/v1/leaderboard?category=OVERALL&"
    "timePeriod=ALL&orderBy=PNL&user={address}"
)
_ACTIVITY_ENDPOINTS = (_TRADE_ENDPOINT, _REDEEM_ENDPOINT, _POSITIONS_ENDPOINT)
_FETCH_LIMITS = {
    "activity_page_size": 1000,
    "trade_max_pages": 4,
    "redeem_max_pages": 2,
    "positions_limit": 500,
}
_TRADE_LIMITS = {"activity_page_size": 1000, "trade_max_pages": 4}
_POSITIONS_LIMITS = {"positions_limit": 500}
_REFRESH_CADENCE = {
    "default_interval_seconds": 900,
    "default_batch_size": 200,
    "stale_first_rotation": True,
    "scheduled_enabled_by_default": True,
    "runs_immediately_on_startup": True,
    "interval_env_var": "TRADER_STATS_REFRESH_SECONDS",
    "batch_size_env_var": "TRADER_STATS_REFRESH_LIMIT",
    "interval_and_batch_configurable": True,
    "autostart_env_var": "STATS_REFRESH_AUTOSTART",
    "disable_value": "0",
    "on_demand_refresh_endpoint": "GET /traders/{address}",
    "on_demand_refreshes_immediately": True,
    "guarantee": "No per-wallet SLA: each pass refreshes only the stalest batch.",
}
_NULL = {
    "value": "null",
    "reason": (
        "Cache null means not fetched or not computable and is unavailable, not numeric "
        "zero (except the legacy open_positions DEFAULT 0 before stats_refreshed_at). "
        "Current TraderCard rendering and tier assignment coerce some missing values to "
        "zero/bronze; that consumer behavior is unsafe and remains a current limitation."
    ),
}
_COMBINED_PARTIAL = {
    "status": "partial",
    "truncation_risks": ["TRADE", "REDEEM", "positions"],
    "reason": (
        "Page budgets can truncate TRADE/REDEEM history and the 500-row positions "
        "snapshot. history_days detects only truncated TRADE coverage."
    ),
}
_TRADE_PARTIAL = {
    "status": "partial",
    "truncation_risks": ["TRADE"],
    "reason": "The four-page TRADE budget can truncate fetched activity.",
}
_POSITIONS_PARTIAL = {
    "status": "partial",
    "truncation_risks": ["positions"],
    "reason": "The 500-row positions snapshot can be truncated.",
}
_UNAVAILABLE_PARTIAL = {
    "status": "unavailable",
    "truncation_risks": [],
    "reason": "Null until the first successful enrichment.",
}
_LEGACY_TRANSITION = (
    "Existing cache rows written before nullable ratio metrics may contain numeric zero "
    "sentinels indistinguishable from genuine observed zero until each wallet is refreshed; "
    "stale-first rotation has no per-wallet completion SLA."
)
_REALIZED_CLOSING_RULES = (
    "Closing events are reconstructed as: SELL only with known basis (held shares > 0), "
    "using min(SELL size, held shares), PnL=(SELL price-average cost)*sold shares, and win "
    "when SELL price > average cost; REDEEM only with known cost > 0, PnL=payout-cost, "
    "and win when payout > cost; redeemable held positions only when size > 0.01, "
    "using cash_pnl and win when cur_price >= 0.5; expired-away losses only when "
    "shares > 0.01, cost > 0.005, the asset is not held, and the positions list is "
    "not truncated. Resolved holdings without a fetched last-trade timestamp remain "
    "undated: they contribute to fetched aggregate PnL/win rate but not daily or rolling windows."
)


def _metric_contract(*, formula, source_endpoint=_ACTIVITY_ENDPOINTS,
                     time_window, provenance="reconstructed", sortable=False,
                     filterable=False, label, tooltip, limits=_FETCH_LIMITS,
                     partial_behavior=_COMBINED_PARTIAL):
    return {
        "formula": formula,
        "source_endpoint": source_endpoint,
        "time_window": time_window,
        "row_page_limits": deepcopy(limits),
        "refresh_cadence": deepcopy(_REFRESH_CADENCE),
        "null_behavior": deepcopy(_NULL),
        "partial_behavior": deepcopy(partial_behavior),
        "legacy_transition": _LEGACY_TRANSITION,
        "provenance": provenance,
        "safe_for_sorting": sortable,
        "safe_for_filtering": filterable,
        "label": label,
        "tooltip": tooltip,
    }


SCREENER_METRIC_CONTRACT = {
    "total_pnl": _metric_contract(
        formula=(
            "Official pnl from the first user-filtered ALL leaderboard row; after an "
            "explicit no-row result, preserve an existing non-null cached value of "
            "unknown/legacy provenance, or if missing use reconstructed "
            "sum(realized closings in fetched history) + current open-position cashPnl."
        ),
        source_endpoint=(_LEADERBOARD_USER_ENDPOINT, *_ACTIVITY_ENDPOINTS),
        time_window=(
            "ALL only for the official leaderboard value; fallback is fetch-bounded "
            "history plus a current positions snapshot and must not be described as lifetime."
        ),
        provenance="official_with_reconstructed_fallback",
        sortable=True, filterable=True, label="Total PnL",
        tooltip=(
            "Polymarket's official ALL-period PnL when returned. Only an explicit "
            "no-row result may fill a missing value with a fetch-bounded estimate; "
            "lookup failure preserves the cache."
        ),
        limits={
            "official_requests": 1, "official_request_row_limit": None,
            "official_rows_used": 1,
            **_FETCH_LIMITS,
        },
        partial_behavior={
            "status": "partial",
            "truncation_risks": ["TRADE", "REDEEM", "positions"],
            "reason": "The reconstructed fallback is page-bounded and is explicitly not official.",
        },
    ),
    "volume_usd": _metric_contract(
        formula=(
            "Official vol from the first user-filtered ALL leaderboard row; after an "
            "explicit no-row result, preserve an existing non-null cached value of "
            "unknown/legacy provenance, or if missing sum(usd_size) over fetched TRADE rows."
        ),
        source_endpoint=(_LEADERBOARD_USER_ENDPOINT, _TRADE_ENDPOINT),
        time_window=(
            "ALL only for the official leaderboard value; fallback is fetch-bounded "
            "activity and has no broader coverage claim."
        ),
        provenance="official_with_reconstructed_fallback",
        sortable=True, filterable=True, label="Total Volume",
        tooltip=(
            "Polymarket's official ALL-period volume when returned; after explicit "
            "no-row, a missing value may use a partial sum of fetched TRADE rows."
        ),
        limits={
            "official_requests": 1, "official_request_row_limit": None,
            "official_rows_used": 1, **_TRADE_LIMITS,
        },
        partial_behavior={
            "status": "partial",
            "truncation_risks": ["TRADE"],
            "reason": "The reconstructed fallback is page-bounded and is explicitly not official.",
        },
    ),
    "win_rate": _metric_contract(
        formula=(
            "count(reconstructed realized closing events marked win) / count(all "
            "reconstructed realized closing events fetched); null when there are no "
            f"closing events. {_REALIZED_CLOSING_RULES}"
        ),
        time_window="Fetched history used by the 90-day activity walk; page boundaries may include older events and coverage may be partial.",
        sortable=True, filterable=True, label="Observed Win Rate",
        tooltip="Share of reconstructed fetched closing events that won; coverage is fetch-bounded.",
    ),
    "open_positions": _metric_contract(
        formula="count(positions where size > 0 and redeemable is false)",
        source_endpoint=(_POSITIONS_ENDPOINT,), limits=_POSITIONS_LIMITS,
        partial_behavior=_POSITIONS_PARTIAL,
        time_window="Current point-in-time positions snapshot.",
        sortable=False, filterable=True, label="Open Positions",
        tooltip="Open non-redeemable positions in the fetched snapshot; 500 is a lower-bound truncation risk.",
    ),
    "consistency_score": _metric_contract(
        formula=(
            "If fewer than 7 observed PnL days: 0; otherwise 0.4*(positive days/observed days) + "
            "0.4*clamp((mean daily PnL/sample stdev)/3,0,1), where sample stdev == 0 "
            "means the component is 0, + 0.2*clamp(mean daily PnL/100,0,1), rounded to 4 decimals."
        ),
        time_window="Observed realized-PnL days in the fetched activity walk; absent calendar days are omitted.",
        sortable=True, filterable=True, label="Consistency",
        tooltip="Reconstructed steadiness score over observed PnL days; 0 can mean fewer than 7 observed days, not poor performance.",
    ),
    "pnl_quality": _metric_contract(
        formula="sum(reconstructed realized closings in fetched history) - sum(cashPnl of current open non-redeemable positions)",
        time_window="Fetch-bounded realized history minus a current point-in-time unrealized snapshot.",
        sortable=True, filterable=True, label="PnL Quality",
        tooltip="Reconstructed fetched realized PnL minus current open-position PnL; realized and snapshot terms use different horizons.",
    ),
    "daily_pnl_90d": _metric_contract(
        formula="JSON object mapping UTC closing day to sum(reconstructed realized PnL), retaining keys on or after (inclusive) the UTC date 90 days ago; days with no closing are omitted",
        time_window="Inclusive cutoff spans 91 possible UTC date labels (cutoff date through refresh date), though omitted no-closing dates mean fewer keys.",
        label="90d Daily PnL",
        tooltip="Reconstructed daily realized PnL points for the sparkline; missing dates are not zero-PnL claims.",
    ),
    "history_days": _metric_contract(
        formula="90.0 when the TRADE fetch exhausted or crossed the 90-day cutoff; otherwise round((refresh time - oldest fetched TRADE timestamp)/86400, 1)",
        source_endpoint=(_TRADE_ENDPOINT,), limits=_TRADE_LIMITS,
        partial_behavior=_TRADE_PARTIAL,
        time_window="TRADE coverage indicator capped at 90 days; it does not certify REDEEM or positions completeness.",
        filterable=True, label="History Coverage",
        tooltip="Approximate fetched TRADE coverage used to flag partial windows; it does not prove every source is complete.",
    ),
    "stats_refreshed_at": _metric_contract(
        formula="UTC ISO-8601 timestamp recorded when reconstructed stats are computed, before the cache upsert",
        source_endpoint="Internal application clock (backend.db.database.now_iso); no external endpoint.",
        time_window="Point in time when this cached reconstruction was computed.",
        label="Stats Updated",
        tooltip="When reconstructed screener statistics were last computed; discovery-only cache writes do not change it.",
        limits={"external_rows": 0, "external_pages": 0},
        partial_behavior=_UNAVAILABLE_PARTIAL,
    ),
}

for _window, _days in _PERIODS.items():
    _window_text = f"Rolling {_days} days using a seconds cutoff for trades and a UTC-date cutoff for closings."
    _defs = {
        "winrate": (
            "count(reconstructed closing events in window marked win) / count(reconstructed closing events in window); null when there are no closing events",
            f"{_days}d Win Rate", "Share of reconstructed closing events that won.", True, True),
        "pnl": (
            "sum(reconstructed realized PnL of closing events in window)",
            f"{_days}d Realized PnL", "Reconstructed realized PnL from fetched closing events.", True, True),
        "volume": (
            "sum(usd_size for fetched TRADE rows whose timestamp is in window)",
            f"{_days}d Volume", "Fetched traded notional; page truncation can undercount it.", True, True),
        "green_days": (
            "count(UTC dates where summed reconstructed realized PnL > 0)",
            f"{_days}d Green Days", "Observed UTC closing days with positive reconstructed realized PnL.", False, False),
        "red_days": (
            "count(UTC dates where summed reconstructed realized PnL < 0)",
            f"{_days}d Red Days", "Observed UTC closing days with negative reconstructed realized PnL.", False, False),
        "consistency_ratio": (
            "green_days / (green_days + red_days); zero-PnL and absent days are excluded; null when there are no green or red days",
            f"{_days}d Green-Day Ratio", "Green days as a share of observed non-zero PnL days.", False, True),
        "fills": (
            "count(fetched TRADE rows in window where side == BUY)",
            f"{_days}d Buy Fills", "Fetched BUY activity-row count, not order or position count.", False, False),
        "exits": (
            "count(fetched TRADE rows in window where side == SELL)",
            f"{_days}d Sell Exits", "Fetched SELL activity-row count; redemptions are not included.", False, False),
        "fill_exit_ratio": (
            "round(SELL TRADE row count / BUY TRADE row count * 100, 2); null when there are no BUY rows",
            f"{_days}d Exit/Fill Ratio", "Fetched SELL-to-BUY activity-row count ratio as a percentage, not an order, position, share, or capital close rate.", True, True),
    }
    for _stem, (_formula, _label, _tooltip, _sortable, _filterable) in _defs.items():
        _trade_only = _stem in {"volume", "fills", "exits", "fill_exit_ratio"}
        SCREENER_METRIC_CONTRACT[f"{_stem}_{_window}"] = _metric_contract(
            formula=_formula, time_window=_window_text, sortable=_sortable,
            filterable=_filterable, label=_label,
            tooltip=f"{_tooltip} Partial when source page budgets truncate coverage.",
            source_endpoint=(_TRADE_ENDPOINT,) if _trade_only else _ACTIVITY_ENDPOINTS,
            limits=_TRADE_LIMITS if _trade_only else _FETCH_LIMITS,
            partial_behavior=_TRADE_PARTIAL if _trade_only else _COMBINED_PARTIAL,
        )

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
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


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
    out: list[tuple[str | None, float, bool]] = []
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
    #    buy). If the buys predate the fetched window, keep the result undated:
    #    inventing "today" would leak an old outcome into every rolling window.
    held_assets: set = set()
    for p in positions:
        if getattr(p, "size", 0) <= 0.01:
            continue
        if p.redeemable:
            books[p.asset] = [0.0, 0.0]          # consumed -> no expired double-count
            ts = last_ts.get(p.asset)
            out.append((_day(ts) if ts is not None else None,
                        p.cash_pnl, p.cur_price >= 0.5))
        else:
            held_assets.add(p.asset)             # genuinely open -> leave basis

    # 4. expired-away: cost basis on a token that is neither still held nor
    #    resolved-in-wallet -> realize the loss (unless the list was truncated).
    if not positions_truncated:
        for a, (sh, c) in books.items():
            if sh > 0.01 and c > 0.005 and a not in held_assets:
                out.append((_day(last_ts.get(a, 0)), -c, False))

    out.sort(key=lambda e: e[0] or "")
    return out


def daily_realized_pnl(closings) -> dict[str, float]:
    daily: dict[str, float] = defaultdict(float)
    for day, realized, _ in closings:
        if day is not None:
            daily[day] += realized
    return dict(daily)


def win_rate_of(closings) -> float | None:
    wins = sum(1 for _, _, is_win in closings if is_win)
    return wins / len(closings) if closings else None


def _period_metrics(closings: list[tuple], trades: list, days: int) -> dict:
    """Windowed screener metrics for one period, given the FULL-history closings
    walk (so avg-cost basis stays correct even for positions opened before the
    window) filtered down to the window, and the full trade list (for volume /
    fill / exit counts, which don't need cost-basis continuity).

    `fill_exit_ratio` is exactly SELL activity-row count / BUY activity-row
    count * 100 within the window (null with no BUY rows). It is an activity
    frequency ratio, not a share, capital, or position close rate.
    """
    cutoff_ts = time.time() - days * 86400
    cutoff_day = dt.datetime.fromtimestamp(
        cutoff_ts, dt.timezone.utc).strftime("%Y-%m-%d")

    in_window = [c for c in closings if c[0] is not None and c[0] >= cutoff_day]
    total_closes = len(in_window)
    wins = sum(1 for _, _, is_win in in_window if is_win)
    winrate = wins / total_closes if total_closes else None
    pnl = sum(r for _, r, _ in in_window)

    daily: dict[str, float] = defaultdict(float)
    for day, r, _ in in_window:
        daily[day] += r
    green_days = sum(1 for v in daily.values() if v > 0)
    red_days = sum(1 for v in daily.values() if v < 0)
    consistency_ratio = (green_days / (green_days + red_days)
                         if (green_days + red_days) else None)

    recent = [t for t in trades if t.timestamp >= cutoff_ts]
    volume = sum(t.usd_size for t in recent)
    fills = sum(1 for t in recent if t.side == "BUY")
    exits = sum(1 for t in recent if t.side == "SELL")
    fill_exit_ratio = round(exits / fills * 100, 2) if fills else None

    return {
        "winrate": round(winrate, 4) if winrate is not None else None,
        "pnl": round(pnl, 2),
        "volume": round(volume, 2),
        "green_days": green_days,
        "red_days": red_days,
        "consistency_ratio": (round(consistency_ratio, 4)
                              if consistency_ratio is not None else None),
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
    """Discover top traders with one cheap API call.

    Every period contributes identity/profile fields. Only ALL entries may
    populate total_pnl/volume_usd: values from MONTH/WEEK/DAY must never be
    cached in columns whose public contract identifies them as ALL-period.
    """
    entries = await pm.get_leaderboard(period=period, order_by=order_by, limit=limit)
    for e in entries:
        fields = {
            "display_name": e.user_name,
            "profile_image": e.profile_image,
            "x_username": e.x_username,
            "verified": int(e.verified),
        }
        if period.upper() == "ALL":
            fields["total_pnl"] = e.pnl
            fields["volume_usd"] = e.vol
        await _upsert(db, e.proxy_wallet, fields)
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


@dataclass(frozen=True)
class TraderAnalysis:
    """One wallet-analysis result backed by a single bounded upstream walk.

    ``trades`` contains every fetched TRADE row used by the calculations.
    ``recent_trades`` reuses those rows and is sorted newest-first, capped at 25;
    no separate preview request is made. ``frozen=True`` prevents attribute
    rebinding only; the route contract intentionally keeps the contained rows
    as mutable lists and does not promise deep immutability.
    """

    stats: dict[str, Any]
    positions: list[Position]
    trades: list[Trade]
    recent_trades: list[Trade]


def _freeze_activity_row(value: Any) -> Any:
    """Return a hashable, recursive identity for a complete activity row.

    Dataclasses use their full ``asdict`` representation. Container/type tags
    keep otherwise-equal primitive values (for example ``True`` and ``1``)
    distinct. This deliberately does not use ``tx_hash``: one transaction may
    legitimately contain multiple different fills.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return _freeze_activity_row(asdict(value))
    if isinstance(value, dict):
        return (
            "dict",
            frozenset(
                (_freeze_activity_row(key), _freeze_activity_row(item))
                for key, item in value.items()
            ),
        )
    if isinstance(value, list):
        return ("list", tuple(_freeze_activity_row(item) for item in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(_freeze_activity_row(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return ("set", frozenset(_freeze_activity_row(item) for item in value))
    return (type(value), value)


async def _fetch_activity_window(fetch, days: int, max_pages: int) -> tuple[list, bool]:
    """Page through a most-recent-first activity fetcher until the window is
    covered or the page budget runs out. Returns (rows, covered): covered=False
    means the wallet is so active the oldest fetched row is still inside the
    window — stats then honestly reflect partial coverage (surfaced to the UI
    via history_days)."""
    cutoff = time.time() - days * 86400
    rows: list = []
    seen: set[Any] = set()
    for page in range(max_pages):
        batch = await fetch(limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)
        for row in batch:
            identity = _freeze_activity_row(row)
            if identity not in seen:
                seen.add(identity)
                rows.append(row)
        if len(batch) < _PAGE_SIZE:
            return rows, True                    # feed exhausted — full coverage
        oldest = batch[-1] if not isinstance(batch[-1], dict) else None
        oldest_ts = (oldest.timestamp if oldest is not None
                     else int(batch[-1].get("timestamp") or 0))
        if oldest_ts < cutoff:
            return rows, True
    return rows, False


async def refresh_trader_analysis(address: str, db, pm) -> TraderAnalysis:
    """Enrich one trader: consistency, win rate, trade count, open positions, and
    the windowed screener metrics (winrate/pnl/volume/consistency/fill-exit ratio
    at 7d/30d/90d) + pnl_quality. 3-8 API calls (paginated trades + redeems +
    positions) — run for the traders shown on the board, and periodically for
    the whole cache (see `refresh_all`).

    total_pnl/volume_usd prefer the leaderboard's per-user filter — the
    official ALL-period numbers Polymarket shows. An explicit no-row result may
    fill missing cache values with fetch-bounded reconstructions; a lookup
    exception preserves both totals. Existing non-null values are also
    preserved on no-row and have unknown/legacy provenance without a source
    column.
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
    # Undated resolved holdings remain valid fetched realized outcomes for the
    # aggregate fallback, but are intentionally absent from calendar windows.
    total_realized = sum(realized for _, realized, _ in closings)

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
    observed_win_rate = win_rate_of(closings)

    stats = {
        "consistency_score": score,
        "win_rate": (round(observed_win_rate, 4)
                     if observed_win_rate is not None else None),
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
    # The fallback state is intentionally tri-valued. An explicit successful
    # no-row response permits a reconstructed value for a missing cache field;
    # an exception proves nothing and must leave both cached totals untouched.
    official_lookup_succeeded = False
    try:
        official = await pm.get_leaderboard_user(address)
        official_lookup_succeeded = True
    except Exception:
        log.exception("official pnl lookup failed for %s (preserving cached totals)", address)
        official = None
    if official is not None:
        stats["total_pnl"] = official.pnl
        stats["volume_usd"] = official.vol
        if official.user_name:
            stats["display_name"] = official.user_name
        if official.x_username:
            stats["x_username"] = official.x_username
    elif official_lookup_succeeded:
        # No provenance column exists. A pre-existing non-null value may be an
        # official seed, a former fallback, or legacy data, so preserve it but
        # never describe it as official. Only fill genuinely missing values.
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
    stats_row = {**row, "tier": assign_tier(score)}
    recent_trades = sorted(
        trades, key=lambda trade: trade.timestamp, reverse=True,
    )[:25]
    return TraderAnalysis(
        stats=stats_row,
        positions=positions,
        trades=trades,
        recent_trades=recent_trades,
    )


async def refresh_trader_stats(address: str, db, pm) -> dict:
    """Backward-compatible stats-only facade for background refresh callers."""
    return (await refresh_trader_analysis(address, db, pm)).stats


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

# Whitelist of numeric columns the screener is allowed to filter on — windowed
# metrics, the fetched TRADE-history coverage indicator, and legacy all-time
# fields. Query params are `<column>_min` / `<column>_max`; anything not in this
# set is ignored (defense against injection and against filtering on arbitrary
# or internal columns).
_FILTERABLE_COLUMNS = frozenset({
    "winrate_7d", "winrate_30d", "winrate_90d",
    "pnl_7d", "pnl_30d", "pnl_90d",
    "volume_7d", "volume_30d", "volume_90d",
    "consistency_ratio_7d", "consistency_ratio_30d", "consistency_ratio_90d",
    "fill_exit_ratio_7d", "fill_exit_ratio_30d", "fill_exit_ratio_90d",
    "pnl_quality", "total_pnl", "win_rate", "volume_usd", "consistency_score",
    "open_positions", "history_days",
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
                        value = float(raw)
                        if math.isfinite(value):
                            out[key] = (col, op, value)
                    except (TypeError, ValueError):
                        pass
                break
    return out


async def get_leaderboard(
    db,
    sort_by: str = "pnl_30d",
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
    col = _SORT_COLS.get(sort_by, "pnl_30d")   # whitelist (no injection)
    clauses: list[str] = []
    params: list = []
    if filters:
        clauses += [f"{fcol} {op} ?" for fcol, op, _ in filters.values()]
        params += [val for _, _, val in filters.values()]
    if search and search.strip():
        # LOWER() both sides for case-insensitive search on BOTH backends —
        # SQLite LIKE ignores ASCII case but Postgres LIKE does not.
        term = f"%{search.strip().lower()}%"
        clauses.append("(LOWER(address) LIKE ? OR LOWER(display_name) LIKE ? "
                       "OR LOWER(x_username) LIKE ?)")
        params += [term, term, term]
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order_sql = (
        f"CASE WHEN {col} IS NULL THEN 1 ELSE 0 END ASC, "
        f"{col} DESC, address ASC"
    )
    rows = await db.fetchall(
        f"SELECT * FROM trader_cache {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        [*params, limit, offset])
    for r in rows:
        r["tier"] = assign_tier(r.get("consistency_score") or 0.0)
    return rows
