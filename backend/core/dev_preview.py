"""Local-only helpers for Vite / Mobile Preview dashboard development."""
from __future__ import annotations

from eth_account import Account

from backend.config import DEV_PREVIEW, ENCRYPTION_SECRET
from backend.core import wallet
from backend.db.database import now_iso

DEV_PREVIEW_USER = "0x" + "d" * 40


async def ensure_dev_preview_user(db) -> str | None:
    """Seed the local preview wallet when DEV_PREVIEW is on; return its id.

    Only ever returns DEV_PREVIEW_USER. This used to return whichever user
    came first when the database was not empty, so DEV_PREVIEW=1 against a
    copy of production (or production itself) made the unauthenticated
    POST /api/auth/dev-login hand anyone a session for a REAL wallet. Real
    wallets present -> None. Postgres -> None: the preview is for a local
    SQLite file, never a shared database.
    """
    if not DEV_PREVIEW:
        return None
    if getattr(db, "is_pg", False) is True:
        return None
    if not ENCRYPTION_SECRET:
        raise RuntimeError("DEV_PREVIEW requires ENCRYPTION_SECRET")
    if await db.fetchone("SELECT id FROM users WHERE id = ?", (DEV_PREVIEW_USER,)):
        return DEV_PREVIEW_USER
    if await db.fetchone("SELECT id FROM users LIMIT 1"):
        return None
    acct = Account.create()
    signer = acct.address
    enc = wallet.encrypt_private_key(acct.key.hex(), ENCRYPTION_SECRET)
    await db.execute(
        "INSERT INTO users(id, signer_address, private_key_enc, created_at) VALUES(?,?,?,?)",
        (DEV_PREVIEW_USER, signer, enc, now_iso()),
    )
    return DEV_PREVIEW_USER
