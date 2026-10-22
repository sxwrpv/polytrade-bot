"""Best-effort Telegram alerts for persisted position lifecycle events."""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging

import httpx

log = logging.getLogger("telegram_alerts")


def _cents(value) -> str:
    return f"{float(value or 0) * 100:.1f}¢"


def _signed_usd(value) -> str:
    amount = float(value or 0)
    return f"{'+' if amount >= 0 else '-'}${abs(amount):.2f}"


def _source(trader_address) -> str:
    """Which wallet the trade came from, shortened the way the Mini App shows it."""
    address = str(trader_address or "").strip()
    if not address:
        return ""
    if address == "manual":
        return "Manual trade"
    short = f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address
    return f"Copied from {html.escape(short)}"


def format_position_alert(event: dict) -> str:
    """One message per CONFIRMED execution. Senders only call this after the
    exchange confirmed the fill (or the resolution was recorded) AND the DB
    committed — never on detection, intent, or a skipped/rejected order.

    Layout, the same for every kind so alerts scan alike in a chat:
      status line   emoji + bold label (what happened; shows in the push preview)
      market        bold title
      facts         a Telegram blockquote: outcome, size or shares, price(s), P&L
      footer        the wallet it was copied from and a link to the market
    """
    kind = str(event.get("event", "")).lower()
    title = html.escape(str(event.get("market_title") or "Polymarket position"))
    outcome = html.escape(str(event.get("outcome") or "").upper())
    shares = float(event.get("shares") or 0)
    slug = str(event.get("market_slug") or "").strip()

    if kind == "opened":
        status = "🟢 <b>POSITION OPENED</b>"
        facts = [
            f"Outcome: <b>{outcome}</b>",
            f"Size: <b>${float(event.get('notional_usd') or 0):.2f}</b> ({shares:.2f} shares)",
            f"Entry: <b>{_cents(event.get('entry_price'))}</b>",
        ]
    elif kind == "increased":       # confirmed partial BUY (leader topped up)
        status = "🟢 <b>POSITION INCREASED</b>"
        facts = [
            f"Outcome: <b>{outcome}</b>",
            f"Added: <b>${float(event.get('notional_usd') or 0):.2f}</b> "
            f"({shares:.2f} shares) @ <b>{_cents(event.get('entry_price'))}</b>",
            f"Now holding: <b>{float(event.get('total_shares') or 0):.2f} shares</b>",
        ]
    elif kind == "reduced":         # confirmed partial SELL (leader trimmed)
        status = "🟠 <b>POSITION REDUCED</b>"
        facts = [
            f"Outcome: <b>{outcome}</b>",
            f"Sold: <b>{shares:.2f} shares</b> @ <b>{_cents(event.get('exit_price'))}</b>",
            f"Realized P&amp;L: <b>{_signed_usd(event.get('realized_pnl'))}</b>",
            f"Still holding: <b>{float(event.get('total_shares') or 0):.2f} shares</b>",
        ]
    else:
        pnl = float(event.get("realized_pnl") or 0)
        label = "POSITION RESOLVED" if kind == "resolved" else "POSITION CLOSED"
        icon = "✅" if pnl >= 0 else "🔴"
        status = f"{icon} <b>{label}</b>"
        facts = [
            f"Outcome: <b>{outcome}</b>",
            f"Shares: <b>{shares:.2f}</b>",
            f"Price: <b>{_cents(event.get('entry_price'))} → {_cents(event.get('exit_price'))}</b>",
            f"Realized P&amp;L: <b>{_signed_usd(pnl)}</b>",
        ]

    lines = [status, "", f"<b>{title}</b>", "<blockquote>" + "\n".join(facts) + "</blockquote>"]
    footer = [part for part in (_source(event.get("trader_address")),) if part]
    if slug:
        # /market/<market slug> opens the market inside its event for single-
        # and multi-outcome events alike; /event/ takes the EVENT slug.
        safe_url = "https://polymarket.com/market/" + html.escape(slug, quote=True)
        footer.append(f'<a href="{safe_url}">View market on Polymarket</a>')
    if footer:
        lines.append(" · ".join(footer))
    return "\n".join(lines)


