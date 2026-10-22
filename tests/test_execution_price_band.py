from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.core import execution, wallet
from backend.core.copy_engine import MIN_NOTIONAL_USD
from backend.core.execution import place_market_order
from backend.core.polymarket import Level, OrderBook
from polymarket import AsyncSecureClient
from polymarket._internal.actions.orders.market import _compute_market_order_amounts
from polymarket.clients import async_secure
from polymarket.errors import InsufficientLiquidityError, RequestRejectedError, TransportError
from tests.fixtures import clob

# The finest tick the CLOB lists: the next ask above a cap is at least this far up.
FINEST_TICK = Fraction(1, 10_000)


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


class LeaderPricePM(FakePM):
    """A 0.01-tick book whose best ask is 0.30, the price a leader just paid:
    with 2% slippage the copy's cap is 0.30 as well."""

    async def get_orderbook(self, token_id):
        return OrderBook(
            token_id=token_id, condition_id=clob.CONDITION,
            bids=(Level(0.29, 1000),), asks=(Level(0.30, 1000),),
            tick_size=0.01, min_order_size=5, neg_risk=False,
            last_trade_price=0.30,
        )


def signed_price(order: dict) -> Fraction:
    """The price the exchange reads off a signed BUY: maker / taker."""
    return Fraction(int(order["makerAmount"]), int(order["takerAmount"]))


