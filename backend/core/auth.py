"""Session tokens + Telegram Mini App authentication.

Auth model: every user row carries a secret ``api_token`` (issued at wallet
creation, returned exactly once to the client, stored in its localStorage /
Telegram cloud). All authenticated endpoints require it as a Bearer token.
The public wallet address is NEVER accepted as auth — addresses are visible
on-chain the moment the bot trades, so address-header auth would let anyone
drain any user via /export-key.

Telegram Mini App login: the app runs inside Telegram, which hands the page a
signed ``initData`` string. ``validate_init_data`` checks Telegram's HMAC
(secret key = HMAC_SHA256(key=b"WebAppData", msg=bot_token) per
core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app) and
its freshness, yielding a trusted telegram user id. A user row linked to that
id gets its session token re-issued on every launch — so Telegram users can
never lose access by clearing storage; their Telegram account IS the login.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.parse

TOKEN_BYTES = 32
INIT_DATA_MAX_AGE = 24 * 3600   # reject initData older than a day (replay window)


def new_api_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


async def ensure_api_tokens(db) -> int:
    """Backfill tokens for rows created before token auth existed (idempotent)."""
    rows = await db.fetchall("SELECT id FROM users WHERE api_token IS NULL")
    for r in rows:
        await db.execute("UPDATE users SET api_token = ? WHERE id = ?",
                         (new_api_token(), r["id"]))
    return len(rows)


def validate_init_data(init_data: str, bot_token: str,
                       *, max_age: int = INIT_DATA_MAX_AGE) -> dict | None:
    """Verify a Telegram WebApp initData string. Returns the embedded ``user``
    object (dict with id/first_name/username/...) if authentic and fresh,
    else None. Never raises on malformed input."""
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
        if max_age and time.time() - int(fields.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(fields.get("user", "{}"))
        return user if isinstance(user, dict) and user.get("id") else None
    except (ValueError, TypeError):
        return None