def _short_address(address) -> str:
    text = str(address or "").strip()
    return f"{text[:6]}…{text[-4:]}" if len(text) > 12 else text


def _polygonscan_link(tx_hash) -> str:
    tx = str(tx_hash or "").strip()
    return (f'<a href="https://polygonscan.com/tx/{html.escape(tx, quote=True)}">'
            "View transaction on Polygonscan</a>") if tx else ""


def _utc_time(value) -> str:
    """An ISO timestamp as '22 Oct, 12:54 UTC'; empty when it does not parse."""
    try:
        moment = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    moment = moment.astimezone(dt.timezone.utc)
    return f"{moment.day} {moment:%b}, {moment:%H:%M} UTC"


def format_deposit_alert(event: dict) -> str:
    """One message per deposit (backend/core/deposits.py), laid out like the
    withdrawal alerts. The wallet appears shortened only."""
    amount = html.escape(str(event.get("amount_usd") or "0.00"))
    wallet = html.escape(_short_address(event.get("user_id")))
    facts = [f"Amount: <b>${amount}</b>", "Arrived as: <b>pUSD</b>",
             f"To: <b>{wallet}</b>, your PolyTrade wallet on Polygon"]
    landed = _utc_time(event.get("landed_at"))
    if landed:
        # Only a late alert carries this (a scan catching up after downtime).
        facts.append(f"Landed: <b>{landed}</b>")
    lines = ["💰 <b>DEPOSIT RECEIVED</b>", "<blockquote>" + "\n".join(facts) + "</blockquote>"]
    link = _polygonscan_link(event.get("tx_hash"))
    if link:
        lines.append(link)
    return "\n".join(lines)


def format_withdrawal_alert(event: dict) -> str:
    """One message per withdrawal step (backend/core/withdraw.py), laid out
    like the position alerts. The destination appears shortened only."""
    kind = str(event.get("event", "")).removeprefix("withdrawal_")
    amount = html.escape(str(event.get("amount_usd") or "0.00"))
    to = html.escape(_short_address(event.get("destination")))
    link = _polygonscan_link(event.get("tx_hash"))
    if kind == "requested":
        status = "🟡 <b>WITHDRAWAL REQUESTED</b>"
        facts = [f"Amount: <b>${amount}</b>", f"To: <b>{to}</b> on Polygon",
                 "Arrives as: <b>USDC</b>"]
        footer = "Not you? Secure your Telegram account right away."
    elif kind == "bridging":
        status = "🔵 <b>WITHDRAWAL ON ITS WAY</b>"
        facts = [f"Sent: <b>${amount}</b> from your PolyTrade wallet",
                 f"To: <b>{to}</b> on Polygon"]
        footer = "The bridge is converting it to USDC. This usually takes under a minute."
    elif kind == "confirmed":
        status = "✅ <b>WITHDRAWAL DELIVERED</b>"
        delivered = html.escape(str(event.get("delivered_usd") or amount))
        facts = [f"Delivered: <b>{delivered} USDC</b>", f"To: <b>{to}</b> on Polygon"]
        footer = link
    elif kind == "attention":
        status = "🟠 <b>WITHDRAWAL NEEDS A CHECK</b>"
        facts = [f"Amount: <b>${amount}</b>", f"To: <b>{to}</b> on Polygon"]
        footer = ("It left your PolyTrade wallet, and the bridge has not confirmed delivery "
                  "yet. PolyTrade is following up with the bridge provider.")
    else:
        status = "🔴 <b>WITHDRAWAL NOT SENT</b>"
        facts = [f"Amount: <b>${amount}</b> stayed in your PolyTrade wallet",
                 f"To: <b>{to}</b> on Polygon"]
        reason = str(event.get("reason") or "").strip()
        footer = html.escape(reason) if reason else ""
    lines = [status, "<blockquote>" + "\n".join(facts) + "</blockquote>"]
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def format_alert(event: dict) -> str:
    kind = str(event.get("event", ""))
    if kind.startswith("withdrawal_"):
        return format_withdrawal_alert(event)
    if kind.startswith("deposit_"):
        return format_deposit_alert(event)
    return format_position_alert(event)


