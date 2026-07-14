"""/api/traders/* — leaderboard, trader profile, follow/unfollow."""
from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from backend.config import (
    DEFAULT_ALLOCATION_PCT, DEFAULT_COPY_RATIO_PCT, DEFAULT_MAX_POSITION_USD,
    DEFAULT_MAX_PRICE, DEFAULT_MIN_PRICE,
)
from backend.core import trader_stats
from backend.core.trader_stats import assign_tier
from backend.api.deps import get_current_user, get_db, get_pm
from backend.db.database import now_iso

router = APIRouter()
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class FollowBody(BaseModel):
    # RATIO %: copy = leader position value × this %. max_position_usd = MAX/TRADE.
    copy_ratio_pct: float = Field(DEFAULT_COPY_RATIO_PCT, ge=0, le=20)
    max_position_usd: float = Field(DEFAULT_MAX_POSITION_USD, ge=1, le=500)
    allocation_pct: float = Field(DEFAULT_ALLOCATION_PCT, ge=0, le=100)


class FollowSettings(BaseModel):
    paused: bool | None = None
    copy_ratio_pct: float | None = Field(None, ge=0, le=20)
    max_position_usd: float | None = Field(None, ge=0, le=500)
    min_leader_usd: float | None = Field(None, ge=0, le=10000)
    ignore_below_usd: float | None = Field(None, ge=0, le=50)
    max_open_positions: int | None = Field(None, ge=0, le=50)
    max_total_exposure_usd: float | None = Field(None, ge=0, le=5000)
    min_price: float | None = Field(None, ge=0, le=1)
    max_price: float | None = Field(None, ge=0, le=1)
    max_slippage_pct: float | None = Field(None, ge=0, le=10)
    daily_loss_limit_usd: float | None = Field(None, ge=0, le=1000)
    allocation_pct: float | None = Field(None, ge=0, le=100)

    @model_validator(mode="after")
    def valid_price_bracket(self):
        if (self.min_price is not None and self.max_price is not None
                and self.min_price > self.max_price):
            raise ValueError("min_price must not exceed max_price")
        return self


_FOLLOW_KEYS = (
    "paused", "copy_ratio_pct", "max_position_usd", "min_leader_usd",
    "ignore_below_usd", "max_open_positions", "max_total_exposure_usd",
    "min_price", "max_price", "max_slippage_pct", "daily_loss_limit_usd",
    "allocation_pct",
)


@asynccontextmanager
async def _risk_write_guard(request: Request):
    lock = getattr(request.app.state, "copy_risk_lock", None)
    if lock is None:
        yield
    else:
        async with lock:
            yield


async def _wait_for_trader_submissions(db, user_id: str, address: str) -> None:
    for _ in range(50):
        pending = await db.fetchval(
            "SELECT COUNT(*) FROM copy_open_claims WHERE user_id=? AND trader_address=? "
            "AND state='submitting'", (user_id, address))
        if not pending:
            return
        await asyncio.sleep(0.1)
    raise HTTPException(503, "settings persisted; an in-flight order needs reconciliation")


# Literal routes must precede /{address} (single-segment) to avoid capture.
# Auth required: the screener is a logged-in surface, and leaving these public
# handed out unmetered DB reads (and, on /{address}, 3-8 upstream API calls +
# cache writes per hit) to anyone who found the tunnel URL.
@router.get("/leaderboard")
async def leaderboard(request: Request, sort: str = "consistency", limit: int = 50,
                      offset: int = 0, search: str | None = None,
                      user=Depends(get_current_user), db=Depends(get_db)):
    """Leaderboard / wallet screener. Any number of `<column>_min` / `<column>_max`
    query params (see `trader_stats._FILTERABLE_COLUMNS`) combine with AND — e.g.
    `?winrate_30d_min=0.6&pnl_30d_min=500&fill_exit_ratio_30d_min=50` filters
    simultaneously on 30d win rate, 30d pnl, and 30d exit-to-fill ratio.
    `search` substring-matches address / display name / X username."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    filters = trader_stats.parse_screener_filters(request.query_params)
    return await trader_stats.get_leaderboard(db, sort, limit, offset, filters,
                                              search=search)


@router.get("/following")
async def following(user=Depends(get_current_user), db=Depends(get_db)):
    """The user's active follows (leaderboard- or manually-added), with cached stats."""
    rows = await db.fetchall(
        "SELECT f.trader_address, f.allocation_pct, f.copy_ratio_pct, f.max_position_usd, "
        "f.paused, f.min_leader_usd, f.ignore_below_usd, f.max_open_positions, "
        "f.min_price, f.max_price, f.max_slippage_pct, f.max_total_exposure_usd, "
        "f.daily_loss_limit_usd, f.created_at, "
        "c.display_name, c.consistency_score, c.total_pnl, c.open_positions "
        "FROM followed_traders f LEFT JOIN trader_cache c ON c.address = f.trader_address "
        "WHERE f.user_id = ? AND f.is_active = 1 ORDER BY f.created_at DESC", (user["id"],))
    for r in rows:
        r["tier"] = assign_tier(r.get("consistency_score") or 0.0)
    return rows


