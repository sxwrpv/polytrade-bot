"""/api/traders/* — leaderboard, trader profile, follow/unfollow."""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.config import DEFAULT_ALLOCATION_PCT, DEFAULT_MAX_POSITION_USD
from backend.core import trader_stats
from backend.core.trader_stats import assign_tier
from backend.api.deps import get_current_user, get_db, get_pm
from backend.db.database import now_iso

router = APIRouter()
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class FollowBody(BaseModel):
    allocation_pct: float = DEFAULT_ALLOCATION_PCT
    max_position_usd: float = DEFAULT_MAX_POSITION_USD


class FollowSettings(BaseModel):
    allocation_pct: float | None = None
    max_position_usd: float | None = None
    paused: bool | None = None
    max_slippage_pct: float | None = None
    max_total_exposure_usd: float | None = None
    daily_loss_limit_usd: float | None = None


_FOLLOW_KEYS = (
    "allocation_pct", "max_position_usd", "paused",
    "max_slippage_pct", "max_total_exposure_usd", "daily_loss_limit_usd",
)


# Literal routes must precede /{address} (single-segment) to avoid capture.
@router.get("/leaderboard")
async def leaderboard(request: Request, sort: str = "consistency", limit: int = 50,
                      offset: int = 0, search: str | None = None, db=Depends(get_db)):
    """Leaderboard / wallet screener. Any number of `<column>_min` / `<column>_max`
    query params (see `trader_stats._FILTERABLE_COLUMNS`) combine with AND — e.g.
    `?winrate_30d_min=0.6&pnl_30d_min=500&fill_exit_ratio_30d_min=50` filters
    simultaneously on 30d win rate, 30d pnl, and 30d exit-to-fill ratio.
    `search` substring-matches address / display name / X username."""
    filters = trader_stats.parse_screener_filters(request.query_params)
    return await trader_stats.get_leaderboard(db, sort, limit, offset, filters,
                                              search=search)


@router.get("/following")
async def following(user=Depends(get_current_user), db=Depends(get_db)):
    """The user's active follows (leaderboard- or manually-added), with cached stats."""
    rows = await db.fetchall(
        "SELECT f.trader_address, f.allocation_pct, f.max_position_usd, f.paused, "
        "f.max_slippage_pct, f.max_total_exposure_usd, f.daily_loss_limit_usd, f.created_at, "
        "c.display_name, c.consistency_score, c.total_pnl, c.open_positions "
        "FROM followed_traders f LEFT JOIN trader_cache c ON c.address = f.trader_address "
        "WHERE f.user_id = ? AND f.is_active = 1 ORDER BY f.created_at DESC", (user["id"],))
    for r in rows:
        r["tier"] = assign_tier(r.get("consistency_score") or 0.0)
    return rows


@router.get("/{address}")
async def trader_profile(address: str, db=Depends(get_db), pmc=Depends(get_pm)):
    address = address.lower()
    stats = await trader_stats.refresh_trader_stats(address, db, pmc)
    positions = await pmc.get_positions(address, size_threshold=0)
    trades = await pmc.get_trade_history(address, limit=25)
    return {**stats,
            "positions": [asdict(p) for p in positions],
            "recent_trades": [asdict(t) for t in trades]}


@router.post("/{address}/follow")
async def follow(address: str, body: FollowBody,
                 user=Depends(get_current_user), db=Depends(get_db)):
    address = address.lower()                       # match data-api/leaderboard casing
    if not _ADDR_RE.match(address):
        raise HTTPException(400, "invalid wallet address (expected 0x + 40 hex)")
    existing = await db.fetchone(
        "SELECT id FROM followed_traders WHERE user_id = ? AND trader_address = ?",
        (user["id"], address))
    if existing:
        await db.execute(
            "UPDATE followed_traders SET allocation_pct = ?, max_position_usd = ?, "
            "is_active = 1 WHERE id = ?",
            (body.allocation_pct, body.max_position_usd, existing["id"]))
    else:
        await db.execute(
            "INSERT INTO followed_traders(id, user_id, trader_address, allocation_pct, "
            "max_position_usd, created_at) VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, user["id"], address, body.allocation_pct,
             body.max_position_usd, now_iso()))
    return {"ok": True, "following": address,
            "allocation_pct": body.allocation_pct, "max_position_usd": body.max_position_usd}


@router.post("/{address}/settings")
async def update_follow_settings(address: str, body: FollowSettings,
                                 user=Depends(get_current_user), db=Depends(get_db)):
    """Edit one copied wallet's risk settings (only provided fields)."""
    address = address.lower()
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in _FOLLOW_KEYS}
    if "paused" in updates:
        updates["paused"] = int(bool(updates["paused"]))
    if updates:
        cols = ", ".join(f"{k} = ?" for k in updates)
        await db.execute(
            f"UPDATE followed_traders SET {cols} WHERE user_id = ? AND trader_address = ?",
            [*updates.values(), user["id"], address])
    return {"ok": True, "updated": list(updates)}


@router.delete("/{address}/follow")
async def unfollow(address: str, user=Depends(get_current_user), db=Depends(get_db)):
    await db.execute(
        "UPDATE followed_traders SET is_active = 0 WHERE user_id = ? AND trader_address = ?",
        (user["id"], address.lower()))
    return {"ok": True}