def _subject(event: dict):
    """The id of what an alert is about, for log lines (they carry no address)."""
    return event.get("position_id") or event.get("withdrawal_id") or event.get("deposit_id")


class TelegramAPIError(RuntimeError):
    """A failed sendMessage, described WITHOUT the request URL.

    The Bot API URL embeds the bot token (/bot<TOKEN>/sendMessage), and httpx
    puts that URL in HTTPStatusError's message. The engine logs notifier
    failures with a traceback, so re-raising the httpx error wrote the token
    into the logs on every Telegram 4xx/5xx.
    """


class TelegramRateLimited(TelegramAPIError):
    """sendMessage answered 429; retry_after is Telegram's own instruction."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Telegram sendMessage rate limited (retry after {retry_after:g}s)")


class TelegramPositionNotifier:
    # Telegram accepts about 30 messages a second per bot (and roughly one a
    # second per chat). One leader trade fans out to every follower at once,
    # so unpaced bursts were refused with 429 and those alerts were lost.
    SEND_INTERVAL_SECONDS = 1 / 25
    QUEUE_MAX = 1000
    MAX_ATTEMPTS = 3
    RATE_LIMIT_FLOOR_SECONDS = 1.0

    def __init__(self, db, bot_token: str, *, http=None, background: bool = False) -> None:
        self.db = db
        self.bot_token = bot_token
        self.http = http or httpx.AsyncClient(timeout=10)
        self._owns_http = http is None
        # background=True: __call__ only enqueues, so an alert never holds up
        # the order path (it was awaited inline, inside the BUY lock).
        self.background = background
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None

    async def __call__(self, event: dict) -> None:
        if not self.bot_token:
            return
        if not self.background:
            await self._deliver(event)
            return
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("alert queue full — dropped alert for %s", _subject(event))

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                for attempt in range(self.MAX_ATTEMPTS):
                    try:
                        await self._deliver(event)
                        break
                    except TelegramRateLimited as exc:
                        if attempt == self.MAX_ATTEMPTS - 1:
                            raise
                        await asyncio.sleep(
                            min(30.0, max(self.RATE_LIMIT_FLOOR_SECONDS, exc.retry_after)))
            except Exception:
                log.exception("alert failed for %s", _subject(event))
            finally:
                self._queue.task_done()
            await asyncio.sleep(self.SEND_INTERVAL_SECONDS)

    async def _deliver(self, event: dict) -> None:
        user = await self.db.fetchone(
            "SELECT telegram_user_id FROM users WHERE id=?", (event["user_id"],))
        chat_id = user.get("telegram_user_id") if user else None
        if not chat_id:
            return
        try:
            response = await self.http.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id": int(chat_id),
                    "text": format_alert(event),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        except httpx.HTTPError as exc:
            # `from None`: a chained cause would print the URL in the traceback.
            raise TelegramAPIError(
                f"Telegram sendMessage failed: {type(exc).__name__}") from None
        if response.status_code == 429:
            raise TelegramRateLimited(_retry_after(response))
        if not 200 <= response.status_code < 300:   # raise_for_status() parity
            raise TelegramAPIError(
                f"Telegram sendMessage returned HTTP {response.status_code}")

    async def aclose(self) -> None:
        if self._worker is not None:
            # Let alerts already queued go out (bounded), then stop the worker.
            try:
                await asyncio.wait_for(self._queue.join(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
            self._worker = None
        if self._owns_http:
            await self.http.aclose()


def _retry_after(response) -> float:
    """Telegram's parameters.retry_after, defaulting to one second."""
    try:
        return float(response.json()["parameters"]["retry_after"])
    except Exception:
        return 1.0
