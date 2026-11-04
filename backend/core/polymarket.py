"""Polymarket read layer — all market-data / public-wallet API calls.

Polymarket only. No other venues, no order placement (that lives in
execution.py). Every response shape here is frozen against the live APIs;
the "Frozen response shapes" section below records the fields each parser
relies on.

Hosts (no auth for any of these):
  - data-api       : leaderboard, positions, activity
  - clob           : order book, resolved markets
  - polymarket.com : frontend geoblock probe
  - bridge         : deposit-address creation (the one POST)

Gotchas handled here: a browser-like User-Agent (data-api 403s library UAs);
activity is mixed-type so trade history filters ``type=TRADE``.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from backend.config import (
    BRIDGE_API, CLOB_API, DATA_API, HTTP_TIMEOUT, HTTP_USER_AGENT,
)
from backend.core.health import upstream

log = logging.getLogger("polymarket")

# Read-retry policy for the public GET endpoints.
#
# Two upstream behaviours drive this, both measured in production (2026-09-02):
#
#  1. data-api answers 429 under our own load. The old policy was two fixed
#     sleeps (1s, 3s) with no jitter, so every worker that hit the limit
#     retried in lockstep and re-collided. Full jitter spreads them out.
#  2. The edge sends a clean HTTP/2 GOAWAY once a connection has carried
#     10,000 streams (`ConnectionTerminated error_code:0,
#     last_stream_id:19999` — the same id every time, 7 occurrences in 3.4
#     days). httpx surfaces that as RemoteProtocolError rather than retrying
#     on a fresh connection, so an in-flight read fails for a reason that has
#     nothing to do with the request.
#
# Only idempotent GETs go through _get; the bridge POST does not retry.
READ_MAX_ATTEMPTS = 4
READ_BACKOFF_BASE = 0.5      # seconds
READ_BACKOFF_CAP = 8.0
RETRY_AFTER_CAP = 30.0
# Transport-level failures: the request provably did not produce a response,
# so re-issuing a GET is safe.
RETRYABLE_TRANSPORT = (
    httpx.RemoteProtocolError,   # GOAWAY / connection terminated mid-stream
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
)
# Gateway-class statuses only. A 500 from data-api is deterministic per wallet
# (two addresses produced 381 of 388 failures over 3.4 days), so retrying it
# just multiplies load — trader_stats quarantines those instead.
RETRYABLE_STATUS = (502, 503, 504)

# Frontend geoblock probe (see PolymarketClient.get_geoblock).
GEOBLOCK_CACHE_SECONDS = 600.0
GEOBLOCK_FAILURE_CACHE_SECONDS = 60.0
GEOBLOCK_TIMEOUT_SECONDS = 3.0


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Honour Retry-After when the server sets it (delta-seconds form)."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw.strip()), RETRY_AFTER_CAP))
    except (TypeError, ValueError):
        return None


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter (AWS's formulation)."""
    ceiling = min(READ_BACKOFF_CAP, READ_BACKOFF_BASE * (2 ** attempt))
    return random.uniform(0.0, ceiling)



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


def _field(d: dict, v2: str, v1: str, default: Any = None) -> Any:
    """A data-api field by its v2 (snake_case) name, falling back to its v1
    (camelCase) name. Data API v1 was retired on 2026-11-02; its names stay
    readable for fixtures and rows recorded before the move."""
    value = d.get(v2)
    return d.get(v1, default) if value is None else value


# v1 spellings callers still pass, mapped to their v2 names.
_POSITION_SORTS = {"CURRENT": "CURRENT_VALUE"}
_LEADERBOARD_SORTS = {"VOL": "VOLUME"}
# v2 activity fields under the v1 names the raw-row readers use
# (trader_stats.realized_closings, copy_engine's redeem settlement).
_ACTIVITY_V1_NAMES = {
    "proxy_wallet": "proxyWallet", "condition_id": "conditionId", "token_id": "asset",
    "usdc_size": "usdcSize", "transaction_hash": "transactionHash",
    "outcome_index": "outcomeIndex", "event_slug": "eventSlug",
}


def _with_v1_names(row: dict) -> dict:
    """A raw v2 activity row that also answers to its v1 field names."""
    return {**row, **{v1: row[v2] for v2, v1 in _ACTIVITY_V1_NAMES.items() if v2 in row}}


