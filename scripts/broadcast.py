"""Owner broadcast — send a message to every Telegram-linked user via the bot.

Deliberately a local CLI, not an API endpoint: broadcasting is an owner-only
power and keeping it off the HTTP surface means there is no auth to get wrong.

Usage (from the repo root, .env loaded automatically):
    .venv/bin/python scripts/broadcast.py --dry-run  -m "..."   # preview + recipients only
    .venv/bin/python scripts/broadcast.py --to-owner -m "..."   # test on yourself first
    .venv/bin/python scripts/broadcast.py            -m "..."   # send to everyone

Optional presentation, matching the bot's position alerts:
    --title "Scheduled maintenance"   a bold 📣 header line above the message
    --app-button                      an "Open PolyTrade" button that opens the Mini App

Message formatting: Telegram HTML (<b>, <i>, <a href>, <code>, <blockquote>).
The message is checked against the tags Telegram accepts BEFORE anything is
sent: a malformed message would otherwise fail for every recipient with the
same HTTP 400. Keep it honest and sparing — Telegram lets a bot message anyone
who has started it (all our users did, wallet creation happens inside the
bot), but every user can block the bot, and spammy marketing is the fastest
way to make that happen. Blocked users (HTTP 403) are reported and skipped,
never retried.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import os
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from backend.config import TELEGRAM_BOT_TOKEN
from backend.db.database import Database

SEND_DELAY_S = 0.05          # ~20 msg/s, safely under Telegram's ~30/s limit
MAX_MESSAGE_CHARS = 4096     # Telegram's limit on a message's visible text
APP_URL = "https://polytradebot.live/"

# Bot API "HTML style": anything else makes sendMessage answer 400.
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "blockquote", "tg-spoiler", "span", "tg-emoji",
}


class _TelegramHTML(HTMLParser):
    """Collects the visible text and every reason Telegram would refuse it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[str] = []
        self.errors: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag not in ALLOWED_TAGS:
            hint = " (use a newline instead)" if tag == "br" else ""
            self.errors.append(f"<{tag}> is not supported by Telegram{hint}")
        elif tag == "a" and not attributes.get("href"):
            self.errors.append("<a> needs an href")
        elif tag == "span" and attributes.get("class") != "tg-spoiler":
            self.errors.append('<span> is only allowed as <span class="tg-spoiler">')
        if tag not in ("br", "hr", "img"):     # void tags never stay open
            self.open.append(tag)

    def handle_endtag(self, tag):
        if self.open and self.open[-1] == tag:
            self.open.pop()
        else:
            self.errors.append(f"</{tag}> does not close the tag that is open")

    def handle_data(self, data):
        self.text.append(data)


def compose(message: str, title: str | None = None) -> str:
    """The exact HTML that will be sent."""
    body = message.strip()
    if title:
        return f"📣 <b>{html.escape(title.strip())}</b>\n\n{body}"
    return body


def check(text: str) -> tuple[list[str], str]:
    """(problems, visible text). Any problem means nothing may be sent."""
    parser = _TelegramHTML()
    parser.feed(text)
    parser.close()
    problems = list(parser.errors)
    problems += [f"<{tag}> is never closed" for tag in parser.open]
    visible = "".join(parser.text)
    if not visible.strip():
        problems.append("the message has no visible text")
    if len(visible) > MAX_MESSAGE_CHARS:
        problems.append(f"the message is {len(visible)} characters; Telegram's limit is {MAX_MESSAGE_CHARS}")
    return problems, visible


def payload(chat_id: int, text: str, *, app_button: bool) -> dict:
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if app_button:
        body["reply_markup"] = {"inline_keyboard": [[
            {"text": "Open PolyTrade", "web_app": {"url": APP_URL}},
        ]]}
    return body


def print_preview(text: str, visible: str, *, app_button: bool) -> None:
    print("message (as sent, Telegram HTML):")
    print("  " + text.replace("\n", "\n  "))
    print(f"preview ({len(visible)} characters):")
    print("  " + visible.replace("\n", "\n  "))
    if app_button:
        print("  [ Open PolyTrade ]  → opens the Mini App")


async def broadcast(message: str, *, dry_run: bool, to_owner: bool,
                    title: str | None = None, app_button: bool = False) -> int:
    text = compose(message, title)
    problems, visible = check(text)
    if problems:
        print("not sent — Telegram would refuse this message:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not configured", file=sys.stderr)
        return 2
    print_preview(text, visible, app_button=app_button)
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
                        json=payload(int(u["telegram_user_id"]), text, app_button=app_button))
                    if r.status_code == 403:      # user blocked the bot
                        blocked += 1
                        print(f"  blocked: {u['id'][:10]}…")
                    elif r.status_code >= 400:
                        failed += 1
                        print(f"  failed:  {u['id'][:10]}… (HTTP {r.status_code})")
                    else:
                        sent += 1
                except Exception as exc:          # keep going; report at the end
                    # Type only: httpx error text carries the request URL, and
                    # the Bot API URL embeds the token.
                    failed += 1
                    print(f"  failed:  {u['id'][:10]}… ({type(exc).__name__})")
                await asyncio.sleep(SEND_DELAY_S)
        print(f"done: sent={sent} blocked={blocked} failed={failed}")
        return 0 if failed == 0 else 1
    finally:
        await db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-m", "--message", required=True,
                    help="message text (Telegram HTML allowed)")
    ap.add_argument("--title", help="optional bold header line, shown with 📣")
    ap.add_argument("--app-button", action="store_true",
                    help="attach an 'Open PolyTrade' button that opens the Mini App")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview the message and list recipients without sending")
    ap.add_argument("--to-owner", action="store_true",
                    help="send only to the first (owner) account as a test")
    args = ap.parse_args()
    return asyncio.run(broadcast(args.message, dry_run=args.dry_run,
                                 to_owner=args.to_owner, title=args.title,
                                 app_button=args.app_button))


if __name__ == "__main__":
    sys.exit(main())
