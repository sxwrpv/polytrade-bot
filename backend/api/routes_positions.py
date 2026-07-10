"""/api/positions/* — open (live unrealized), closed history, manual close."""
from __future__ import annotations

import uuid

import aiosqlite

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.core import execution
from backend.api.deps import get_current_user, get_db, get_pm, get_user_client
from backend.config import MAX_COPY_SLIPPAGE_PCT
from backend.db.database import now_iso

router = APIRouter()

# copy_positions.trader_address for wallet holdings the user closed manually —
# they were never copied from anyone, but the row keeps them in closed history
# and in the per-wallet PnL breakdown (shown as MANUAL in the UI).
MANUAL_TRADER = "manual"


@router.get("/open")
async def open_positions(user=Depends(get_current_user), db=Depends(get_db), pmc=Depends(get_pm)):
    rows = await db.fetchall(
        "SELECT * FROM copy_positions WHERE user_id = ? "
        "AND status IN ('open','closing','reconciliation_required') "
        "ORDER BY opened_at DESC", (user["id"],))
    claims = await db.fetchall(
        "SELECT * FROM copy_open_claims WHERE user_id=? "
        "AND state IN ('reserved','submitting','uncertain') ORDER BY updated_at DESC",
        (user["id"],))
    # Enrich with the wallet snapshot, then overlay every active BUY fencing
    # claim.  A claim is visible even before shares appear on-chain/indexer.
    live = {p.asset: p for p in await pmc.get_positions(user["id"], size_threshold=0)}
    by_token = {r["token_id"]: r for r in rows}
    for r in rows:
        p = live.get(r["token_id"])
        r["current_price"] = p.cur_price if p else None
        r["unrealized_pnl"] = round(p.cash_pnl, 2) if p else None
        r["reconciliation_required"] = r["status"] in (
            "closing", "reconciliation_required")
    for claim in claims:
        token = claim["token_id"]
        p = live.get(token)
        r = by_token.get(token)
        if r is None:
            r = {
                "id": None, "external": False, "origin": "pending_buy",
                "token_id": token,
                "market_title": p.title if p else None,
                "market_slug": (p.event_slug or p.slug) if p else None,
                "outcome": (p.outcome or "").upper() if p else "",
                "shares": p.size if p else 0.0,
                "entry_price": p.avg_price if p else None,
                "notional_usd": round(p.initial_value, 2) if p else claim["reserved_usd"],
                "current_price": p.cur_price if p else None,
                "unrealized_pnl": round(p.cash_pnl, 2) if p else None,
                "trader_address": claim["trader_address"],
                "redeemable": p.redeemable if p else False,
            }
            rows.append(r)
            by_token[token] = r
        # A wallet holding behind an unresolved BUY claim is never presented as
        # an ordinary external position: neither close nor retry is safe. Prefer
        # the live wallet share count over a potentially stale pre-resize row.
        if p is not None:
            r["shares"] = p.size
            r["live_shares"] = p.size
        r.update({
            "status": "reconciliation_required",
            "reconciliation_required": True,
            "external": False,
            "claim_id": claim["claim_id"],
            "claim_state": claim["state"],
            "claim_action": claim["action"],
            "claim_error": claim.get("last_error"),
            "reserved_usd": claim["reserved_usd"],
        })
    # Include every other live wallet holding. Missing history does not prove it
    # was manual; preserve bot attribution when history does exist.
    managed = set(by_token)
    for p in live.values():
        if p.asset in managed or p.size <= 0.01:
            continue
        history = await db.fetchone(
            "SELECT trader_address, opened_at FROM copy_positions "
            "WHERE user_id = ? AND token_id = ? AND trader_address != ? "
            "ORDER BY opened_at DESC LIMIT 1",
            (user["id"], p.asset, MANUAL_TRADER))
        rows.append({
            "id": None, "external": True, "token_id": p.asset,
            "market_title": p.title, "market_slug": p.event_slug or p.slug,
            "outcome": (p.outcome or "").upper(), "shares": p.size,
            "entry_price": p.avg_price, "notional_usd": round(p.initial_value, 2),
            "current_price": p.cur_price, "unrealized_pnl": round(p.cash_pnl, 2),
            "status": "open", "origin": "bot_history" if history else "unknown",
            "reconciliation_required": False,
            "trader_address": history["trader_address"] if history else None,
            "redeemable": p.redeemable,
        })
    return rows


@router.get("/closed")
async def closed_positions(user=Depends(get_current_user), db=Depends(get_db)):
    return await db.fetchall(
        "SELECT * FROM copy_positions WHERE user_id = ? AND status IN ('closed','resolved') "
        "ORDER BY closed_at DESC", (user["id"],))


class CloseExternalBody(BaseModel):
    token_id: str
    acceptable_slippage_pct: float = Field(
        default=MAX_COPY_SLIPPAGE_PCT, ge=0, le=10, allow_inf_nan=False)


class CloseBody(BaseModel):
    acceptable_slippage_pct: float = Field(
        default=MAX_COPY_SLIPPAGE_PCT, ge=0, le=10, allow_inf_nan=False)