# ---------------------------------------------------------------------------
# Frozen response shapes
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
            proxy_wallet=str(_field(d, "proxy_wallet", "proxyWallet", "")),
            asset=str(_field(d, "token_id", "asset", "")),
            condition_id=str(_field(d, "condition_id", "conditionId", "")),
            size=_f(_field(d, "current_size", "size")),
            avg_price=_f(_field(d, "avg_price", "avgPrice")),
            cur_price=_f(_field(d, "current_price", "curPrice")),
            initial_value=_f(_field(d, "entry_cost_usdc", "initialValue")),
            current_value=_f(_field(d, "current_value", "currentValue")),
            cash_pnl=_f(_field(d, "unrealized_pnl", "cashPnl")),
            percent_pnl=_f(_field(d, "percent_pnl", "percentPnl")),
            realized_pnl=_f(_field(d, "realized_pnl", "realizedPnl")),
            redeemable=bool(d.get("redeemable", False)),
            mergeable=bool(d.get("mergeable", False)),
            negative_risk=bool(_field(d, "negative_risk", "negativeRisk", False)),
            outcome=str(d.get("outcome", "")),
            outcome_index=_i(_field(d, "outcome_index", "outcomeIndex")),
            opposite_asset=str(_field(d, "opposite_token_id", "oppositeAsset", "")),
            title=str(d.get("title", "")),
            slug=str(d.get("slug", "")),
            icon=str(d.get("icon", "")),
            event_slug=str(_field(d, "event_slug", "eventSlug", "")),
            end_date=str(_field(d, "end_date", "endDate", "")),
        )


