"""Operator view of BUY fences: list them, and clear one.

A row in copy_open_claims fences BUYs of one token for one user. 'reserved'
and 'submitting' rows are in-flight work the engine settles by itself within
minutes (CopyEngine._recover_stale_claims). 'uncertain' rows are BUYs whose
outcome the exchange did not make clear. With POLYGON_RPC_URL set, the engine
settles those from the deposit wallet's on-chain balance once
UNCERTAIN_CLAIM_EVIDENCE_SECONDS have passed; otherwise it only adopts a
visible fill or keeps the fence. This tool is the way out for what neither
resolves: no SQL, and no HTTP endpoint (a CLI has no auth to get wrong). It
reads the same evidence the engine does, and refuses to clear a fence that the
chain says still guards untracked shares unless --force is given.

Production, inside the app container (the image ships backend/, not scripts/):
    docker compose exec app python -m backend.core.fences list
    docker compose exec app python -m backend.core.fences list --evidence
    docker compose exec app python -m backend.core.fences clear <claim_id>        # dry run
    docker compose exec app python -m backend.core.fences clear <claim_id> --yes

From a checkout (repo root, .env loaded automatically):
    .venv/bin/python scripts/fences.py list
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from backend.core.copy_engine import (
    SHARE_DUST,
    chain_verdict,
    live_token_rows,
    parse_utc,
    token_last_change,
)
from backend.core.ctf import CtfError


def _short(address: str) -> str:
    address = str(address or "")
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


def _short_token(token_id: str) -> str:
    token_id = str(token_id or "")
    return f"{token_id[:10]}…{token_id[-6:]}" if len(token_id) > 20 else token_id


def _age(raw) -> str:
    ts = parse_utc(raw)
    if ts is None:
        return "?"
    seconds = max(0.0, (dt.datetime.now(dt.timezone.utc) - ts).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.0f}{unit}"
    return f"{seconds:.0f}s"


def _claim_line(claim: dict) -> str:
    error = claim.get("last_error")
    return (f"{claim['claim_id']}  {claim['state']:<10} {str(claim.get('action') or ''):<6} "
            f"user {_short(claim['user_id'])}  token {_short_token(claim['token_id'])}  "
            f"${float(claim.get('reserved_usd') or 0):.2f} reserved  "
            f"claimed {_age(claim.get('claimed_at'))} ago, changed {_age(claim.get('updated_at'))} ago"
            + (f"  [{error}]" if error else ""))


def _rows_text(rows: list[dict]) -> str:
    if not rows:
        return "no tracked row"
    return ", ".join(f"{r['status']} row {r['id']} ({float(r['shares']):.4f} shares)"
                     for r in rows)


async def _indexed_shares(pm, user_id: str, token_id: str, cache: dict) -> float | None:
    """Shares the data-api shows (0 on a complete scan without it), or None."""
    if pm is None:
        return None
    if user_id not in cache:
        try:
            positions, complete = await pm.get_all_positions(user_id, size_threshold=0)
            cache[user_id] = ({p.asset: float(p.size) for p in positions
                               if p.size > SHARE_DUST}, complete)
        except Exception:
            cache[user_id] = None
    entry = cache[user_id]
    if entry is None:
        return None
    held, complete = entry
    if token_id in held:
        return held[token_id]
    return 0.0 if complete else None


async def list_fences(db, *, pm=None, ctf=None, evidence: bool = False, out=print) -> int:
    claims = await db.fetchall(
        "SELECT * FROM copy_open_claims WHERE state IN ('reserved','submitting','uncertain') "
        "ORDER BY claimed_at, user_id, token_id")
    if not claims:
        out("no active BUY fences")
        return 0
    head = head_error = None
    if evidence and ctf is not None:
        try:
            head = await ctf.head()
        except CtfError as exc:
            head_error = str(exc)
    indexed_cache: dict = {}
    for claim in claims:
        out(_claim_line(claim))
        rows = await live_token_rows(db, claim["user_id"], claim["token_id"])
        out(f"    tracked: {_rows_text(rows)}")
        if not evidence or claim["state"] != "uncertain":
            continue
        onchain = None
        if ctf is None:
            out("    on-chain: n/a (POLYGON_RPC_URL is not set)")
        elif head is None:
            out(f"    on-chain: unavailable ({head_error})")
        else:
            try:
                onchain = await ctf.balance(claim["user_id"], claim["token_id"], head)
                out(f"    on-chain: {onchain.shares:.4f} shares at block {head.number}")
            except CtfError as exc:
                out(f"    on-chain: unavailable ({exc})")
        indexed = await _indexed_shares(pm, claim["user_id"], claim["token_id"], indexed_cache)
        out("    indexer: " + (f"{indexed:.4f} shares" if indexed is not None else "unavailable"))
        if onchain is not None and indexed is not None:
            stamps = [t for t in (parse_utc(claim.get("updated_at")),
                                  await token_last_change(db, claim["user_id"], claim["token_id"]))
                      if t is not None]
            verdict, reason = chain_verdict(
                rows=rows, last_change=max(stamps) if stamps else None,
                onchain=onchain, indexed=indexed)
            out(f"    engine: {verdict.upper()}: {reason}")
    out(f"{len(claims)} active fence(s)")
    return 0


async def clear_fence(db, claim_id: str, *, ctf=None, yes: bool = False,
                      force: bool = False, out=print) -> int:
    claim = await db.fetchone("SELECT * FROM copy_open_claims WHERE claim_id=?", (claim_id,))
    if claim is None:
        out(f"no fence with claim id {claim_id}")
        return 1
    out(_claim_line(claim))
    if claim["state"] != "uncertain":
        out(f"refusing: a {claim['state']} claim is in-flight work, and the engine "
            "releases a stale one by itself within minutes")
        return 2
    rows = await live_token_rows(db, claim["user_id"], claim["token_id"])
    out(f"    tracked: {_rows_text(rows)}")
    tracked = sum(float(r["shares"]) for r in rows)
    objection = None
    if ctf is None:
        out("    on-chain: n/a (POLYGON_RPC_URL is not set); nothing here proves the BUY "
            "did not fill, so check the wallet on Polymarket first")
    else:
        try:
            head = await ctf.head()
            onchain = await ctf.balance(claim["user_id"], claim["token_id"], head)
            out(f"    on-chain: {onchain.shares:.4f} shares at block {head.number}")
            if onchain.shares > tracked + SHARE_DUST:
                objection = (f"the wallet holds {onchain.shares - tracked:.4f} shares on-chain "
                             f"beyond the {tracked:.4f} tracked, and clearing lets another "
                             "BUY stack on them")
        except CtfError as exc:
            out(f"    on-chain: unavailable ({exc})")
            objection = "the on-chain balance could not be read"
    if objection and not force:
        out(f"refusing: {objection}. Re-run with --force to clear it anyway.")
        return 2
    if not yes:
        out("dry run: nothing changed. Re-run with --yes to clear this fence.")
        return 0
    deleted = await db.execute(
        "DELETE FROM copy_open_claims WHERE claim_id=? AND user_id=? AND token_id=? "
        "AND state='uncertain'", (claim_id, claim["user_id"], claim["token_id"]))
    if deleted != 1:
        out("not cleared: the claim changed meanwhile; list the fences again")
        return 1
    out(f"cleared fence {claim_id}" + (" (forced)" if objection else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.core.fences",
        description="List and clear BUY fences (copy_open_claims).")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="show every active BUY fence")
    listing.add_argument(
        "--evidence", action="store_true",
        help="for uncertain fences, read the wallet's on-chain balance (needs "
             "POLYGON_RPC_URL) and the indexer, and show what the engine will do")
    clearing = commands.add_parser("clear", help="delete one uncertain fence")
    clearing.add_argument("claim_id")
    clearing.add_argument("--yes", action="store_true",
                          help="really delete it (without this it is a dry run)")
    clearing.add_argument("--force", action="store_true",
                          help="clear even when the chain shows untracked shares "
                               "or cannot be read")
    return parser


async def main(argv: list[str] | None = None) -> int:
    from backend.config import POLYGON_RPC_URL
    from backend.core.ctf import CtfReader
    from backend.core.polymarket import PolymarketClient
    from backend.db.database import Database

    args = build_parser().parse_args(argv)
    ctf = None
    if POLYGON_RPC_URL:
        try:
            ctf = CtfReader(POLYGON_RPC_URL)
        except Exception as exc:
            print(f"on-chain evidence unavailable ({type(exc).__name__})", file=sys.stderr)
    pm = PolymarketClient() if args.command == "list" and args.evidence else None
    db = Database()
    await db.connect()
    try:
        if args.command == "list":
            return await list_fences(db, pm=pm, ctf=ctf, evidence=args.evidence)
        return await clear_fence(db, args.claim_id, ctf=ctf, yes=args.yes, force=args.force)
    finally:
        await db.close()
        if ctf is not None:
            await ctf.aclose()
        if pm is not None:
            await pm.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
