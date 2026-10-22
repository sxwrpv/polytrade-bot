from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from backend.core.copy_engine import Action, CopyEngine
from backend.core.execution import OrderResult
from backend.core.telegram_alerts import (
    TelegramPositionNotifier, format_alert, format_deposit_alert, format_position_alert,
    format_withdrawal_alert,
)
from backend.db.database import Database, now_iso

DESTINATION = "0x3c3C3c3C3c3C3C3c3C3c3c3c3c3C3C3c3c3C3c3C"
WALLET = "0x1a1A1A1A1a1A1A1a1A1a1a1a1a1a1a1A1A1a1a1a"
DEPOSIT_TX = "0x" + "ab" * 32


class DepositAlertTests(unittest.IsolatedAsyncioTestCase):
    def event(self, **extra) -> dict:
        return {"event": "deposit_received", "user_id": WALLET, "deposit_id": "d" * 32,
                "amount_usd": "44.00", "tx_hash": DEPOSIT_TX, **extra}

    def test_shows_the_amount_the_asset_the_short_wallet_and_a_polygonscan_link(self):
        text = format_alert(self.event())
        self.assertTrue(text.startswith("💰 <b>DEPOSIT RECEIVED</b>"))
        for part in ("Amount: <b>$44.00</b>", "Arrived as: <b>pUSD</b>",
                     "To: <b>0x1a1A…1a1a</b>, your PolyTrade wallet on Polygon",
                     f'<a href="https://polygonscan.com/tx/{DEPOSIT_TX}">'):
            self.assertIn(part, text)
        self.assertNotIn(WALLET[2:].lower(), text.lower())
        self.assertNotIn("Landed", text)

    def test_a_late_alert_says_when_the_deposit_landed(self):
        text = format_deposit_alert(self.event(landed_at="2026-10-22T12:54:02+00:00"))
        self.assertIn("Landed: <b>22 Oct, 12:54 UTC</b>", text)
        self.assertNotIn("Landed", format_deposit_alert(self.event(landed_at="not a time")))

    async def test_it_goes_to_the_owners_chat_and_a_failure_is_logged_by_deposit_id(self):
        db = AsyncMock()
        db.fetchone.return_value = {"telegram_user_id": 12345}
        http = AsyncMock()
        http.post.return_value = SimpleNamespace(status_code=200)
        await TelegramPositionNotifier(db, "bot-token", http=http)(self.event())
        sent = http.post.await_args.kwargs["json"]
        self.assertEqual((sent["chat_id"], sent["parse_mode"]), (12345, "HTML"))
        self.assertIn("DEPOSIT RECEIVED", sent["text"])

        http.post.side_effect = httpx.ConnectError("unreachable")
        notifier = TelegramPositionNotifier(db, "bot-token", http=http, background=True)
        with self.assertLogs("telegram_alerts", level="ERROR") as logs:
            await notifier(self.event())
            await notifier.aclose()
        self.assertIn("alert failed for " + "d" * 32, logs.output[0])
        self.assertNotIn(WALLET[2:].lower(), "\n".join(logs.output).lower())


class WithdrawalAlertTests(unittest.TestCase):
    def event(self, kind: str, **extra) -> dict:
        return {"event": f"withdrawal_{kind}", "user_id": "0xwallet", "withdrawal_id": "w1",
                "amount_usd": "25.00", "destination": DESTINATION, **extra}

    def test_each_step_has_its_own_status_line(self):
        expected = {"requested": "WITHDRAWAL REQUESTED", "bridging": "WITHDRAWAL ON ITS WAY",
                    "confirmed": "WITHDRAWAL DELIVERED", "failed": "WITHDRAWAL NOT SENT",
                    "attention": "WITHDRAWAL NEEDS A CHECK"}
        for kind, status in expected.items():
            text = format_alert(self.event(kind, tx_hash="0x" + "f7" * 32,
                                           delivered_usd="24.99"))
            self.assertIn(status, text, kind)
            self.assertIn("0x3c3C…3c3C", text, kind)
            self.assertNotIn(DESTINATION[2:].lower(), text.lower(), kind)

    def test_delivery_links_the_transaction_and_failures_carry_the_reason(self):
        delivered = format_withdrawal_alert(self.event("confirmed", delivered_usd="24.99",
                                                       tx_hash="0x" + "e8" * 32))
        self.assertIn("24.99 USDC", delivered)
        self.assertIn("https://polygonscan.com/tx/0x" + "e8" * 32, delivered)
        failed = format_withdrawal_alert(self.event("failed", reason="Not sent: <busy>"))
        self.assertIn("$25.00", failed)
        self.assertIn("stayed in your PolyTrade wallet", failed)
        self.assertIn("Not sent: &lt;busy&gt;", failed)

    def test_position_events_keep_their_own_format(self):
        text = format_alert({"event": "opened", "market_title": "M", "outcome": "YES",
                             "shares": 1, "entry_price": .5, "notional_usd": 1})
        self.assertIn("POSITION OPENED", text)


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
        self.assertIn("https://polymarket.com/market/spain-win", text)

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
        http.post.return_value = SimpleNamespace(status_code=200)
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


