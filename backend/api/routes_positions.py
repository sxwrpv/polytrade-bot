"""/api/positions/* — open (live unrealized), closed history, manual close."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.core import execution
from backend.api.deps import get_current_user, get_db, get_pm, get_user_client
from backend.db.database import now_iso

router = APIRouter()


@router.get("/open")
async def open_positions(user=Depends(get_current_user), db=Depends(get_db), pmc=Depends(get_pm)):
    rows = await db.fetchall(
        "SELECT * FROM copy_positions WHERE user_id = ? AND status = 'open' "
        "ORDER BY opened_at DESC", (user["id"],))
    # enrich with live current price / unrealized PnL (from the user's wallet positions)
    live = {p.asset: p for p in await pmc.get_positions(user["id"], size_threshold=0)}
    for r in rows:
        p = live.get(r["token_id"])
        r["current_price"] = p.cur_price if p else None
        r["unrealized_pnl"] = round(p.cash_pnl, 2) if p else None
    # positions the wallet holds that the engine didn't open (bought manually
    # on polymarket.com with the exported key, or resolution leftovers) — shown
    # so the dashboard always reflects the WHOLE wallet, marked external since
    # the bot doesn't manage their exit.
    managed = {r["token_id"] for r in rows}
    for p in live.values():
        if p.asset in managed or p.size <= 0.01:
            continue
        rows.append({
            "id": None, "external": True, "token_id": p.asset,
            "market_title": p.title, "market_slug": p.event_slug or p.slug,
            "outcome": (p.outcome or "").upper(), "shares": p.size,
            "entry_price": p.avg_price, "notional_usd": round(p.initial_value, 2),
            "current_price": p.cur_price, "unrealized_pnl": round(p.cash_pnl, 2),
            "status": "open", "trader_address": None, "redeemable": p.redeemable,
        })
    return rows


@router.get("/closed")
async def closed_positions(user=Depends(get_current_user), db=Depends(get_db)):
    return await db.fetchall(
        "SELECT * FROM copy_positions WHERE user_id = ? AND status IN ('closed','resolved') "
        "ORDER BY closed_at DESC", (user["id"],))


@router.post("/{position_id}/close")
async def close_position(position_id: str, request: Request,
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
    if not await db.try_transition(row["id"], "open", "closing"):
        raise HTTPException(409, "position is already being closed")
    client = await get_user_client(request, user)
    result = await execution.place_market_order(
        client, pmc, row["token_id"], "SELL", row["shares"],
        reference_price=row["entry_price"])
    if result.ok:
        pnl = (result.avg_price - row["entry_price"]) * result.filled_shares
        await db.execute(
            "UPDATE copy_positions SET status='closed', exit_price=?, realized_pnl=?, "
            "closed_at=? WHERE id=?", (result.avg_price, pnl, now_iso(), row["id"]))
        await db.execute(
            "INSERT INTO trade_events(id, user_id, position_id, event_type, amount_usd, pnl, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, user["id"], row["id"], "close", row["notional_usd"],
             pnl, now_iso()))
    else:
        await db.try_transition(row["id"], "closing", "open")
    return {"ok": result.ok, "reason": result.reason, "order_id": result.order_id,
            "avg_price": result.avg_price}
