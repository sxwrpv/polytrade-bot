"""Local-only helpers for Vite / Mobile Preview dashboard development."""
from __future__ import annotations

from eth_account import Account

from backend.config import DEV_PREVIEW, ENCRYPTION_SECRET
from backend.core import wallet
from backend.db.database import now_iso

DEV_PREVIEW_USER = "0x" + "d" * 40


async def ensure_dev_preview_user(db) -> str | None:
    """Seed one local wallet when DEV_PREVIEW is on and the DB has no users."""
    if not DEV_PREVIEW:
        return None
    if not ENCRYPTION_SECRET:
        raise RuntimeError("DEV_PREVIEW requires ENCRYPTION_SECRET")
    existing = await db.fetchone("SELECT id FROM users LIMIT 1")
    if existing:
        return existing["id"]
    acct = Account.create()
    signer = acct.address
    enc = wallet.encrypt_private_key(acct.key.hex(), ENCRYPTION_SECRET)
    await db.execute(
        "INSERT INTO users(id, signer_address, private_key_enc, created_at) VALUES(?,?,?,?)",
        (DEV_PREVIEW_USER, signer, enc, now_iso()),
    )
    return DEV_PREVIEW_USER