@router.post("/close-external")
async def close_external_position(body: CloseExternalBody, request: Request,
                                  user=Depends(get_current_user), db=Depends(get_db),
                                  pmc=Depends(get_pm)):
    """Claim an untracked holding durably before submitting exactly one SELL."""
    live = await pmc.get_positions(user["id"], size_threshold=0)
    p = next((x for x in live if x.asset == body.token_id), None)
    if p is None or p.size <= 0.01:
        raise HTTPException(404, "wallet does not hold this token")
    if p.redeemable:
        raise HTTPException(400, "market already resolved — winnings redeem automatically, "
                                 "there is nothing to sell")
    position_id = uuid.uuid4().hex
    ts = now_iso()
    # Serialize on the same user row as BUY reservation. The claim/position
    # checks and closing-row insert are one transaction, so a BUY reservation
    # and external SELL claim cannot pass each other across API/engine workers.
    try:
        async with db.transaction(write=True) as tx:
            user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if db.is_pg else "")
            await tx.fetchone(user_sql, (user["id"],))
            buy_claim = await tx.fetchone(
                "SELECT token_id FROM copy_open_claims WHERE user_id=? AND token_id=? "
                "AND state IN ('reserved','submitting','uncertain')",
                (user["id"], body.token_id))
            managed = await tx.fetchone(
                "SELECT id FROM copy_positions WHERE user_id=? AND token_id=? "
                "AND status IN ('open','closing','reconciliation_required')",
                (user["id"], body.token_id))
            if buy_claim or managed:
                raise HTTPException(
                    409, "position is already managed, reserved, or awaiting reconciliation")
            await tx.execute(
                "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
                "market_slug,market_title,outcome,shares,entry_price,notional_usd,status,opened_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'closing',?)",
                (position_id, user["id"], MANUAL_TRADER, p.condition_id, body.token_id,
                 p.event_slug or p.slug, p.title, (p.outcome or "").upper(), p.size,
                 p.avg_price, round(p.initial_value, 2), ts))
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "position is already being closed")
    try:
        client = await get_user_client(request, user)
        result = await execution.place_market_order(
            client, pmc, body.token_id, "SELL", p.size, reference_price=p.cur_price,
            max_slippage_pct=body.acceptable_slippage_pct)
    except Exception:
        # A raised exception happened outside execution's submission boundary;
        # execution converts transport ambiguity into submission_uncertain.
        await db.execute("DELETE FROM copy_positions WHERE id=? AND status='closing'",
                         (position_id,))
        raise
    if result.ok:
        pnl = (result.avg_price - p.avg_price) * result.filled_shares
        async with db.transaction(write=True) as tx:
            changed = await tx.execute(
                "UPDATE copy_positions SET status='closed',shares=?,exit_price=?,realized_pnl=?,"
                "closed_at=? WHERE id=? AND user_id=? AND status='closing'",
                (result.filled_shares, result.avg_price, pnl, now_iso(), position_id,
                 user["id"]))
            if changed != 1:
                raise RuntimeError("external SELL finalization lost closing fence")
            inserted = await tx.execute(
                "INSERT INTO trade_events(id,user_id,position_id,event_type,amount_usd,pnl,ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, user["id"], position_id, "close",
                 round(p.initial_value, 2), pnl, now_iso()))
            if inserted != 1:
                raise RuntimeError("external SELL event insertion affected an unexpected row count")
    elif not result.submission_uncertain:
        await db.execute("DELETE FROM copy_positions WHERE id=? AND status='closing'",
                         (position_id,))
    return {"ok": result.ok, "reason": result.reason, "order_id": result.order_id,
            "avg_price": result.avg_price, "reconciliation_required": result.submission_uncertain}


@router.post("/{position_id}/close")
async def close_position(position_id: str, request: Request, body: CloseBody | None = None,
                         user=Depends(get_current_user), db=Depends(get_db), pmc=Depends(get_pm)):
    row = await db.fetchone(
        "SELECT * FROM copy_positions WHERE id = ? AND user_id = ? AND status = 'open'",
        (position_id, user["id"]))
    if not row:
        raise HTTPException(404, "open position not found")
    # Claim the row before placing the order: if the copy engine is closing this
    # same position concurrently (leader exited / market resolved), only one
    # caller wins the atomic status flip — the loser gets a clean 409 instead of
    # both submitting a SELL for the same shares.
    if not await db.claim_managed_sell(user["id"], row["token_id"], row["id"]):
        raise HTTPException(
            409, "position is being changed, reserved, or awaiting reconciliation")
    try:
        client = await get_user_client(request, user)
        slippage = body.acceptable_slippage_pct if body else MAX_COPY_SLIPPAGE_PCT
        result = await execution.place_market_order(
            client, pmc, row["token_id"], "SELL", row["shares"],
            reference_price=None, max_slippage_pct=slippage)
    except Exception:
        # execution reports transport ambiguity as a result; a raised exception
        # is therefore pre-submission and safe to release for retry.
        await db.try_transition(row["id"], "closing", "open")
        raise
    if result.ok:
        pnl = (result.avg_price - row["entry_price"]) * result.filled_shares
        async with db.transaction(write=True) as tx:
            changed = await tx.execute(
                "UPDATE copy_positions SET status='closed',exit_price=?,realized_pnl=?,closed_at=? "
                "WHERE id=? AND user_id=? AND status='closing'",
                (result.avg_price, pnl, now_iso(), row["id"], user["id"]))
            if changed != 1:
                raise RuntimeError("managed SELL finalization lost closing fence")
            inserted = await tx.execute(
                "INSERT INTO trade_events(id,user_id,position_id,event_type,amount_usd,pnl,ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, user["id"], row["id"], "close",
                 row["notional_usd"], pnl, now_iso()))
            if inserted != 1:
                raise RuntimeError("managed SELL event insertion affected an unexpected row count")
    elif not result.submission_uncertain:
        await db.try_transition(row["id"], "closing", "open")
    return {"ok": result.ok, "reason": result.reason, "order_id": result.order_id,
            "avg_price": result.avg_price,
            "reconciliation_required": result.submission_uncertain}
