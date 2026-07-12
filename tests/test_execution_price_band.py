from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace

from backend.core.execution import place_market_order
from backend.core.polymarket import Level, OrderBook
from polymarket.errors import InsufficientLiquidityError, TransportError


class FakePM:
    async def get_geoblock(self):
        return {"blocked": False}

    async def get_orderbook(self, token_id):
        return OrderBook(
            token_id=token_id,
            condition_id="condition",
            bids=(Level(0.97, 100),),
            asks=(Level(0.99, 100),),
            tick_size=0.01,
            min_order_size=1,
            neg_risk=False,
            last_trade_price=0.99,
        )


class FakeClient:
    def __init__(self):
        self.calls = 0

    async def place_market_order(self, **kwargs):
        self.calls += 1
        raise AssertionError("an out-of-band fill must never reach the exchange")


class CapturingClient:
    def __init__(self):
        self.kwargs = None

    async def place_market_order(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            ok=True, order_id="order", status="matched",
            making_amount=10, taking_amount=20,
            trade_ids=("trade",), transactions_hashes=(),
            model_dump=lambda: {},
        )


class FailingClient:
    def __init__(self, error):
        self.error = error

    async def place_market_order(self, **kwargs):
        raise self.error


class FineTickPM(FakePM):
    async def get_orderbook(self, token_id):
        book = await super().get_orderbook(token_id)
        return OrderBook(
            token_id=book.token_id, condition_id=book.condition_id,
            bids=(Level(0.49, 100),), asks=(Level(0.50, 100),),
            tick_size=0.005, min_order_size=1, neg_risk=False,
            last_trade_price=0.50,
        )


class AbsolutePriceBandTests(unittest.IsolatedAsyncioTestCase):
    async def test_fok_insufficient_liquidity_is_a_definitive_kill(self):
        result = await place_market_order(
            FailingClient(InsufficientLiquidityError(
                "order couldn't be fully filled. FOK orders are fully filled or killed.")),
            FineTickPM(), "token", "BUY", 10,
            reference_price=0.50, max_slippage_pct=2,
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.submission_uncertain)
        self.assertIn("insufficient_liquidity", result.reason)

    async def test_transport_failure_after_submission_remains_uncertain(self):
        result = await place_market_order(
            FailingClient(TransportError("connection lost")),
            FineTickPM(), "token", "BUY", 10,
            reference_price=0.50, max_slippage_pct=2,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.submission_uncertain)

    async def test_buy_quote_above_wallet_max_price_is_rejected(self):
        client = FakeClient()

        result = await place_market_order(
            client,
            FakePM(),
            "token",
            "BUY",
            10,
            reference_price=0.98,
            max_slippage_pct=2,
            min_price=0.10,
            max_price=0.98,
        )

        self.assertFalse(result.ok)
        self.assertIn("price_out_of_band", result.reason)
        self.assertEqual(0, client.calls)

    async def test_buy_submission_carries_exchange_enforced_tick_aligned_cap(self):
        client = CapturingClient()
        result = await place_market_order(
            client, FineTickPM(), "token", "BUY", 10,
            reference_price=0.503, max_slippage_pct=0,
            min_price=0.10, max_price=0.90,
        )

        self.assertTrue(result.ok)
        self.assertEqual("FOK", client.kwargs["order_type"])
        self.assertEqual(Decimal("0.500"), client.kwargs["max_price"])
        self.assertEqual(Decimal("10"), client.kwargs["amount"])

    async def test_sell_submission_carries_selected_tick_aligned_slippage_floor(self):
        client = CapturingClient()
        result = await place_market_order(
            client, FineTickPM(), "token", "SELL", 10,
            reference_price=0.503, max_slippage_pct=5,
        )

        self.assertTrue(result.ok)
        self.assertEqual("FOK", client.kwargs["order_type"])
        self.assertEqual(Decimal("0.480"), client.kwargs["min_price"])
        self.assertEqual(10, client.kwargs["shares"])


if __name__ == "__main__":
    unittest.main()
