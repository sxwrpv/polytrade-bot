"""Best-effort Telegram alerts for persisted position lifecycle events."""
from __future__ import annotations

import html

import httpx


def _cents(value) -> str:
    return f"{float(value or 0) * 100:.1f}¢"


def format_position_alert(event: dict) -> str:
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


class TelegramPositionNotifier:
    def __init__(self, db, bot_token: str, *, http=None) -> None:
        self.db = db
        self.bot_token = bot_token
        self.http = http or httpx.AsyncClient(timeout=10)
        self._owns_http = http is None

    async def __call__(self, event: dict) -> None:
        if not self.bot_token:
            return
        user = await self.db.fetchone(
            "SELECT telegram_user_id FROM users WHERE id=?", (event["user_id"],))
        chat_id = user.get("telegram_user_id") if user else None
        if not chat_id:
            return
        response = await self.http.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={
                "chat_id": int(chat_id),
                "text": format_position_alert(event),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()
