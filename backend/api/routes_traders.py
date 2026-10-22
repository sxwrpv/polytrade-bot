"""/api/traders/* — authenticated profiles and copied-wallet controls."""
from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from backend.config import (
    DEFAULT_COPY_RATIO_PCT, DEFAULT_MAX_POSITION_USD,
    DEFAULT_MAX_PRICE, DEFAULT_MIN_PRICE, MAX_FOLLOWS_PER_USER,
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


class FollowSettings(BaseModel):
    paused: bool | None = None
    copy_ratio_pct: float | None = Field(None, ge=0, le=20)
    max_position_usd: float | None = Field(None, ge=1, le=500)   # same bounds as FollowBody and the UI
    min_leader_usd: float | None = Field(None, ge=0, le=10000)
    ignore_below_usd: float | None = Field(None, ge=0, le=50)
    max_open_positions: int | None = Field(None, ge=0, le=50)
    max_total_exposure_usd: float | None = Field(None, ge=0, le=5000)
    min_price: float | None = Field(None, ge=0, le=1)
    max_price: float | None = Field(None, ge=0, le=1)
    max_slippage_pct: float | None = Field(None, ge=0, le=10)
    daily_loss_limit_usd: float | None = Field(None, ge=0, le=1000)
    # SIZING: 'position_share' = their position × copy_ratio_pct (null means
    # this too); 'capital_share' = the share of their free cash a bet took, of
    # your free cash, × capital_multiplier. Same bounds as the UI slider.
    sizing_mode: Literal["position_share", "capital_share"] | None = None
    capital_multiplier: float | None = Field(None, ge=0.1, le=100)

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
    "sizing_mode", "capital_multiplier",
)


@asynccontextmanager
async def _risk_write_guard(request: Request, user_id: str):
    """Serialize with THIS user's in-flight BUYs (per-user KeyedLocks), so a
    settings change waits for nobody else's orders. A plain lock, if one was
    injected, still applies globally."""
    lock = getattr(request.app.state, "copy_risk_lock", None)
    if lock is None:
        yield
    elif hasattr(lock, "hold"):
        async with lock.hold(user_id):
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
@router.get("/following")
async def following(user=Depends(get_current_user), db=Depends(get_db)):
    """The user's active follows (leaderboard- or manually-added), with cached stats."""
    rows = await db.fetchall(
        "SELECT f.trader_address, f.copy_ratio_pct, f.max_position_usd, "
        "f.paused, f.min_leader_usd, f.ignore_below_usd, f.max_open_positions, "
        "f.min_price, f.max_price, f.max_slippage_pct, f.max_total_exposure_usd, "
        "f.daily_loss_limit_usd, f.sizing_mode, f.capital_multiplier, f.created_at, "
        "c.display_name, c.consistency_score, c.total_pnl, c.open_positions "
        "FROM followed_traders f LEFT JOIN trader_cache c ON c.address = f.trader_address "
        "WHERE f.user_id = ? AND f.is_active = 1 ORDER BY f.created_at DESC", (user["id"],))
    for r in rows:
        r["tier"] = assign_tier(r.get("consistency_score") or 0.0)
    return rows


@router.get("/{address}")
async def trader_profile(address: str, user=Depends(get_current_user),
                         db=Depends(get_db), pmc=Depends(get_pm)):
    """Authenticated on-demand analysis for any valid wallet address.

    This endpoint computes and caches windowed stats. The public standalone
    screener remains cache-only and never invokes this upstream-spending path.
    """
    address = address.lower()
    if not _ADDR_RE.match(address):
        raise HTTPException(400, "invalid wallet address (expected 0x + 40 hex)")
    analysis = await trader_stats.refresh_trader_analysis(address, db, pmc)
    return {**analysis.stats,
            "positions": [asdict(p) for p in analysis.positions],
            "recent_trades": [asdict(t) for t in analysis.recent_trades]}


@router.post("/{address}/follow")
async def follow(address: str, body: FollowBody, request: Request,
                 user=Depends(get_current_user), db=Depends(get_db)):
    address = address.lower()
    if not _ADDR_RE.match(address):
        raise HTTPException(400, "invalid wallet address (expected 0x + 40 hex)")
    # Copying your own deposit wallet feeds every copy back in as a new
    # leader trade, ratcheting the position up to MAX/TRADE on its own.
    own = {str(user["id"]).lower(), str(user.get("signer_address") or "").lower()}
    if address in own:
        raise HTTPException(400, "you cannot copy your own wallet")
    async with _risk_write_guard(request, user["id"]):
        async with db.transaction(write=True) as tx:
            user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if db.is_pg else "")
            await tx.fetchone(user_sql, (user["id"],))
            existing = await tx.fetchone(
                "SELECT id,is_active,paused FROM followed_traders WHERE user_id=? AND trader_address=?",
                (user["id"], address))
            if not (existing and existing["is_active"]):
                # Every active follow costs the engine a positions read per
                # reconcile pass and a trade poll every detection tick, so one
                # account following hundreds of wallets slows copying for all.
                active = await tx.fetchval(
                    "SELECT COUNT(*) FROM followed_traders WHERE user_id=? AND is_active=1",
                    (user["id"],))
                if int(active or 0) >= MAX_FOLLOWS_PER_USER:
                    raise HTTPException(
                        409, f"you can copy at most {MAX_FOLLOWS_PER_USER} wallets — "
                             "stop copying one first")
            await tx.execute("UPDATE users SET risk_revision=risk_revision+1 WHERE id=?", (user["id"],))
            if existing:
                paused = 0 if not existing["is_active"] else existing["paused"]
                await tx.execute(
                    "UPDATE followed_traders SET copy_ratio_pct=?,max_position_usd=?,"
                    "paused=?,is_active=1 WHERE id=?",
                    (body.copy_ratio_pct, body.max_position_usd,
                     paused, existing["id"]))
            else:
                await tx.execute(
                    "INSERT INTO followed_traders(id,user_id,trader_address,copy_ratio_pct,"
                    "max_position_usd,created_at) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user["id"], address, body.copy_ratio_pct,
                     body.max_position_usd, now_iso()))
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

    async with _risk_write_guard(request, user["id"]):
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
    async with _risk_write_guard(request, user["id"]):
        async with db.transaction(write=True) as tx:
            user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if db.is_pg else "")
            await tx.fetchone(user_sql, (user["id"],))
            await tx.execute("UPDATE users SET risk_revision=risk_revision+1 WHERE id=?", (user["id"],))
            await tx.execute(
                "UPDATE followed_traders SET is_active=0 WHERE user_id=? AND trader_address=?",
                (user["id"], address.lower()))
    await _wait_for_trader_submissions(db, user["id"], address.lower())
    return {"ok": True}
