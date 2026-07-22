"""Owner broadcast — send a message to every Telegram-linked user via the bot.

Deliberately a local CLI, not an API endpoint: broadcasting is an owner-only
power and keeping it off the HTTP surface means there is no auth to get wrong.

Usage (from the repo root, .env loaded automatically):
    .venv/bin/python scripts/broadcast.py --dry-run  -m "..."   # list recipients only
    .venv/bin/python scripts/broadcast.py --to-owner -m "..."   # test on yourself first
    .venv/bin/python scripts/broadcast.py            -m "..."   # send to everyone

Message formatting: Telegram HTML (<b>, <i>, <a href>). Keep it honest and
sparing — Telegram lets a bot message anyone who has started it (all our users
did, wallet creation happens inside the bot), but every user can block the bot,
and spammy marketing is the fastest way to make that happen. Blocked users
(HTTP 403) are reported and skipped, never retried.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from backend.config import TELEGRAM_BOT_TOKEN
from backend.db.database import Database

SEND_DELAY_S = 0.05          # ~20 msg/s, safely under Telegram's ~30/s limit


async def broadcast(message: str, *, dry_run: bool, to_owner: bool) -> int:
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not configured", file=sys.stderr)
        return 2
    db = Database()
    await db.connect()
    try:
        users = await db.fetchall(
            "SELECT id, telegram_user_id, display_name FROM users "
            "WHERE telegram_user_id IS NOT NULL ORDER BY created_at")
        if to_owner:
            users = users[:1]   # oldest account = the owner's
        print(f"recipients: {len(users)}")
        for u in users:
            print(f"  {u['id'][:10]}… tg={u['telegram_user_id']} "
                  f"({u['display_name'] or 'no name'})")
        if dry_run:
            print("dry run — nothing sent")
            return 0
        sent = blocked = failed = 0
        async with httpx.AsyncClient(timeout=10) as http:
            for u in users:
                try:
                    r = await http.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": int(u["telegram_user_id"]),
                              "text": message, "parse_mode": "HTML",
                              "disable_web_page_preview": True})
                    if r.status_code == 403:      # user blocked the bot
                        blocked += 1
                        print(f"  blocked: {u['id'][:10]}…")
                    else:
                        r.raise_for_status()
                        sent += 1
                except Exception as exc:          # keep going; report at the end
                    failed += 1
                    print(f"  failed:  {u['id'][:10]}… ({exc})")
                await asyncio.sleep(SEND_DELAY_S)
        print(f"done: sent={sent} blocked={blocked} failed={failed}")
        return 0 if failed == 0 else 1
    finally:
        await db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-m", "--message", required=True,
                    help="message text (Telegram HTML allowed)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list recipients without sending")
    ap.add_argument("--to-owner", action="store_true",
                    help="send only to the first (owner) account as a test")
    args = ap.parse_args()
    return asyncio.run(broadcast(args.message, dry_run=args.dry_run,
                                 to_owner=args.to_owner))


if __name__ == "__main__":
    sys.exit(main())
