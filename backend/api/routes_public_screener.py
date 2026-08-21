"""/api/public/screener/* — anonymous, read-only wallet research.

Why this exists as a separate namespace instead of opening /api/traders/*:
that router is session-gated on purpose. Leaving it public handed out
unmetered database reads, and on /{address} three to eight upstream Polymarket
calls plus cache writes, to anyone who found the URL. Those semantics are
unchanged.

This namespace makes a much narrower promise, and the promise is the security
boundary:

  * it reads ONLY precomputed columns from trader_cache — the background stats
    loop is the sole writer, so a public request can never trigger upstream
    fan-out or a cache write;
  * it never touches the Polymarket client (there is deliberately no get_pm
    dependency in this module);
  * it projects an explicit field allowlist, so a column added to trader_cache
    later cannot start leaking through this route by accident;
  * it is rate limited per client address.

trader_cache holds public wallet data only. No PolyTrade user, session,
balance, follow relationship or Telegram identity is readable here, and none is
joined in.
"""
from __future__ import annotations

import re
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api.deps import get_db

router = APIRouter()

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
PERIODS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}
SORTS = {"pnl", "winrate", "volume"}
FILTERS = {"pnl", "winrate", "volume", "consistency_ratio", "fill_exit_ratio", "history_days"}

# Per-client budget. Generous enough for a person researching wallets, tight
# enough that the endpoint cannot be used as a free bulk export of the cache.
PUBLIC_RATE_LIMIT = 60
PUBLIC_RATE_WINDOW_SECONDS = 60.0
_MAX_TRACKED_CLIENTS = 4096

# client key -> recent request timestamps. The key is a client IP, never a
# wallet address or anything else derived from what was searched for.
_requests: dict[str, deque[float]] = {}


def reset_rate_limits() -> None:
    """Test helper: drop every bucket."""
    _requests.clear()


def rate_limit_keys() -> list[str]:
    """Test helper: what the limiter is keyed on."""
    return list(_requests)


def _enforce_rate_limit(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _requests.get(client)
    if bucket is None:
        # Bound memory: a flood of distinct source addresses must not grow this
        # map without limit. Drop the least recently seen bucket.
        if len(_requests) >= _MAX_TRACKED_CLIENTS:
            oldest = min(_requests, key=lambda key: _requests[key][-1] if _requests[key] else 0.0)
            _requests.pop(oldest, None)
        bucket = _requests[client] = deque()
    while bucket and now - bucket[0] > PUBLIC_RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= PUBLIC_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="too many requests — the public screener is rate limited",
            headers={"Retry-After": str(int(PUBLIC_RATE_WINDOW_SECONDS))},
        )
    bucket.append(now)


def _number(value):
    """Keep an absent metric absent. A missing value is not a zero, and
    rendering it as one would misdescribe the wallet."""
    return None if value is None else float(value)


def _project(row: dict, period: str) -> dict:
    """Explicit allowlist projection of one trader_cache row.

    Only period-aware, truthful fields. No composite 'copyability' score is
    published: the inputs are partial by construction, and a single number
    would hide that.
    """
    days = PERIODS[period]
    history_days = _number(row.get("history_days"))
    refreshed = row.get("stats_refreshed_at")
    return {
        "address": row["address"],
        "display_name": row.get("display_name"),
        "x_username": row.get("x_username"),
        "verified": bool(row.get("verified")),
        "period": period,
        "period_days": days,
        "pnl": _number(row.get(f"pnl_{period}")),
        "win_rate": _number(row.get(f"winrate_{period}")),
        "volume": _number(row.get(f"volume_{period}")),
        # open_positions defaults to 0 in the schema for rows that predate
        # enrichment, so it only means something once stats were computed.
        "active_positions": (
            None if refreshed is None else
            (None if row.get("open_positions") is None else int(row["open_positions"]))
        ),
        # Published because they are filterable below: a threshold you cannot
        # see the value behind asserts something the reader cannot check.
        "consistency_ratio": _number(row.get(f"consistency_ratio_{period}")),
        # Already a percentage (SELL rows / BUY rows * 100), not a 0..1 share.
        "fill_exit_ratio": _number(row.get(f"fill_exit_ratio_{period}")),
        "history_days": history_days,
        # "Partial" describes the fetched TRADE history only — it says nothing
        # about coverage of any other source.
        "history_partial": history_days is None or history_days < days,
        "stats_refreshed_at": refreshed,
    }