class MarketBuyGridTests(unittest.IsolatedAsyncioTestCase):
    """The CLOB takes a market BUY whose maker amount (pUSD) has at most 2
    decimals and whose taker amount (shares) has at most 4. The pinned SDK
    rounds the shares to 5 decimals on a 0.001 or 0.005 tick and to 6 on a
    0.0001 or 0.0025 tick; on 2026-10-22 the exchange refused two $10.00 BUYs
    for it, and each became an uncertain fence. It also rounds them up, which
    prices a capped BUY (maker / taker) a hair under its cap, so an ask resting
    exactly at the cap cannot be lifted (github.com/Polymarket/py-sdk/issues/291).
    TradingClient signs every market BUY on the grid with spend / cap shares
    rounded down (execution.market_buy_amounts): at or just above the cap and
    under cap + 0.0001."""

    def test_every_tick_signs_spend_over_cap_rounded_down(self):
        """market_buy_amounts over the SDK's own amounts for every tick it
        supports: on the grid, the same cents, spend / cap shares rounded down
        to 0.0001 (at most what the SDK asks for), and a price from the cap up
        to under cap + 0.0001."""
        # Beside the SDK's own output: a maker off the cent grid rounds down.
        self.assertEqual((10_000_000, 10_131_700),
                         execution.market_buy_amounts(10_004_999, 10_131_720, Decimal("0.987")))
        # py-sdk#291's example on a 0.01 tick: $1.00 at 0.07. The SDK asks for
        # 14.2858 shares, a price of 0.069999; upstream's fix asks for 14.2857.
        sdk = _compute_market_order_amounts(amount=Decimal("1"), price=Decimal("0.07"),
                                            side="BUY", tick_size=Decimal("0.01"),
                                            protect_price=True)
        self.assertEqual((1_000_000, 14_285_800), sdk)
        self.assertEqual((1_000_000, 14_285_700),
                         execution.market_buy_amounts(*sdk, Decimal("0.07")))
        # A spend that divides exactly keeps the SDK's amounts: $9.90 at 0.99.
        self.assertEqual((9_900_000, 10_000_000),
                         execution.market_buy_amounts(9_900_000, 10_000_000, Decimal("0.99")))
        for tick in ("0.1", "0.01", "0.005", "0.001", "0.0025", "0.0001"):
            step = Decimal(tick)
            prices = [step * k for k in range(1, int(1 / step))]
            caps = prices[::max(1, len(prices) // 60)] + prices[-5:]
            with self.subTest(tick=tick):
                for cap in caps:
                    exact = Fraction(cap)
                    for cents in range(100, 5001, 97):
                        sdk_maker, sdk_taker = _compute_market_order_amounts(
                            amount=Decimal(cents) / 100, price=cap, side="BUY",
                            tick_size=step, protect_price=True)
                        maker, taker = execution.market_buy_amounts(sdk_maker, sdk_taker, cap)
                        case = f"cap {cap}, ${cents / 100:.2f}"
                        self.assertEqual((0, 0), (maker % 10_000, taker % 100), case)
                        self.assertEqual(cents * 10_000, maker, case)
                        self.assertEqual(maker / exact // 100 * 100, taker, case)
                        self.assertLessEqual(taker, sdk_taker, case)
                        self.assertTrue(exact <= Fraction(maker, taker) < exact + FINEST_TICK,
                                        case)

    def test_no_spend_from_the_minimum_reaches_the_next_finest_tick(self):
        """Rounding down drops under 0.0001 shares, which keeps spend / shares
        under cap + 0.0001, the next price on the finest grid the CLOB lists,
        for any spend of at least cap^2 + 0.0001 x cap. The engine's smallest
        BUY ($1, MIN_NOTIONAL_USD) is the tightest case: checked at every cap on
        that grid, with a spread of larger spends at every 97th."""
        smallest = round(MIN_NOTIONAL_USD * 100)
        spends = [smallest, *range(smallest + 1, 50_001, 1_237)]
        for k in range(1, 10_000):
            cap = Decimal(k) / 10_000
            for cents in (spends if k % 97 == 0 else spends[:1]):
                maker, taker = execution.market_buy_amounts(cents * 10_000, 0, cap)
                self.assertTrue(Fraction(cap) <= Fraction(maker, taker)
                                < Fraction(cap) + FINEST_TICK, (str(cap), cents))

    def test_a_spend_that_could_reach_the_next_tick_rounds_up(self):
        """Below $1 rounding down can overshoot. 1 cent at 0.97 would ask for
        0.0103 shares, a price of 0.970874, past 0.9701; at 0.4999 it would ask
        for 0.02, a price of exactly 0.5000, the next finest tick itself. Those
        shares round up, and the order prices under its cap."""
        for cap, down, up in (("0.97", 10_300, 10_400), ("0.4999", 20_000, 20_100),
                              ("0.1032", 96_800, 96_900)):
            with self.subTest(cap=cap):
                self.assertGreaterEqual(Fraction(10_000, down), Fraction(cap) + FINEST_TICK)
                maker, taker = execution.market_buy_amounts(10_000, 0, Decimal(cap))
                self.assertEqual((10_000, up), (maker, taker))
                self.assertLessEqual(Fraction(maker, taker), Fraction(cap))

    def test_an_unprotected_buy_rounds_the_sdks_shares_down(self):
        """Without a cap the SDK prices the BUY off the book and rounds its
        shares down; on the grid they round down again, to 0.0001."""
        self.assertEqual((10_000_000, 10_131_700),
                         execution.market_buy_amounts(10_000_000, 10_131_729, None))

    async def test_the_2026_10_22_buy_reaches_the_exchange_on_the_grid(self):
        """$10.00 on a 0.001 tick capped at 0.987, end to end through the
        pinned SDK, with asks at 0.985. Signed by the SDK's own client it asks
        for 10.13172 shares and the exchange refuses it, which is now a definite
        failure. Signed by TradingClient it asks for 10.1317 shares, a price a
        hair above the cap, and buys at 0.985 for the whole $10.00."""
        for cls, taker, fills in ((AsyncSecureClient, "10131720", False),
                                  (execution.TradingClient, "10131700", True)):
            with self.subTest(cls.__name__):
                exchange = clob.Exchange(tick="0.001", asks=(("0.985", "1000"),))
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
                    self.assertTrue(Fraction("0.987") <= signed_price(order)
                                    < Fraction("0.9871"))
                    self.assertAlmostEqual(10.152284, result.filled_shares)
                    self.assertAlmostEqual(0.985, result.avg_price, places=6)
                else:
                    self.assertEqual("invalid_amounts", result.exchange_rejection)

    async def test_an_ask_resting_at_the_cap_is_lifted(self):
        """A leader paid 0.30 on a 0.01 tick and asks remain at 0.30; with 2%
        slippage the copy's cap is 0.30 too, as it is for every price from
        0.10 to 0.49 on that tick. Signed by the SDK's own client, $10.00 asks
        for 33.3334 shares, a price of 0.2999994, and the exchange kills the
        FOK. Signed by TradingClient it asks for 33.3333, a price of 0.3000003,
        and buys at 0.30 for the whole $10.00."""
        for cls, taker, fills in ((AsyncSecureClient, "33333400", False),
                                  (execution.TradingClient, "33333300", True)):
            with self.subTest(cls.__name__):
                exchange = clob.Exchange(tick="0.01", asks=(("0.30", "1000"),))
                async with clob.client(exchange, cls) as sdk:
                    result = await place_market_order(
                        sdk, LeaderPricePM(), clob.TOKEN, "BUY", 10.0,
                        reference_price=0.30, max_slippage_pct=2,
                        min_price=0.10, max_price=0.98)
                [order] = exchange.orders
                self.assertEqual(("10000000", taker), (order["makerAmount"], order["takerAmount"]))
                self.assertEqual(0.30, result.limit_price)
                self.assertEqual(fills, result.ok)
                self.assertFalse(result.submission_uncertain)
                if fills:
                    self.assertAlmostEqual(33.333333, result.filled_shares)
                    self.assertAlmostEqual(0.30, result.avg_price)
                else:
                    self.assertEqual("fok_not_filled", result.exchange_rejection)

    async def test_every_tick_reaches_the_exchange_on_the_grid(self):
        """A real BUY through TradingClient on each tick the SDK supports, at
        caps where its own rounding leaves the grid or under the cap."""
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
                self.assertTrue(Fraction(cap) <= signed_price(order)
                                < Fraction(cap) + FINEST_TICK)

    async def test_an_ask_at_the_cap_fills_and_one_above_it_is_out_of_reach(self):
        """On every tick: an ask exactly at the cap fills at the cap. An ask one
        finest tick (0.0001) above it, which a market whose tick turned finer
        could hold, is out of the order's reach, and the exchange kills the FOK
        with its 400."""
        for tick, cap in (("0.1", "0.3"), ("0.01", "0.07"), ("0.01", "0.98"),
                          ("0.005", "0.985"), ("0.001", "0.013"), ("0.001", "0.999"),
                          ("0.0025", "0.9875"), ("0.0001", "0.9871"), ("0.0001", "0.9999")):
            above = str(Decimal(cap) + Decimal("0.0001"))
            for ask in (cap, above):
                with self.subTest(tick=tick, cap=cap, ask=ask):
                    exchange = clob.Exchange(tick=tick, asks=((ask, "100000"),))
                    async with clob.client(exchange, execution.TradingClient) as sdk:
                        buy = sdk.place_market_order(
                            token_id=clob.TOKEN, side="BUY", amount=Decimal("7.31"),
                            max_price=Decimal(cap), order_type="FOK")
                        if ask == cap:
                            response = await buy
                        else:
                            with self.assertRaises(RequestRejectedError) as killed:
                                await buy
                    self.assertEqual(1, len(exchange.orders))
                    if ask == cap:
                        self.assertTrue(response.ok)
                        self.assertAlmostEqual(
                            float(cap), float(response.making_amount / response.taking_amount),
                            places=6)
                    else:
                        self.assertEqual(400, killed.exception.status)
                        self.assertIn("FOK orders are fully filled or killed",
                                      str(killed.exception))

    async def test_concurrent_buys_each_sign_against_their_own_cap(self):
        """The cap reaches _sign_order in a context variable, which belongs to
        one task. Two BUYs in flight on one client, the first held between
        taking its cap and signing until the second has signed, each ask for
        spend / their own cap."""
        prepare = async_secure.prepare_market_order_draft
        second_signed = asyncio.Event()

        async def held(ctx, params):
            if params.max_price == Decimal("0.30"):
                await second_signed.wait()
            return await prepare(ctx, params)

        exchange = clob.Exchange(tick="0.01")
        async with clob.client(exchange, execution.TradingClient) as sdk:
            async def buy(cap: str):
                await sdk.place_market_order(
                    token_id=clob.TOKEN, side="BUY", amount=Decimal("10"),
                    max_price=Decimal(cap), order_type="FOK")
                second_signed.set()

            with patch.object(async_secure, "prepare_market_order_draft", held):
                await asyncio.gather(buy("0.30"), buy("0.70"))
        self.assertEqual(["14285700", "33333300"], [o["takerAmount"] for o in exchange.orders])

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
        """market_sell_amounts over the SDK's own SELL amounts for every tick
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
                        maker, taker = execution.market_sell_amounts(sdk_maker, sdk_taker)
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
