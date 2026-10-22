from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.core import execution, wallet
from backend.core.execution import place_market_order
from backend.core.polymarket import Level, OrderBook
from polymarket import AsyncSecureClient
from polymarket._internal.actions.orders.market import _compute_market_order_amounts
from polymarket.errors import InsufficientLiquidityError, TransportError
from tests.fixtures import clob


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


class RejectedClient:
    async def place_market_order(self, **kwargs):
        return SimpleNamespace(
            ok=False, code="fok_killed", message="reported unfilled",
            model_dump=lambda: {"ok": False, "code": "fok_killed"},
        )


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
    async def test_sdk_liquidity_exception_after_submit_is_uncertain(self):
        result = await place_market_order(
            FailingClient(InsufficientLiquidityError(
                "order couldn't be fully filled. FOK orders are fully filled or killed.")),
            FineTickPM(), "token", "BUY", 10,
            reference_price=0.50, max_slippage_pct=2,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.submission_uncertain)
        self.assertIn("insufficient_liquidity", result.reason)

    async def test_transport_failure_after_submission_remains_uncertain(self):
        result = await place_market_order(
            FailingClient(TransportError("connection lost")),
            FineTickPM(), "token", "BUY", 10,
            reference_price=0.50, max_slippage_pct=2,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.submission_uncertain)

    async def test_returned_rejection_after_submit_is_also_uncertain(self):
        result = await place_market_order(
            RejectedClient(), FineTickPM(), "token", "BUY", 10,
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


class ThousandthTickPM(FakePM):
    """A 0.001-tick book near 0.98, where the 2026-10-22 refusals happened."""

    async def get_orderbook(self, token_id):
        return OrderBook(
            token_id=token_id, condition_id=clob.CONDITION,
            bids=(Level(0.98, 1000),), asks=(Level(0.985, 1000),),
            tick_size=0.001, min_order_size=5, neg_risk=False,
            last_trade_price=0.985,
        )


class MarketBuyGridTests(unittest.IsolatedAsyncioTestCase):
    """The CLOB takes a market BUY whose maker amount (pUSD) has at most 2
    decimals and whose taker amount (shares) has at most 4. The pinned SDK
    rounds the shares to 5 decimals on a 0.001 or 0.005 tick and to 6 on a
    0.0001 or 0.0025 tick; on 2026-10-22 the exchange refused two $10.00 BUYs
    for it, and each became an uncertain fence. TradingClient signs every
    market BUY on the grid (execution.market_order_amounts)."""

    def test_every_tick_lands_on_the_grid_at_the_same_spend_and_cap(self):
        """market_order_amounts over the SDK's own BUY amounts for every tick
        it supports: on the grid, the same cents, under 0.0001 more shares, and
        a price (maker / taker) at or below the cap. Ticks of 0.1 and 0.01 come
        back unchanged."""
        # Beside the SDK's own output: a maker off the cent grid rounds down.
        self.assertEqual((10_000_000, 10_131_800),
                         execution.market_order_amounts(10_004_999, 10_131_720))
        for tick in ("0.1", "0.01", "0.005", "0.001", "0.0025", "0.0001"):
            step = Decimal(tick)
            prices = [step * k for k in range(1, int(1 / step))]
            caps = prices[::max(1, len(prices) // 60)] + prices[-5:]
            with self.subTest(tick=tick):
                for cap in caps:
                    for cents in range(100, 5001, 97):
                        sdk_maker, sdk_taker = _compute_market_order_amounts(
                            amount=Decimal(cents) / 100, price=cap, side="BUY",
                            tick_size=step, protect_price=True)
                        maker, taker = execution.market_order_amounts(sdk_maker, sdk_taker)
                        case = f"cap {cap}, ${cents / 100:.2f}"
                        self.assertEqual((0, 0), (maker % 10_000, taker % 100), case)
                        self.assertEqual(cents * 10_000, maker, case)
                        self.assertTrue(0 <= taker - sdk_taker < 100, case)
                        self.assertLessEqual(Decimal(maker) / taker, cap, case)
                        if tick in ("0.1", "0.01"):
                            self.assertEqual((sdk_maker, sdk_taker), (maker, taker), case)

    async def test_the_2026_10_22_buy_reaches_the_exchange_on_the_grid(self):
        """$10.00 on a 0.001 tick capped at 0.987, end to end through the
        pinned SDK. Signed by the SDK's own client it asks for 10.13172 shares
        and the exchange refuses it, which is now a definite failure. Signed by
        TradingClient it asks for 10.1318 shares, spends the whole $10.00 at a
        price under the cap, and fills."""
        for cls, taker, fills in ((AsyncSecureClient, "10131720", False),
                                  (execution.TradingClient, "10131800", True)):
            with self.subTest(cls.__name__):
                exchange = clob.Exchange(tick="0.001")
                async with clob.client(exchange, cls) as sdk:
                    result = await place_market_order(
                        sdk, ThousandthTickPM(), clob.TOKEN, "BUY", 10.0,
                        reference_price=0.98, max_slippage_pct=1,
                        min_price=0.10, max_price=0.987)
                [order] = exchange.orders
                self.assertEqual(("BUY", "10000000", taker),
                                 (order["side"], order["makerAmount"], order["takerAmount"]))
                self.assertEqual(0.987, result.limit_price)
                self.assertEqual(fills, result.ok)
                self.assertFalse(result.submission_uncertain)
                if fills:
                    self.assertAlmostEqual(10.1318, result.filled_shares)
                    self.assertLessEqual(clob.units(order["makerAmount"])
                                         / clob.units(order["takerAmount"]), Decimal("0.987"))
                else:
                    self.assertEqual("invalid_amounts", result.exchange_rejection)

    async def test_every_tick_reaches_the_exchange_on_the_grid(self):
        """A real BUY through TradingClient on each tick the SDK supports, at
        caps where its own rounding leaves the grid."""
        for tick, cap in (("0.005", "0.985"), ("0.001", "0.999"), ("0.001", "0.013"),
                          ("0.0025", "0.9875"), ("0.0001", "0.9871"), ("0.01", "0.97"),
                          ("0.1", "0.7")):
            with self.subTest(tick=tick, cap=cap):
                exchange = clob.Exchange(tick=tick)
                async with clob.client(exchange, execution.TradingClient) as sdk:
                    response = await sdk.place_market_order(
                        token_id=clob.TOKEN, side="BUY", amount=Decimal("7.31"),
                        max_price=Decimal(cap), order_type="FOK")
                [order] = exchange.orders
                self.assertTrue(response.ok, order)
                self.assertEqual("7310000", order["makerAmount"])
                self.assertEqual(0, int(order["takerAmount"]) % 100)
                self.assertLessEqual(clob.units(order["makerAmount"])
                                     / clob.units(order["takerAmount"]), Decimal(cap))

    async def test_limit_orders_sign_the_sdks_own_amounts(self):
        """Only market orders move: a limit BUY and a limit SELL sign the same
        amounts through TradingClient as through the SDK's own client, their
        5 decimals of pUSD on a 0.001 tick included."""
        signed = {}
        for cls in (AsyncSecureClient, execution.TradingClient):
            async with clob.client(clob.Exchange(tick="0.001"), cls) as sdk:
                orders = [await sdk.create_limit_order(
                    token_id=clob.TOKEN, price=Decimal("0.973"), size=Decimal("10.37"),
                    side=side) for side in ("BUY", "SELL")]
            signed[cls] = [(o.order_type, o.maker_amount, o.taker_amount) for o in orders]
        self.assertEqual(signed[AsyncSecureClient], signed[execution.TradingClient])
        [(_, buy_pusd, _), (_, _, sell_pusd)] = signed[execution.TradingClient]
        self.assertEqual(("10090010", "10090010"), (str(buy_pusd), str(sell_pusd)))

    async def test_every_client_is_built_as_a_trading_client(self):
        create = AsyncMock(return_value="client")
        with patch.object(execution.TradingClient, "create", create):
            built = await wallet.make_clob_client("0x" + "11" * 32, funder="0x" + "2" * 40)
        self.assertEqual("client", built)
        create.assert_awaited_once()


class MarketSellGridTests(unittest.IsolatedAsyncioTestCase):
    """A market SELL offers shares (maker) for pUSD (taker). The pinned SDK
    floors the shares to 2 decimals and asks for shares x min_price pUSD: 5
    decimals on a 0.001 or 0.005 tick, 6 on a 0.0025 or 0.0001 tick. No
    exchange refusal of a SELL for that is on record. TradingClient signs
    every market SELL on the same 2/4-decimal grid as a BUY, its pUSD rounded
    up to 0.0001, so a close stays within the rule that refused the
    2026-10-22 BUYs."""

    def test_every_tick_lands_on_the_grid_at_the_same_shares_and_floor(self):
        """market_order_amounts over the SDK's own SELL amounts for every tick
        it supports: on the grid, the same shares, under 0.0001 more pUSD, and
        a price (taker / maker) at or above the floor and under the next tick,
        so a bid one tick up stays in reach. Ticks of 0.1 and 0.01 come back
        unchanged."""
        for tick in ("0.1", "0.01", "0.005", "0.001", "0.0025", "0.0001"):
            step = Decimal(tick)
            prices = [step * k for k in range(1, int(1 / step))]
            floors = prices[::max(1, len(prices) // 60)] + prices[-5:]
            with self.subTest(tick=tick):
                for floor in floors:
                    for hundredths in range(500, 50001, 997):
                        shares = Decimal(hundredths) / 100
                        sdk_maker, sdk_taker = _compute_market_order_amounts(
                            amount=shares, price=floor, side="SELL",
                            tick_size=step, protect_price=True)
                        maker, taker = execution.market_order_amounts(sdk_maker, sdk_taker)
                        case = f"floor {floor}, {shares} shares"
                        self.assertEqual((0, 0), (maker % 10_000, taker % 100), case)
                        self.assertEqual(hundredths * 10_000, maker, case)
                        self.assertTrue(0 <= taker - sdk_taker < 100, case)
                        self.assertGreaterEqual(Decimal(taker) / maker, floor, case)
                        self.assertLess(Decimal(taker) / maker, floor + step, case)
                        if tick in ("0.1", "0.01"):
                            self.assertEqual((sdk_maker, sdk_taker), (maker, taker), case)

    async def test_a_fine_tick_close_reaches_the_exchange_on_the_grid(self):
        """10.37 shares on a 0.001 tick, floored at 0.971 (1% under a 0.98
        reference), end to end through the pinned SDK. Signed by the SDK's own
        client it asks for 10.06927 pUSD. Signed by TradingClient it asks for
        10.0693, sells every share and keeps the floor."""
        for cls, taker in ((AsyncSecureClient, "10069270"),
                           (execution.TradingClient, "10069300")):
            with self.subTest(cls.__name__):
                exchange = clob.Exchange(tick="0.001")
                async with clob.client(exchange, cls) as sdk:
                    result = await place_market_order(
                        sdk, ThousandthTickPM(), clob.TOKEN, "SELL", 10.37,
                        reference_price=0.98, max_slippage_pct=1)
                [order] = exchange.orders
                self.assertEqual(("SELL", "10370000", taker),
                                 (order["side"], order["makerAmount"], order["takerAmount"]))
                self.assertEqual(0.971, result.limit_price)
                self.assertTrue(result.ok)
                self.assertAlmostEqual(10.37, result.filled_shares)
                self.assertGreaterEqual(clob.units(order["takerAmount"])
                                        / clob.units(order["makerAmount"]), Decimal("0.971"))

    async def test_every_tick_reaches_the_exchange_on_the_grid(self):
        """A real SELL through TradingClient on each tick the SDK supports, at
        floors where its own pUSD leaves the grid."""
        for tick, floor in (("0.005", "0.985"), ("0.001", "0.973"), ("0.001", "0.013"),
                            ("0.0025", "0.9875"), ("0.0001", "0.9871"), ("0.01", "0.97"),
                            ("0.1", "0.7")):
            with self.subTest(tick=tick, floor=floor):
                exchange = clob.Exchange(tick=tick)
                async with clob.client(exchange, execution.TradingClient) as sdk:
                    response = await sdk.place_market_order(
                        token_id=clob.TOKEN, side="SELL", shares=Decimal("10.37"),
                        min_price=Decimal(floor), order_type="FOK")
                [order] = exchange.orders
                self.assertTrue(response.ok, order)
                self.assertEqual("10370000", order["makerAmount"])
                self.assertEqual(0, int(order["takerAmount"]) % 100)
                self.assertGreaterEqual(clob.units(order["takerAmount"])
                                        / clob.units(order["makerAmount"]), Decimal(floor))


if __name__ == "__main__":
    unittest.main()