_PROVENANCE = {
    "source": "Public Polymarket leaderboard and activity data, recomputed by "
              "PolyTrade into 7-day, 30-day and 90-day windows.",
    "limitations": [
        "Metrics are reconstructed from fetched trade history; where that "
        "history does not reach back across the whole period the wallet is "
        "marked partial.",
        "A missing metric is shown as unavailable, never as zero.",
        "Figures are a cache, refreshed periodically — see stats_refreshed_at "
        "for when each wallet was last recomputed.",
        "The exit/fill ratio counts fetched SELL activity rows against BUY "
        "activity rows. It is an activity-frequency ratio, not an order, "
        "position, share, or capital close rate.",
        "The positive close-day ratio counts UTC days with positive realized "
        "PnL against days with negative realized PnL; days that netted exactly "
        "zero, and days with no closings at all, are excluded.",
        "Past wallet activity does not predict future results.",
    ],
}

# Sortable expressions, whitelisted per (sort, period) — never interpolated
# from user input.
_SORT_COLUMNS = {
    (sort, period): f"{'winrate' if sort == 'winrate' else sort}_{period}"
    for sort in SORTS for period in PERIODS
}


@router.get("/wallets")
async def public_wallets(
    request: Request,
    period: str = Query("30d"),
    sort: str = Query("pnl"),
    search: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    pnl_min: float | None = Query(None),
    winrate_min: float | None = Query(None),
    volume_min: float | None = Query(None),
    consistency_ratio_min: float | None = Query(None, ge=0, le=1),
    fill_exit_ratio_min: float | None = Query(None, ge=0),
    fill_exit_ratio_max: float | None = Query(None, ge=0),
    complete_history_only: bool = Query(False),
    db=Depends(get_db),
):
    """Anonymous wallet discovery over the precomputed screener cache.

    Read-only: this never recomputes a wallet and never writes. Wallets the
    background stats loop has not reached yet simply carry null metrics.
    """
    _enforce_rate_limit(request)
    if period not in PERIODS:
        raise HTTPException(422, "period must be one of 7d, 30d, 90d")
    if sort not in SORTS:
        raise HTTPException(422, "sort must be one of pnl, winrate, volume")

    column = _SORT_COLUMNS[(sort, period)]
    clauses: list[str] = []
    params: list = []
    if pnl_min is not None:
        clauses.append(f"pnl_{period} >= ?")
        params.append(pnl_min)
    if winrate_min is not None:
        clauses.append(f"winrate_{period} >= ?")
        params.append(winrate_min)
    if volume_min is not None:
        clauses.append(f"volume_{period} >= ?")
        params.append(volume_min)
    if consistency_ratio_min is not None:
        clauses.append(f"consistency_ratio_{period} >= ?")
        params.append(consistency_ratio_min)
    if fill_exit_ratio_min is not None:
        clauses.append(f"fill_exit_ratio_{period} >= ?")
        params.append(fill_exit_ratio_min)
    if fill_exit_ratio_max is not None:
        clauses.append(f"fill_exit_ratio_{period} <= ?")
        params.append(fill_exit_ratio_max)
    if complete_history_only:
        clauses.append("history_days >= ?")
        params.append(float(PERIODS[period]))
    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        clauses.append("(LOWER(address) LIKE ? OR LOWER(display_name) LIKE ? "
                       "OR LOWER(x_username) LIKE ?)")
        params += [term, term, term]

    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = await db.fetchall(
        f"SELECT * FROM trader_cache {where_sql} "
        f"ORDER BY CASE WHEN {column} IS NULL THEN 1 ELSE 0 END ASC, "
        f"{column} DESC, address ASC LIMIT ? OFFSET ?",
        [*params, limit, offset])

    return {
        "period": period,
        "period_days": PERIODS[period],
        "sort": sort,
        "count": len(rows),
        "wallets": [_project(row, period) for row in rows],
        "provenance": _PROVENANCE,
    }


@router.get("/wallets/{address}")
async def public_wallet(
    request: Request,
    address: str,
    period: str = Query("30d"),
    db=Depends(get_db),
):
    """One wallet, from the cache only.

    A wallet the background loop has never seen returns 404 rather than being
    fetched on demand: an anonymous caller must not be able to spend upstream
    API budget, which is exactly what the authenticated /api/traders/{address}
    route is for.
    """
    _enforce_rate_limit(request)
    if period not in PERIODS:
        raise HTTPException(422, "period must be one of 7d, 30d, 90d")
    address = address.lower()
    if not _ADDR_RE.match(address):
        raise HTTPException(400, "invalid wallet address (expected 0x + 40 hex)")

    row = await db.fetchone("SELECT * FROM trader_cache WHERE address = ?", (address,))
    if not row:
        raise HTTPException(
            404,
            "this wallet is not in the public screener cache",
        )
    return _project(row, period)


@router.get("/provenance")
async def provenance(request: Request):
    """Where the numbers come from and what they do not claim."""
    _enforce_rate_limit(request)
    return _PROVENANCE
