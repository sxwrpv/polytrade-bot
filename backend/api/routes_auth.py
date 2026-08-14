"""/api/auth/* — Telegram Mini App login."""
from __future__ import annotations

import datetime as dt
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from backend.config import TELEGRAM_BOT_TOKEN
from backend.core import auth
from backend.api.deps import get_current_user, get_db
from backend.db.database import now_iso

import aiosqlite

router = APIRouter()


class TelegramAuth(BaseModel):
    init_data: str


class LinkTelegram(BaseModel):
    init_data: str


LINK_STEP_UP_MAX_AGE = 300
LINK_CLAIM_STALE_SECONDS = 300


@router.post("/link-telegram")
async def link_telegram(body: LinkTelegram, user=Depends(get_current_user),
                        db=Depends(get_db)):
    """Explicitly bind an authenticated legacy wallet to a live Telegram user.

    The existing cookie selects the wallet; fresh signed initData proves the
    Telegram identity. The DB transaction preserves the existing wallet and
    refuses identities already owned by another row.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(503, "Telegram account linking is not configured")
    tg_user = auth.validate_init_data(
        body.init_data, TELEGRAM_BOT_TOKEN, max_age=LINK_STEP_UP_MAX_AGE)
    if not tg_user:
        raise HTTPException(401, "invalid or expired Telegram init data")
    telegram_user_id = int(tg_user["id"])
    claim_token = secrets.token_urlsafe(32)
    now = now_iso()
    stale_before = (dt.datetime.now(dt.timezone.utc) -
                    dt.timedelta(seconds=LINK_CLAIM_STALE_SECONDS)).isoformat()
    try:
        async with db.transaction(write=True) as tx:
            user_sql = "SELECT id,telegram_user_id FROM users WHERE id=?" + (
                " FOR UPDATE" if db.is_pg else "")
            current = await tx.fetchone(user_sql, (user["id"],))
            if not current:
                raise HTTPException(401, "authenticated wallet no longer exists")
            linked = current.get("telegram_user_id")
            if linked is not None:
                if int(linked) == telegram_user_id:
                    return {"address": current["id"], "linked": True,
                            "telegram_user_id": telegram_user_id}
                raise HTTPException(409, "wallet is already linked to another Telegram account")

            # Acquire the same durable identity fence as create-wallet and hold
            # it through the bind. Never steal side_effect_started or complete;
            # only an aged, provably pre-side-effect claim is recoverable.
            inserted = await tx.execute(
                "INSERT INTO wallet_creation_claims(telegram_user_id,claim_token,state,"
                "claimed_at,updated_at) VALUES(?,?,'claimed',?,?) "
                "ON CONFLICT(telegram_user_id) DO NOTHING",
                (telegram_user_id, claim_token, now, now),
            )
            if inserted != 1:
                claim_sql = (
                    "SELECT state,claim_token,updated_at FROM wallet_creation_claims "
                    "WHERE telegram_user_id=?" + (" FOR UPDATE" if db.is_pg else "")
                )
                claim = await tx.fetchone(claim_sql, (telegram_user_id,))
                recovered = 0
                if claim and claim["state"] == "claimed" and claim["updated_at"] < stale_before:
                    recovered = await tx.execute(
                        "UPDATE wallet_creation_claims SET claim_token=?,claimed_at=?,updated_at=?,"
                        "last_error=NULL WHERE telegram_user_id=? AND state='claimed' "
                        "AND updated_at < ?",
                        (claim_token, now, now, telegram_user_id, stale_before),
                    )
                if recovered != 1:
                    raise HTTPException(
                        409, "wallet creation or reconciliation is in progress; linking is blocked")
            owner = await tx.fetchone(
                "SELECT id FROM users WHERE telegram_user_id=?", (telegram_user_id,))
            if owner:
                raise HTTPException(409, "Telegram account is already linked to another wallet")
            changed = await tx.execute(
                "UPDATE users SET telegram_user_id=? WHERE id=? AND telegram_user_id IS NULL",
                (telegram_user_id, current["id"]),
            )
            if changed != 1:
                raise HTTPException(409, "wallet linking conflict")
            completed = await tx.execute(
                "UPDATE wallet_creation_claims SET state='complete',updated_at=? "
                "WHERE telegram_user_id=? AND claim_token=? AND state='claimed'",
                (now_iso(), telegram_user_id, claim_token),
            )
            if completed != 1:
                raise HTTPException(409, "Telegram link claim was lost before completion")
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(409, "Telegram account is already linked to another wallet") from exc
    return {"address": user["id"], "linked": True,
            "telegram_user_id": telegram_user_id}


@router.post("/logout")
async def logout(request: Request, response: Response, db=Depends(get_db)):
    """Revoke the exact server-side session named by this request's cookie."""
    raw = request.cookies.get(auth.SESSION_COOKIE)
    if raw:
        await db.execute(
            "UPDATE users SET api_token = NULL, api_token_expires_at = NULL "
            "WHERE api_token = ?",
            (auth.hash_session_token(raw),),
        )
    auth.clear_session_cookie(response)
    return {"ok": True}


@router.post("/telegram")
async def telegram_login(body: TelegramAuth, response: Response, db=Depends(get_db)):
    """Log in with Telegram's signed initData. If this Telegram account is
    linked to a wallet, re-issue its session — Telegram identity is the durable
    login, so clearing storage never locks a Telegram user out. If it isn't
    linked yet, respond with address=null and the frontend runs onboarding
    (create-wallet links the account via the same init_data)."""
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(501, "Telegram login is not configured on this server")
    tg_user = auth.validate_init_data(body.init_data, TELEGRAM_BOT_TOKEN)
    if not tg_user:
        raise HTTPException(401, "invalid or expired Telegram init data")
    user = await db.fetchone(
        "SELECT * FROM users WHERE telegram_user_id = ?", (int(tg_user["id"]),))
    if not user:
        return {"address": None, "linked": False}
    # Issue a fresh short-lived session and hand it back ONLY as an HttpOnly
    # cookie — the raw value is never in the response body (JS must not be able
    # to read or store it) and only its digest is persisted.
    raw = await auth.issue_session(db, user["id"])
    auth.set_session_cookie(response, raw)
    return {"address": user["id"], "linked": True,
            "display_name": user["display_name"],
            "gasless": user["id"] != user["signer_address"]}
