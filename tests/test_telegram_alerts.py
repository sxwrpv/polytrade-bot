from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from backend.core.telegram_alerts import TelegramPositionNotifier, format_position_alert


class TelegramAlertTests(unittest.IsolatedAsyncioTestCase):
    def test_formats_open_alert_with_market_and_fill(self):
        text = format_position_alert({
            "event": "opened", "market_title": "Will Spain win?", "outcome": "YES",
            "shares": 20, "entry_price": .5, "notional_usd": 10,
            "market_slug": "spain-win",
        })
        self.assertIn("POSITION OPENED", text)
        self.assertIn("Will Spain win?", text)
        self.assertIn("$10.00", text)
        self.assertIn("50.0¢", text)
        self.assertIn("https://polymarket.com/event/spain-win", text)

    def test_formats_close_alert_with_signed_pnl(self):
        text = format_position_alert({
            "event": "closed", "market_title": "Market", "outcome": "NO",
            "shares": 10, "entry_price": .4, "exit_price": .6, "realized_pnl": 2,
        })
        self.assertIn("POSITION CLOSED", text)
        self.assertIn("+$2.00", text)
        self.assertIn("40.0¢ → 60.0¢", text)

    async def test_sends_to_linked_telegram_user(self):
        db = AsyncMock()
        db.fetchone.return_value = {"telegram_user_id": 12345}
        http = AsyncMock()
        http.post.return_value = AsyncMock(raise_for_status=lambda: None)
        notifier = TelegramPositionNotifier(db, "bot-token", http=http)

        await notifier({"event": "opened", "user_id": "wallet", "market_title": "Market",
                        "outcome": "YES", "shares": 2, "entry_price": .5,
                        "notional_usd": 1})

        http.post.assert_awaited_once()
        self.assertEqual(12345, http.post.await_args.kwargs["json"]["chat_id"])

    async def test_skips_unlinked_user(self):
        db = AsyncMock()
        db.fetchone.return_value = {"telegram_user_id": None}
        http = AsyncMock()
        notifier = TelegramPositionNotifier(db, "bot-token", http=http)

        await notifier({"event": "opened", "user_id": "wallet"})

        http.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