@router.get("/{address}")
async def trader_profile(address: str, user=Depends(get_current_user),
                         db=Depends(get_db), pmc=Depends(get_pm)):
    """Live stats for ANY wallet (the screener's paste-an-address checker) —
    computes + caches windowed stats on the spot, so it also works for wallets
    the leaderboard crawler has never seen."""
    address = address.lower()
    if not _ADDR_RE.match(address):
        raise HTTPException(400, "invalid wallet address (expected 0x + 40 hex)")
    stats = await trader_stats.refresh_trader_stats(address, db, pmc)
    positions = await pmc.get_positions(address, size_threshold=0)
    trades = await pmc.get_trade_history(address, limit=25)
    return {**stats,
            "positions": [asdict(p) for p in positions],
            "recent_trades": [asdict(t) for t in trades]}


@router.post("/{address}/follow")
async def follow(address: str, body: FollowBody, request: Request,
                 user=Depends(get_current_user), db=Depends(get_db)):
    address = address.lower()
    if not _ADDR_RE.match(address):
        raise HTTPException(400, "invalid wallet address (expected 0x + 40 hex)")
    async with _risk_write_guard(request):
        async with db.transaction(write=True) as tx:
            user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if db.is_pg else "")
            await tx.fetchone(user_sql, (user["id"],))
            await tx.execute("UPDATE users SET risk_revision=risk_revision+1 WHERE id=?", (user["id"],))
            existing = await tx.fetchone(
                "SELECT id,is_active,paused FROM followed_traders WHERE user_id=? AND trader_address=?",
                (user["id"], address))
            if existing:
                paused = 0 if not existing["is_active"] else existing["paused"]
                await tx.execute(
                    "UPDATE followed_traders SET copy_ratio_pct=?,max_position_usd=?,"
                    "allocation_pct=?,paused=?,is_active=1 WHERE id=?",
                    (body.copy_ratio_pct, body.max_position_usd, body.allocation_pct,
                     paused, existing["id"]))
            else:
                await tx.execute(
                    "INSERT INTO followed_traders(id,user_id,trader_address,copy_ratio_pct,"
                    "max_position_usd,allocation_pct,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user["id"], address, body.copy_ratio_pct,
                     body.max_position_usd, body.allocation_pct, now_iso()))
    await _wait_for_trader_submissions(db, user["id"], address)
    return {"ok": True, "following": address,
            "copy_ratio_pct": body.copy_ratio_pct, "max_position_usd": body.max_position_usd}


@router.post("/{address}/settings")
async def update_follow_settings(address: str, body: FollowSettings, request: Request,
                                 user=Depends(get_current_user), db=Depends(get_db)):
    """Edit one copied wallet's risk settings (only provided fields)."""
    address = address.lower()
    if not _ADDR_RE.match(address):
        raise HTTPException(400, "invalid wallet address (expected 0x + 40 hex)")
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items()
               if k in _FOLLOW_KEYS}
    if "paused" in updates:
        updates["paused"] = int(bool(updates["paused"]))
    # Canonical API semantics: zero means no limit for these three controls.
    for key in ("max_open_positions", "max_total_exposure_usd", "daily_loss_limit_usd"):
        if updates.get(key) == 0:
            updates[key] = None

    async with _risk_write_guard(request):
        async with db.transaction(write=True) as tx:
            user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if db.is_pg else "")
            await tx.fetchone(user_sql, (user["id"],))
            current = await tx.fetchone(
                "SELECT * FROM followed_traders WHERE user_id=? AND trader_address=?",
                (user["id"], address))
            if not current:
                raise HTTPException(404, "followed wallet not found")
            effective_min = updates.get("min_price", current.get("min_price"))
            effective_max = updates.get("max_price", current.get("max_price"))
            effective_min = DEFAULT_MIN_PRICE if effective_min is None else float(effective_min)
            effective_max = DEFAULT_MAX_PRICE if effective_max is None else float(effective_max)
            if effective_min > effective_max:
                raise HTTPException(422, "min_price must not exceed max_price")
            if updates:
                # Increment first in the same transaction: every reserved BUY is
                # fenced before any stricter setting becomes visible.
                await tx.execute("UPDATE users SET risk_revision=risk_revision+1 WHERE id=?", (user["id"],))
                cols = ", ".join(f"{k} = ?" for k in updates)
                await tx.execute(
                    f"UPDATE followed_traders SET {cols} WHERE user_id=? AND trader_address=?",
                    [*updates.values(), user["id"], address])
    if updates:
        await _wait_for_trader_submissions(db, user["id"], address)
    return {"ok": True, "updated": list(updates)}


@router.delete("/{address}/follow")
async def unfollow(address: str, request: Request,
                   user=Depends(get_current_user), db=Depends(get_db)):
    async with _risk_write_guard(request):
        async with db.transaction(write=True) as tx:
            user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if db.is_pg else "")
            await tx.fetchone(user_sql, (user["id"],))
            await tx.execute("UPDATE users SET risk_revision=risk_revision+1 WHERE id=?", (user["id"],))
            await tx.execute(
                "UPDATE followed_traders SET is_active=0 WHERE user_id=? AND trader_address=?",
                (user["id"], address.lower()))
    await _wait_for_trader_submissions(db, user["id"], address.lower())
    return {"ok": True}
