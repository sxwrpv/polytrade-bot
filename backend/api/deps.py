"""Shared FastAPI dependencies: app-state accessors, auth, per-user CLOB client.

Auth: Bearer session token (users.api_token — a secret issued at wallet
creation or re-issued via Telegram login). NOT the wallet address: addresses
are public on-chain data the moment the bot trades, so address-header auth
would let anyone act as any user — including exporting their private key.
"""
from __future__ import annotations

from fastapi import Header, HTTPException, Request

from backend.config import ENCRYPTION_SECRET
from backend.core import wallet


def get_db(request: Request):
    return request.app.state.db


def get_pm(request: Request):
    return request.app.state.pm


async def get_current_user(request: Request,
                           authorization: str = Header(default=None),
                           x_api_token: str = Header(default=None)):
    token = x_api_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing session token")
    user = await request.app.state.db.fetchone(
        "SELECT * FROM users WHERE api_token = ?", (token,))
    if not user:
        raise HTTPException(status_code=401, detail="invalid session")
    return user


async def get_user_client(request: Request, user: dict):
    """Build + cache an authenticated CLOB client for a user (derives API creds —
    hits the network on first build per process)."""
    cache = request.app.state.clients
    cid = user["id"]
    if cid not in cache:
        pk = wallet.decrypt_private_key(user["private_key_enc"], ENCRYPTION_SECRET)
        cache[cid] = await wallet.make_clob_client(pk, funder=cid)
    return cache[cid]
