"""An offline CLOB for real polymarket-client orders.

`client()` builds the pinned SDK's own AsyncSecureClient (or a subclass, such
as execution.TradingClient) for a deposit wallet, the way production does, with
a throwaway key and every HTTP request answered by an `Exchange`. The SDK
prices, signs and serializes each order itself, so a test reads exactly what
would reach POST /order.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from fractions import Fraction
from unittest import mock

import httpx
from eth_account import Account
from polymarket import AsyncSecureClient
from polymarket._internal.environment import get_environment_config
from polymarket._internal.wallet import derive_beacon_deposit_wallet_address
from polymarket.environments import PRODUCTION
from polymarket.models.clob.api_key import ApiKeyCreds

TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
CONDITION = "0x" + "ab" * 32
# The CLOB's answer to a market BUY off its grid, as production saw it on
# 2026-10-22 (also github.com/Polymarket/rs-clob-client/issues/261, a 400).
INVALID_AMOUNTS = ("invalid amounts, the market buy orders maker amount supports a max "
                   "accuracy of 2 decimals, taker amount a max of 4 decimals")
# Its answer to a FOK it cannot fill whole (a 400; the SDK raises it).
FOK_KILLED = "order couldn't be fully filled. FOK orders are fully filled or killed."
# Its answer to a BUY whose spend plus fee estimate is more than the balance:
# the 2026-10-22 production text up to the figures it goes on with.
NOT_ENOUGH_BALANCE = ("not enough balance / allowance: the balance is not enough to cover "
                      "the fee estimate")


def units(amount: str) -> Decimal:
    """A 6-decimal base-unit amount from POST /order as a Decimal."""
    return Decimal(amount) / Decimal(10**6)


class Exchange:
    """Answers the CLOB requests a market order makes. POST /order refuses a
    market BUY whose maker amount has more than 2 decimals or whose taker
    amount has more than 4, with the exchange's own 400, and keeps each order
    it receives.

    Given `asks` ((price, shares) levels, best first) it matches a market BUY
    the way the CLOB does: the order's price is makerAmount / takerAmount, it
    takes only asks at or below that price, and it spends the whole maker
    amount at the resting prices. A FOK those asks cannot fill whole is killed
    with the exchange's 400. Without `asks` every other order fills whole at
    its own amounts.

    Given a pUSD `balance`, it refuses a market BUY whose spend plus fee
    estimate is more than that, with the exchange's 400 (NOT_ENOUGH_BALANCE).
    The estimate is shares x r x (p(1 - p))^e at the order's price p, for the
    market's taker fee `fee` = (r, e), served as /clob-markets' "fd", plus the
    spend x the builder's taker rate, `builder_fee_bps`, served at
    /fees/builder-fees/<code>. Paths starting with one in `unavailable` answer
    503."""

    def __init__(self, tick: str = "0.001", asks: tuple[tuple[str, str], ...] = (), *,
                 fee: tuple[str, int] | None = None, balance: str | None = None,
                 builder_fee_bps: int | None = None, unavailable: tuple[str, ...] = ()):
        self.tick = tick
        self.asks = [(Fraction(price), Fraction(shares)) for price, shares in asks]
        self.fee = None if fee is None else (Fraction(fee[0]), int(fee[1]))
        self.balance = None if balance is None else int(Decimal(balance) * 10**6)
        self.builder_fee_bps = builder_fee_bps
        self.unavailable = unavailable
        self.orders: list[dict] = []

    def fee_estimate(self, maker: int, taker: int) -> int:
        """A market BUY's fee estimate in pUSD base units."""
        fee = Fraction(0)
        if self.fee is not None:
            rate, exponent = self.fee
            price = Fraction(maker, taker)
            fee += taker * rate * (price * (1 - price)) ** exponent
        if self.builder_fee_bps:
            fee += maker * Fraction(self.builder_fee_bps, 10_000)
        return math.ceil(fee)

    def match_buy(self, maker: int, taker: int) -> tuple[int, int]:
        """(pUSD spent, shares bought) in base units for a market BUY."""
        limit, left, bought = Fraction(maker, taker), Fraction(maker), Fraction(0)
        for price, shares in self.asks:
            if price > limit or not left:
                break
            take = min(shares * 10**6, left / price)
            bought += take
            left -= take * price
        return maker - math.ceil(left), math.floor(bought)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(self.unavailable):
            return httpx.Response(503, json={"error": "service unavailable"})
        if path == f"/markets-by-token/{TOKEN}":
            return httpx.Response(200, json={"condition_id": CONDITION})
        if path == f"/clob-markets/{CONDITION}":
            market = {"nr": False, "t": [{"t": TOKEN}], "mts": self.tick}
            if self.fee is not None:
                market["fd"] = {"r": float(self.fee[0]), "e": self.fee[1], "to": True}
            return httpx.Response(200, json=market)
        if path.startswith("/fees/builder-fees/") and self.builder_fee_bps is not None:
            return httpx.Response(200, json={"builder_maker_fee_rate_bps": 0,
                                             "builder_taker_fee_rate_bps": self.builder_fee_bps})
        if path == "/order":
            sent = json.loads(request.content)
            order = sent["order"]
            self.orders.append(order)
            maker, taker = int(order["makerAmount"]), int(order["takerAmount"])
            market_buy = sent["orderType"] in ("FOK", "FAK") and order["side"] == "BUY"
            if market_buy and (maker % 10_000 or taker % 100):
                return httpx.Response(400, json={"error": INVALID_AMOUNTS})
            if market_buy and self.balance is not None:
                fee = self.fee_estimate(maker, taker)
                if maker + fee > self.balance:
                    return httpx.Response(400, json={"error": (
                        f"{NOT_ENOUGH_BALANCE} -> balance: {self.balance}, "
                        f"order amount: {maker}, fee: {fee}")})
            making, taking = maker, taker
            if market_buy and self.asks:
                making, taking = self.match_buy(maker, taker)
                if not taking or (sent["orderType"] == "FOK" and making < maker):
                    return httpx.Response(400, json={"error": FOK_KILLED})
            return httpx.Response(200, json={
                "success": True, "errorMsg": "", "orderID": f"0x{len(self.orders):064x}",
                "status": "matched", "makingAmount": str(units(str(making))),
                "takingAmount": str(units(str(taking))),
                "tradeIDs": [f"trade-{len(self.orders)}"], "transactionsHashes": []})
        return httpx.Response(404, json={"error": f"unexpected {request.method} {path}"})


def client(exchange: Exchange, cls: type = AsyncSecureClient):
    """A `cls` client for a deposit wallet (POLY_1271 signatures, as in
    production), every host answered by `exchange`. The key is a throwaway."""
    signer = Account.from_key("0x" + "11" * 32)
    config = get_environment_config(PRODUCTION)
    deposit_wallet = derive_beacon_deposit_wallet_address(signer.address,
                                                          config.wallet_derivation)
    real = httpx.AsyncClient

    def offline(*args, **kwargs):
        return real(*args, **{**kwargs, "transport": httpx.MockTransport(exchange)})

    with mock.patch("httpx.AsyncClient", offline):
        return cls._construct_for_wallet(
            signer=signer, wallet=deposit_wallet, environment=PRODUCTION, config=config,
            credentials=ApiKeyCreds(apiKey="key", secret="c2VjcmV0", passphrase="pass"),
            api_key=None, logger=None)