@dataclass(frozen=True)
class LeaderEntry:
    rank: int
    proxy_wallet: str
    user_name: str
    x_username: str
    verified: bool
    vol: float            # traded volume in SHARES on v2 boards; USD: get_user_volume
    pnl: float
    profile_image: str

    @classmethod
    def from_api(cls, d: dict) -> "LeaderEntry":
        # A v2 board row carries `rank`; a v2 single-user row carries
        # `rank_pnl`/`rank_volume`.
        return cls(
            rank=_i(d.get("rank", d.get("rank_pnl"))),
            proxy_wallet=str(_field(d, "user_id", "proxyWallet", "")),
            user_name=str(_field(d, "user_name", "userName", "")),
            x_username=str(_field(d, "x_username", "xUsername", "")),
            verified=bool(_field(d, "verified", "verifiedBadge", False)),
            vol=_f(_field(d, "volume", "vol")),
            pnl=_f(d.get("pnl")),
            profile_image=str(_field(d, "profile_image", "profileImage", "")),
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
            proxy_wallet=str(_field(d, "proxy_wallet", "proxyWallet", "")),
            timestamp=_i(d.get("timestamp")),
            condition_id=str(_field(d, "condition_id", "conditionId", "")),
            side=str(d.get("side", "")),
            asset=str(_field(d, "token_id", "asset", "")),
            outcome=str(d.get("outcome", "")),
            outcome_index=_i(_field(d, "outcome_index", "outcomeIndex")),
            price=_f(d.get("price")),
            size=_f(d.get("size")),
            usd_size=_f(_field(d, "usdc_size", "usdcSize")),
            title=str(d.get("title", "")),
            slug=str(d.get("slug", "")),
            tx_hash=str(_field(d, "transaction_hash", "transactionHash", "")),
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
        """GET with bounded retries: jittered backoff, Retry-After, and one
        class of transport failure (see RETRYABLE_TRANSPORT above).

        The last attempt always raises rather than returning None, so callers
        keep their existing `except Exception` semantics.
        """
        last_attempt = READ_MAX_ATTEMPTS - 1
        for attempt in range(READ_MAX_ATTEMPTS):
            final = attempt == last_attempt
            try:
                r = await self._client.get(url, params=params)
            except RETRYABLE_TRANSPORT as exc:
                upstream.record(transport_error=True)
                if final:
                    raise
                delay = _backoff_delay(attempt)
                upstream.record(retry=True)
                log.warning("transport failure on %s (%s: %s) — retry %d/%d in %.2fs",
                            url, type(exc).__name__, exc, attempt + 1, last_attempt, delay)
                await asyncio.sleep(delay)
                continue
            if r.status_code == 429:
                upstream.record(rate_limited=True)
            elif r.status_code >= 500:
                upstream.record(server_error=True)
            if r.status_code == 429 and not final:
                delay = _retry_after_seconds(r)
                if delay is None:
                    delay = _backoff_delay(attempt)
                upstream.record(retry=True)
                log.warning("429 from %s — retry %d/%d in %.2fs",
                            url, attempt + 1, last_attempt, delay)
                await asyncio.sleep(delay)
                continue
            if r.status_code in RETRYABLE_STATUS and not final:
                delay = _backoff_delay(attempt)
                upstream.record(retry=True)
                log.warning("%d from %s — retry %d/%d in %.2fs",
                            r.status_code, url, attempt + 1, last_attempt, delay)
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            upstream.record(ok=True)
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

    # --- data-api v2 ---------------------------------------------------------
    # Data API v1 was retired on 2026-11-02; every route now answers 410. v2
    # wraps rows in {"data", "pagination"}, pages by an opaque cursor (an
    # `offset` parameter is a 400), and names fields in snake_case.
    async def _v2_page(self, route: str, params: dict) -> tuple[Any, str | None]:
        """One /v2 page: (data, next_cursor). next_cursor is None on the last
        page and for unpaginated routes."""
        d = await self._get(f"{DATA_API}/v2/{route}", params)
        if not isinstance(d, dict):
            return None, None
        pagination = d.get("pagination")
        cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
        return d.get("data"), (cursor or None)

    # --- positions ---------------------------------------------------------
    @staticmethod
    def _positions_params(wallet_address: str, size_threshold: float, sort_by: str,
                          market: str | None) -> dict:
        # status defaults to OPEN, the superset v1 /positions served: it holds
        # settled-but-unredeemed winners and still-held losers (row status
        # REDEEMABLE, `redeemable` true), verified live 2026-11-04.
        params = {
            "user": wallet_address,
            "filter_type": "TOKENS",
            "filter_amount": size_threshold,
            "sort_by": _POSITION_SORTS.get(sort_by, sort_by),
            "sort_direction": "DESC",
        }
        if market:
            # condition filter — a targeted, truncation-proof read of one
            # market's position (v1 called it `market`)
            params["condition"] = market
        return params

    async def get_positions(
        self,
        wallet_address: str,
        *,
        size_threshold: float = 1.0,
        limit: int = 500,
        sort_by: str = "CURRENT",
        market: str | None = None,
    ) -> list[Position]:
        """The first page of the wallet's positions, up to `limit` rows."""
        params = self._positions_params(wallet_address, size_threshold, sort_by, market)
        rows, _ = await self._v2_page("positions", {**params, "limit": limit})
        return [Position.from_api(p) for p in rows] if isinstance(rows, list) else []

    async def get_all_positions(
        self,
        wallet_address: str,
        *,
        size_threshold: float = 1.0,
        page_size: int = 500,
        max_pages: int = 6,
    ) -> tuple[list[Position], bool]:
        """Every position the wallet holds, following the cursor past the
        per-page cap. Returns (positions, complete): complete=False means the
        wallet holds more than page_size*max_pages positions and the list is
        truncated — callers must NOT treat absence from a truncated list as
        proof a position was exited (whale leaders really do hold 500+ open
        positions)."""
        params = {**self._positions_params(wallet_address, size_threshold, "CURRENT", None),
                  "limit": page_size}
        out: list[Position] = []
        for _ in range(max_pages):
            rows, cursor = await self._v2_page("positions", params)
            if isinstance(rows, list):
                out.extend(Position.from_api(p) for p in rows)
            if not cursor:
                return out, True
            # The cursor binds status and sort; the anchor and filters go along.
            params = {**params, "cursor": cursor}
        return out, False

    # --- leaderboard -------------------------------------------------------
    async def get_leaderboard_page(
        self,
        *,
        period: str = "MONTH",     # DAY | WEEK | MONTH | ALL
        order_by: str = "PNL",     # PNL | VOL
        category: str = "OVERALL",
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[LeaderEntry], str | None]:
        """One leaderboard page and the cursor of the next one (None at the end)."""
        params: dict[str, Any] = {
            "category": category.lower(),
            "time_period": period.lower(),
            "sort_by": _LEADERBOARD_SORTS.get(order_by.upper(), order_by.upper()),
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        rows, next_cursor = await self._v2_page("leaderboard", params)
        entries = [LeaderEntry.from_api(e) for e in rows] if isinstance(rows, list) else []
        return entries, next_cursor

    async def get_leaderboard(self, **kwargs) -> list[LeaderEntry]:
        """The first leaderboard page (see get_leaderboard_page)."""
        entries, _ = await self.get_leaderboard_page(**kwargs)
        return entries

    async def get_leaderboard_user(self, wallet_address: str,
                                   period: str = "ALL") -> "LeaderEntry | None":
        """Official lifetime (or windowed) pnl/vol for ONE wallet — the same
        numbers polymarket.com shows on the profile page. With a `user` filter
        the board answers that wallet's own row as `data` (rank_pnl and
        rank_volume in place of rank), or `data: null` for an unknown wallet,
        so this is the authoritative source for total_pnl/volume_usd
        regardless of rank."""
        row, _ = await self._v2_page(
            "leaderboard",
            {"category": "overall", "time_period": period.lower(), "sort_by": "PNL",
             "user": wallet_address},
        )
        if isinstance(row, list):
            row = row[0] if row else None
        return LeaderEntry.from_api(row) if isinstance(row, dict) else None

    async def get_user_volume(self, wallet_address: str, *, start: int | None = None,
                              end: int | None = None) -> float | None:
        """A wallet's traded volume in USD (`volume_usdc`) over [start, end],
        epoch seconds floored to UTC days; omitted bounds cover its full
        history. The leaderboard's `volume` counts shares, so USD volume comes
        from here. A wallet with no trades is 0.0; None when the answer has no
        row."""
        params: dict[str, Any] = {"user": wallet_address}
        if start:
            params["start"] = int(start)
        if end:
            params["end"] = int(end)
        row, _ = await self._v2_page("user-volume", params)
        if not isinstance(row, dict) or row.get("volume_usdc") is None:
            return None
        return _f(row["volume_usdc"])

    # --- geoblock ----------------------------------------------------------
    async def get_geoblock(self) -> dict:
        """Geo-restriction check for the caller's IP (call before placing orders).
        Returns {blocked: bool, ip, country, region}.

        Cached: it was fetched before EVERY order, an extra round trip in the
        latency path of every copy, with the full read-retry policy (up to
        four attempts with backoff and a 15s timeout each) for an answer that
        only describes this server's egress IP and is advisory by default
        (execution._check_frontend_geoblock). One quick attempt; a failure is
        remembered briefly so a dead probe does not stall each order in turn.
        """
        now = time.monotonic()
        cached = getattr(self, "_geoblock_cache", None)
        if cached is not None and cached[0] > now:
            if isinstance(cached[1], Exception):
                raise cached[1]
            return cached[1]
        try:
            r = await self._client.get("https://polymarket.com/api/geoblock",
                                       timeout=GEOBLOCK_TIMEOUT_SECONDS)
            r.raise_for_status()
            value = r.json()
        except Exception as exc:
            self._geoblock_cache = (now + GEOBLOCK_FAILURE_CACHE_SECONDS, exc)
            raise
        self._geoblock_cache = (now + GEOBLOCK_CACHE_SECONDS, value)
        return value

    # --- bridge deposit ------------------------------------------------------
    async def create_bridge_address(self, wallet_address: str) -> dict:
        """Deposit addresses across EVM/Solana/Bitcoin/Tron for funding
        `wallet_address` from any supported chain in USDC/USDT/etc. — whatever
        arrives is auto-converted into pUSD at that address by Polymarket's
        Collateral Onramp. No gas needed on our side for this step (the bridge
        itself is a Polymarket-run service); the on-chain allowance approval
        for actually trading is a separate, still gas-costing step for an EOA
        wallet (see wallet.py).
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

    async def _activity_page(self, wallet_address: str, kind: str, limit: int,
                             cursor: str | None) -> tuple[list[dict], str | None]:
        """One most-recent-first page of one activity type (limit up to 1000),
        and the cursor of the next page. The filters go along with the cursor."""
        params: dict[str, Any] = {"user": wallet_address, "type": kind, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        rows, next_cursor = await self._v2_page("activity", params)
        return (rows if isinstance(rows, list) else []), next_cursor

    async def redeems_page(self, wallet_address: str, limit: int = 50,
                           cursor: str | None = None) -> tuple[list[dict], str | None]:
        """A page of REDEEM rows (v2 fields plus their v1 names) and the next
        page's cursor."""
        rows, next_cursor = await self._activity_page(wallet_address, "REDEEM", limit, cursor)
        return [_with_v1_names(r) for r in rows if isinstance(r, dict)], next_cursor

    async def get_redeems(self, wallet_address: str, limit: int = 50) -> list[dict]:
        """REDEEM activity — how much a wallet was paid when resolved positions
        were redeemed (winners redeem $1/share; losers produce no payout).
        Used to finalize copy positions whose market died before we could exit,
        and by trader_stats to credit hold-to-resolution wins as realized PnL."""
        rows, _ = await self.redeems_page(wallet_address, limit)
        return rows

    async def trade_history_page(self, wallet_address: str, limit: int = 100,
                                 cursor: str | None = None) -> tuple[list[Trade], str | None]:
        """A page of TRADE activity and the next page's cursor."""
        rows, next_cursor = await self._activity_page(wallet_address, "TRADE", limit, cursor)
        return [Trade.from_api(t) for t in rows if isinstance(t, dict)], next_cursor

    async def get_trade_history(self, wallet_address: str, limit: int = 100) -> list[Trade]:
        """Most-recent-first TRADE activity, the first `limit` rows."""
        trades, _ = await self.trade_history_page(wallet_address, limit)
        return trades
