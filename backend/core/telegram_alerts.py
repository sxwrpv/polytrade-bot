"""Best-effort Telegram alerts for persisted position lifecycle events."""
from __future__ import annotations

import asyncio
import html
import logging

import httpx

log = logging.getLogger("telegram_alerts")


def _cents(value) -> str:
    return f"{float(value or 0) * 100:.1f}¢"


def format_position_alert(event: dict) -> str:
    """One message per CONFIRMED execution. Senders only call this after the
    exchange confirmed the fill (or the resolution was recorded) AND the DB
    committed — never on detection, intent, or a skipped/rejected order."""
    kind = str(event.get("event", "")).lower()
    title = html.escape(str(event.get("market_title") or "Polymarket position"))
    outcome = html.escape(str(event.get("outcome") or "").upper())
    shares = float(event.get("shares") or 0)
    slug = str(event.get("market_slug") or "").strip()

    if kind == "opened":
        text = (
            "🟢 <b>POSITION OPENED</b>\n\n"
            f"<b>{title}</b>\n"
            f"Outcome: <b>{outcome}</b>\n"
            f"Size: <b>${float(event.get('notional_usd') or 0):.2f}</b> "
            f"({shares:.2f} shares)\n"
            f"Entry: <b>{_cents(event.get('entry_price'))}</b>"
        )
    elif kind == "increased":       # confirmed partial BUY (leader topped up)
        text = (
            "🟢 <b>POSITION INCREASED</b>\n\n"
            f"<b>{title}</b>\n"
            f"Outcome: <b>{outcome}</b>\n"
            f"Added: <b>${float(event.get('notional_usd') or 0):.2f}</b> "
            f"({shares:.2f} shares) @ <b>{_cents(event.get('entry_price'))}</b>\n"
            f"Now holding: <b>{float(event.get('total_shares') or 0):.2f} shares</b>"
        )
    elif kind == "reduced":         # confirmed partial SELL (leader trimmed)
        pnl = float(event.get("realized_pnl") or 0)
        text = (
            "🟠 <b>POSITION REDUCED</b>\n\n"
            f"<b>{title}</b>\n"
            f"Outcome: <b>{outcome}</b>\n"
            f"Sold: <b>{shares:.2f} shares</b> @ <b>{_cents(event.get('exit_price'))}</b>\n"
            f"Realized P&amp;L: <b>{'+' if pnl >= 0 else '-'}${abs(pnl):.2f}</b>\n"
            f"Still holding: <b>{float(event.get('total_shares') or 0):.2f} shares</b>"
        )
    else:
        pnl = float(event.get("realized_pnl") or 0)
        label = "POSITION RESOLVED" if kind == "resolved" else "POSITION CLOSED"
        icon = "✅" if pnl >= 0 else "🔴"
        text = (
            f"{icon} <b>{label}</b>\n\n"
            f"<b>{title}</b>\n"
            f"Outcome: <b>{outcome}</b>\n"
            f"Shares: <b>{shares:.2f}</b>\n"
            f"Price: <b>{_cents(event.get('entry_price'))} → "
            f"{_cents(event.get('exit_price'))}</b>\n"
            f"Realized P&amp;L: <b>{'+' if pnl >= 0 else '-'}${abs(pnl):.2f}</b>"
        )
    if slug:
        safe_url = "https://polymarket.com/event/" + html.escape(slug, quote=True)
        text += f'\n\n<a href="{safe_url}">View market</a>'
    return text


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
            log.warning("alert queue full — dropped alert for position %s",
                        event.get("position_id"))

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
                log.exception("position alert failed for %s", event.get("position_id"))
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
                    "text": format_position_alert(event),
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
