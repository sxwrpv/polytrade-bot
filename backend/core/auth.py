"""Short-lived, server-stored session hashes + Telegram Mini App authentication.

``api.deps.get_current_user`` accepts the HttpOnly ``polytrade_session``
cookie and nothing else — Bearer and ``X-API-Token`` headers are refused.
Sessions expire (``users.api_token_expires_at``), and only the SHA-256 digest
of a session value is ever stored, so a database leak yields no usable
credential.

``invalidate_legacy_sessions`` runs once at boot and destroys any surviving
plaintext or non-expiring token from before the cookie cutover.
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
    from backend.config import SESSION_COOKIE_SECURE

    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response) -> None:
    from backend.config import SESSION_COOKIE_SECURE

    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
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
