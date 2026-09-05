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
    # Per-path, because the loop used to abort on the first unreadable
    # candidate and silently skip everything after it. In the container that
    # is /app/data/copybot.db -- root-owned 0700 in the bind mount, unreadable
    # to the non-root app user -- so this function raised PermissionError and
    # hardened NOTHING, logging an ERROR on every boot since 2026-08-27 while
    # the runbook said it ran.
    for path in candidates:
        try:
            if path.exists() and path.is_file():
                path.chmod(0o600)
        except OSError:
            # Not ours to tighten (or already gone). The other candidates still
            # matter more than this one's failure.
            continue


def secure_process_umask() -> None:
    """Make newly created DB, WAL, log, and secret files owner-only by default."""
    os.umask(0o077)


# launchd owns the log file (StandardOutPath), so a Python RotatingFileHandler
# cannot rotate it — the process only ever appends to a descriptor launchd
# opened. Trimming in place at boot is the one lever we do have.
LOG_MAX_BYTES = 64 * 1024 * 1024      # trim above this…
LOG_KEEP_BYTES = 8 * 1024 * 1024      # …down to this much recent history


def trim_oversized_logs(root: str | Path, *, max_bytes: int = LOG_MAX_BYTES,
                        keep_bytes: int = LOG_KEEP_BYTES) -> list[str]:
    """Truncate oversized log files in place, keeping the most recent tail.

    Truncating (rather than deleting) matters: the running process holds the
    file open in append mode, so unlinking it would free no space until restart
    while writes silently continued into a detached inode. Returns the files
    trimmed. Best-effort — a concurrent write during the rewrite can cost a few
    lines, which is an acceptable trade for bounded disk use.
    """
    logs = Path(root).resolve() / "logs"
    trimmed: list[str] = []
    if not logs.is_dir():
        return trimmed
    for path in logs.iterdir():
        try:
            if not path.is_file() or path.stat().st_size <= max_bytes:
                continue
            with path.open("rb") as fh:
                fh.seek(-keep_bytes, os.SEEK_END)
                fh.readline()                    # drop the partial first line
                tail = fh.read()
            with path.open("r+b") as fh:
                fh.write(tail)
                fh.truncate()
            trimmed.append(path.name)
        except OSError:
            continue
    return trimmed
