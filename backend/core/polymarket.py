"""Polymarket read layer — all market-data / public-wallet API calls.

Polymarket only. No other venues, no order placement (that lives in
the executor, phase 6). Every response shape here is frozen against the live
APIs in ``backend/core/API_RECON.md`` — read that for endpoints, params, and
parsing gotchas.

Two hosts (both GET, no auth for these reads):
  - data-api  : leaderboard, positions, activity, holders
  - clob      : order book

Gotchas handled here: a browser-like User-Agent (data-api 403s library UAs);
activity is mixed-type so trade history filters ``type=TRADE``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from backend.config import (
    BRIDGE_API, CLOB_API, DATA_API, HTTP_TIMEOUT, HTTP_USER_AGENT,
)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Frozen response shapes (see API_RECON.md)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Level:
    price: float  # 0.0 - 1.0
    size: float


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    condition_id: str
    bids: tuple[Level, ...]   # best (highest) first
    asks: tuple[Level, ...]   # best (lowest) first
    tick_size: float
    min_order_size: float
    neg_risk: bool
    last_trade_price: float

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def midpoint(self) -> float | None:
        if self.best_bid and self.best_ask:
            return round((self.best_bid.price + self.best_ask.price) / 2, 6)
        return None


@dataclass(frozen=True)
class Position:
    """An open position held by a public wallet (data-api/positions)."""
    proxy_wallet: str
    asset: str            # token_id
    condition_id: str
    size: float           # shares
    avg_price: float      # entry
    cur_price: float      # current
    initial_value: float
    current_value: float
    cash_pnl: float       # unrealized PnL (free from the API)
    percent_pnl: float
    realized_pnl: float
    redeemable: bool      # True => market resolved -> redeem
    mergeable: bool
    negative_risk: bool
    outcome: str          # "Yes" / "No"
    outcome_index: int    # 0 / 1
    opposite_asset: str
    title: str
    slug: str
    icon: str
    event_slug: str
    end_date: str         # date-only, e.g. "2026-07-20"

    @classmethod
    def from_api(cls, d: dict) -> "Position":
        return cls(
            proxy_wallet=str(d.get("proxyWallet", "")),
            asset=str(d.get("asset", "")),
            condition_id=str(d.get("conditionId", "")),
            size=_f(d.get("size")),
            avg_price=_f(d.get("avgPrice")),
            cur_price=_f(d.get("curPrice")),
            initial_value=_f(d.get("initialValue")),
            current_value=_f(d.get("currentValue")),
            cash_pnl=_f(d.get("cashPnl")),
            percent_pnl=_f(d.get("percentPnl")),
            realized_pnl=_f(d.get("realizedPnl")),
            redeemable=bool(d.get("redeemable", False)),
            mergeable=bool(d.get("mergeable", False)),
            negative_risk=bool(d.get("negativeRisk", False)),
            outcome=str(d.get("outcome", "")),
            outcome_index=_i(d.get("outcomeIndex")),
            opposite_asset=str(d.get("oppositeAsset", "")),
            title=str(d.get("title", "")),
            slug=str(d.get("slug", "")),
            icon=str(d.get("icon", "")),
            event_slug=str(d.get("eventSlug", "")),
            end_date=str(d.get("endDate", "")),
        )


@dataclass(frozen=True)
class LeaderEntry:
    rank: int
    proxy_wallet: str
    user_name: str
    x_username: str
    verified: bool
    vol: float
    pnl: float
    profile_image: str

    @classmethod
    def from_api(cls, d: dict) -> "LeaderEntry":
        return cls(
            rank=_i(d.get("rank")),
            proxy_wallet=str(d.get("proxyWallet", "")),
            user_name=str(d.get("userName", "")),
            x_username=str(d.get("xUsername", "")),
            verified=bool(d.get("verifiedBadge", False)),
            vol=_f(d.get("vol")),
            pnl=_f(d.get("pnl")),
            profile_image=str(d.get("profileImage", "")),
        )


@dataclass(frozen=True)
class Trade:
    proxy_wallet: str
    timestamp: int        # unix seconds
    condition_id: str
    side: str             # BUY / SELL
    asset: str            # token_id
    outcome: str
    outcome_index: int
    price: float          # 0-1
    size: float           # shares
    usd_size: float       # pUSD notional (field name `usdcSize` upstream)
    title: str
    slug: str
    tx_hash: str

    @classmethod
    def from_api(cls, d: dict) -> "Trade":
        return cls(
            proxy_wallet=str(d.get("proxyWallet", "")),
            timestamp=_i(d.get("timestamp")),
            condition_id=str(d.get("conditionId", "")),
            side=str(d.get("side", "")),
            asset=str(d.get("asset", "")),
            outcome=str(d.get("outcome", "")),
            outcome_index=_i(d.get("outcomeIndex")),
            price=_f(d.get("price")),
            size=_f(d.get("size")),
            usd_size=_f(d.get("usdcSize")),
            title=str(d.get("title", "")),
            slug=str(d.get("slug", "")),
            tx_hash=str(d.get("transactionHash", "")),
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PolymarketClient:
    """Async read client for the Polymarket public APIs.

    Owns an ``httpx.AsyncClient`` with the required User-Agent. Pass an existing
    client to share a connection pool, or let it create one. Close with
    ``await aclose()`` (or use as an async context manager).
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": HTTP_USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, url: str, params: dict | None = None) -> Any:
        # Courtesy retry on 429: the background stats crawler and the copy
        # engine share these hosts, so a burst of crawler traffic must not
        # turn into hard failures (seen live: data-api 429s at concurrency 8).
        for attempt, delay in ((0, 1.0), (1, 3.0), (2, None)):
            r = await self._client.get(url, params=params)
            if r.status_code == 429 and delay is not None:
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            return r.json()

    # --- orderbook ---------------------------------------------------------
    async def get_orderbook(self, token_id: str) -> OrderBook:
        d = await self._get(f"{CLOB_API}/book", {"token_id": token_id})
        bids = tuple(
            Level(_f(b["price"]), _f(b["size"]))
            for b in sorted(d.get("bids", []), key=lambda x: -_f(x["price"]))
        )
        asks = tuple(
            Level(_f(a["price"]), _f(a["size"]))
            for a in sorted(d.get("asks", []), key=lambda x: _f(x["price"]))
        )
        return OrderBook(
            token_id=str(d.get("asset_id", token_id)),
            condition_id=str(d.get("market", "")),
            bids=bids,
            asks=asks,
            tick_size=_f(d.get("tick_size"), 0.01),
            min_order_size=_f(d.get("min_order_size")),
            neg_risk=bool(d.get("neg_risk", False)),
            last_trade_price=_f(d.get("last_trade_price")),
        )

    # --- positions ---------------------------------------------------------
    async def get_positions(
        self,
        wallet_address: str,
        *,
        size_threshold: float = 1.0,
        limit: int = 100,
        sort_by: str = "CURRENT",
    ) -> list[Position]:
        d = await self._get(
            f"{DATA_API}/positions",
            {
                "user": wallet_address,
                "sizeThreshold": size_threshold,
                "limit": limit,
                "sortBy": sort_by,
                "sortDirection": "DESC",
            },
        )
        return [Position.from_api(p) for p in d] if isinstance(d, list) else []

    # --- leaderboard -------------------------------------------------------
    async def get_leaderboard(
        self,
        *,
        period: str = "MONTH",     # DAY | WEEK | MONTH | ALL
        order_by: str = "PNL",     # PNL | VOL
        category: str = "OVERALL",
        limit: int = 50,
        offset: int = 0,
    ) -> list[LeaderEntry]:
        d = await self._get(
            f"{DATA_API}/v1/leaderboard",
            {
                "category": category,
                "timePeriod": period,
                "orderBy": order_by,
                "limit": limit,
                "offset": offset,
            },
        )
        return [LeaderEntry.from_api(e) for e in d] if isinstance(d, list) else []

    # --- geoblock ----------------------------------------------------------
    async def get_geoblock(self) -> dict:
        """Geo-restriction check for the caller's IP (call before placing orders).
        Returns {blocked: bool, ip, country, region}."""
        return await self._get("https://polymarket.com/api/geoblock")

    # --- bridge deposit ------------------------------------------------------
    async def create_bridge_address(self, wallet_address: str) -> dict:
        """Deposit addresses across EVM/Solana/Bitcoin/Tron for funding
        `wallet_address` from any supported chain in USDC/USDT/etc. — whatever
        arrives is auto-converted into pUSD at that address by Polymarket's
        Collateral Onramp. No gas needed on our side for this step (the bridge
        itself is a Polymarket-run service); the on-chain allowance approval
        for actually trading is a separate, still gas-costing step for an EOA
        wallet (see BUILD_PLAN §wallet model).
        Returns {"address": {"evm": "0x...", "svm": "...", "btc": "...", ...}}.
        """
        r = await self._client.post(f"{BRIDGE_API}/deposit", json={"address": wallet_address})
        r.raise_for_status()
        return r.json()

    # --- trade history -----------------------------------------------------
    async def get_resolved_prices(self, condition_id: str) -> dict[str, float]:
        """token_id -> resolved price (1.0 winner / 0.0 loser) for a RESOLVED
        market, from the CLOB's own /markets/{condition_id} (serves resolved
        markets with per-token winner flags; verified live 2026-07-03 — note
        Gamma's ?condition_ids= filter does NOT index sports markets, so it
        can't be used for this). Empty dict when the market is unknown or no
        winner is flagged yet (not resolved)."""
        d = await self._get(f"{CLOB_API}/markets/{condition_id}")
        tokens = d.get("tokens", []) if isinstance(d, dict) else []
        if not any(t.get("winner") for t in tokens):
            return {}
        return {str(t.get("token_id")): (1.0 if t.get("winner") else 0.0)
                for t in tokens}

    async def get_redeems(self, wallet_address: str, limit: int = 50) -> list[dict]:
        """REDEEM activity — how much a wallet was paid when resolved positions
        were redeemed (winners redeem $1/share; losers produce no payout).
        Used to finalize copy positions whose market died before we could exit."""
        d = await self._get(
            f"{DATA_API}/activity",
            {"user": wallet_address, "type": "REDEEM", "limit": limit},
        )
        return d if isinstance(d, list) else []

    async def get_trade_history(
        self, wallet_address: str, limit: int = 100, offset: int = 0
    ) -> list[Trade]:
        """Most-recent-first TRADE activity. The endpoint accepts limit up to
        at least 1000 and offset pagination (both verified live 2026-07-02)."""
        d = await self._get(
            f"{DATA_API}/activity",
            {"user": wallet_address, "type": "TRADE", "limit": limit, "offset": offset},
        )
        return [Trade.from_api(t) for t in d] if isinstance(d, list) else []
