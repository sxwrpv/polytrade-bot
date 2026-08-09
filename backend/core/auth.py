"""Short-lived, server-stored session hashes + Telegram Mini App authentication.

MIGRATION IN PROGRESS (hashed, expiring, cookie-borne sessions). The helpers
below — ``new_session`` / ``issue_session`` / ``*_session_cookie`` /
``invalidate_legacy_sessions`` — are the TARGET model but are not wired up yet:
``api.deps.get_current_user`` still authenticates a plaintext Bearer token,
``users.api_token_expires_at`` does not exist in the schema, and the frontend
still sends ``Authorization: Bearer``. Until those land together, the plaintext
issuing path (``new_api_token`` / ``ensure_api_tokens``) remains the live one —
deleting it took the whole service down at boot (main.py lifespan), so it stays
until the cookie path replaces it end to end.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse

TOKEN_BYTES = 32
SESSION_COOKIE = "polytrade_session"
SESSION_TTL_SECONDS = 12 * 3600
INIT_DATA_MAX_AGE = 24 * 3600
_HASH_PREFIX = "sha256:"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_api_token() -> str:
    """CURRENT live issuing path (plaintext Bearer). Superseded by
    ``new_session`` once cookie auth is wired end to end — see module docstring."""
    return secrets.token_urlsafe(TOKEN_BYTES)


async def ensure_api_tokens(db) -> int:
    """Backfill tokens for rows created before token auth existed (idempotent).

    Called from the main.py lifespan; must exist or the service fails to boot.
    """
    rows = await db.fetchall("SELECT id FROM users WHERE api_token IS NULL")
    for r in rows:
        await db.execute("UPDATE users SET api_token = ? WHERE id = ?",
                         (new_api_token(), r["id"]))
    return len(rows)


def hash_session_token(token: str) -> str:
    return _HASH_PREFIX + hashlib.sha256(token.encode()).hexdigest()


def parse_session_expiry(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def new_session(*, ttl_seconds: int = SESSION_TTL_SECONDS) -> tuple[str, str, str]:
    """Return raw cookie value, stored hash, and UTC expiry."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    expires = _utcnow() + dt.timedelta(seconds=ttl_seconds)
    return raw, hash_session_token(raw), expires.isoformat()


async def issue_session(db, user_id: str) -> str:
    raw, stored, expires_at = new_session()
    await db.execute(
        "UPDATE users SET api_token=?, api_token_expires_at=? WHERE id=?",
        (stored, expires_at, user_id),
    )
    return raw


def set_session_cookie(response, raw_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


async def invalidate_legacy_sessions(db) -> int:
    """One-way migration: discard plaintext/permanent tokens from old releases."""
    return await db.execute(
        "UPDATE users SET api_token = NULL, api_token_expires_at = NULL "
        "WHERE api_token IS NOT NULL AND "
        "(api_token NOT LIKE 'sha256:%' OR api_token_expires_at IS NULL)"
    )


def validate_init_data(init_data: str, bot_token: str,
                       *, max_age: int = INIT_DATA_MAX_AGE) -> dict | None:
    """Verify Telegram WebApp initData and return its trusted user object."""
    if not init_data or not bot_token:
        return None
    try:
        fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        their_hash = fields.pop("hash", "")
        if not their_hash:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, their_hash):
            return None
        auth_date = int(fields.get("auth_date", "0"))
        age = time.time() - auth_date
        if age < -30 or (max_age and age > max_age):
            return None
        user = json.loads(fields.get("user", "{}"))
        return user if isinstance(user, dict) and user.get("id") else None
    except (ValueError, TypeError):
        return None