class PartialFillAlertFormatTests(unittest.TestCase):
    def test_formats_increase_with_delta_and_new_total(self):
        text = format_position_alert({
            "event": "increased", "market_title": "Market", "outcome": "YES",
            "shares": 4, "entry_price": .55, "notional_usd": 2.2,
            "total_shares": 14,
        })
        self.assertIn("POSITION INCREASED", text)
        self.assertIn("$2.20", text)
        self.assertIn("55.0¢", text)
        self.assertIn("14.00 shares", text)

    def test_formats_reduction_with_pnl_and_remaining(self):
        text = format_position_alert({
            "event": "reduced", "market_title": "Market", "outcome": "NO",
            "shares": 5, "exit_price": .6, "realized_pnl": 1.0,
            "total_shares": 5,
        })
        self.assertIn("POSITION REDUCED", text)
        self.assertIn("+$1.00", text)
        self.assertIn("60.0¢", text)
        self.assertIn("5.00 shares", text)


class ResizeEmitsAlertTests(unittest.IsolatedAsyncioTestCase):
    """Partial exits are real confirmed executions — they must alert, and only
    AFTER the fill is confirmed and persisted."""

    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.db.execute(
            "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
            ("0x" + "1" * 40, "encrypted", now_iso()))
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?)",
            ("position", "0x" + "1" * 40, "0xleader", "condition", "token-1",
             "Market", "YES", 10.0, 100.0, 0.4, 4.0, now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def test_resize_down_sends_reduced_alert_after_persistence(self):
        alerts = []

        async def notify(event):
            # persistence must be committed before the alert goes out
            row = await self.db.fetchone(
                "SELECT shares FROM copy_positions WHERE id='position'")
            event["shares_in_db_at_alert"] = row["shares"]
            alerts.append(event)

        async def place(client, pm, token, side, amount, **kwargs):
            return OrderResult(ok=True, side=side, filled_shares=amount,
                               avg_price=0.6)

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place,
                            position_notifier=notify)
        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='position'")
        action = Action(kind="resize", subkind="decrease", token_id="token-1",
                        side="SELL", amount=5.0, reference_price=0.6,
                        trader_shares=50.0, row=row)

        await engine._execute("0x" + "1" * 40, object(), action)

        self.assertEqual(1, len(alerts))
        self.assertEqual("reduced", alerts[0]["event"])
        self.assertEqual(5.0, alerts[0]["shares"])
        self.assertAlmostEqual(1.0, alerts[0]["realized_pnl"])   # (0.6-0.4)*5
        self.assertEqual(5.0, alerts[0]["total_shares"])
        self.assertEqual(5.0, alerts[0]["shares_in_db_at_alert"])

    async def test_no_alert_when_order_is_skipped(self):
        alerts = []

        async def notify(event):
            alerts.append(event)

        async def place(client, pm, token, side, amount, **kwargs):
            return OrderResult(ok=False, reason="insufficient_liquidity")

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place,
                            position_notifier=notify)
        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='position'")
        action = Action(kind="resize", subkind="decrease", token_id="token-1",
                        side="SELL", amount=5.0, reference_price=0.6,
                        trader_shares=50.0, row=row)

        await engine._execute("0x" + "1" * 40, object(), action)

        self.assertEqual([], alerts)


if __name__ == "__main__":
    unittest.main()
