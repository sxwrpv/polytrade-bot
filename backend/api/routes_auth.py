"""/api/auth/* — Telegram Mini App login."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.config import TELEGRAM_BOT_TOKEN
from backend.core import auth
from backend.api.deps import get_db

router = APIRouter()


class TelegramAuth(BaseModel):
    init_data: str


@router.post("/telegram")
async def telegram_login(body: TelegramAuth, db=Depends(get_db)):
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
        return {"address": None, "api_token": None, "linked": False}
    return {"address": user["id"], "api_token": user["api_token"], "linked": True,
            "display_name": user["display_name"],
            "gasless": user["id"] != user["signer_address"]}
