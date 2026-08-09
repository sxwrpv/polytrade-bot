"""Shared FastAPI dependencies: app-state accessors and cookie auth.

Authentication uses a short-lived random value held only in an HttpOnly cookie.
Only its SHA-256 digest is stored in the database. Wallet addresses and legacy
Bearer/X-API-Token headers are never accepted as credentials.
"""
from __future__ import annotations

import time

from fastapi import Header, HTTPException, Request

from backend.config import ENCRYPTION_SECRET
from backend.core import auth, wallet


def get_db(request: Request):
    return request.app.state.db


def get_pm(request: Request):
    return request.app.state.pm


async def get_current_user(request: Request,
                           authorization: str = Header(default=None),
                           x_api_token: str = Header(default=None)):
    """Authenticate from the HttpOnly session cookie only.

    The raw cookie value never touches the database — we look the session up by
    its SHA-256 digest, so a database/backup leak yields no usable credential.
    Sessions expire (auth.SESSION_TTL_SECONDS), so a captured cookie has a
    bounded lifetime. The `authorization` / `x_api_token` parameters remain in
    the signature only so callers and tests keep working; header credentials are
    deliberately NOT accepted — accepting them would reintroduce the
    XSS-readable, never-expiring token this replaced.
    """
    raw = request.cookies.get(auth.SESSION_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="missing session token")
    user = await request.app.state.db.fetchone(
        "SELECT * FROM users WHERE api_token = ?", (auth.hash_session_token(raw),))
    if not user:
        raise HTTPException(status_code=401, detail="invalid session")
    if auth.parse_session_expiry(user.get("api_token_expires_at")) <= time.time():
        raise HTTPException(status_code=401, detail="session expired — sign in again")
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
