"""Owner-only permissions for files containing credentials, wallets, or logs."""
from __future__ import annotations

import os
from pathlib import Path


def harden_runtime_files(root: str | Path, *, db_path: str = "copybot.db") -> None:
    root = Path(root).resolve()
    logs = root / "logs"
    if logs.exists():
        logs.chmod(0o700)

    db = Path(db_path)
    if not db.is_absolute():
        db = root / db
    candidates = [root / ".env", db, Path(f"{db}-wal"), Path(f"{db}-shm")]
    if logs.exists():
        candidates.extend(p for p in logs.iterdir() if p.is_file())
    for path in candidates:
        if path.exists() and path.is_file():
            path.chmod(0o600)


def secure_process_umask() -> None:
    """Make newly created DB, WAL, log, and secret files owner-only by default."""
    os.umask(0o077)
