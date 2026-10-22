"""A copied wallet's free cash, read from Polygon, for capital-share sizing.

A capital-share follow sizes each copy from the share of the leader's free
cash the bet took (copy_engine.cash_share). Free cash is what the leader's
wallet holds of pUSD, the exchange's collateral, plus bridged and native USDC,
which the deposit flow converts to pUSD one for one. Open positions are left
out of it: the owner chose free cash as the base on 2026-10-22.

One reading serves every follower of a leader for LEADER_CASH_CACHE_SECONDS,
and only trades it postdates. A balance read before a bet still holds the
money the bet spent, so sizing that bet from it would read the share low.

Every failure raises LeaderCashError, whose message leaves out the RPC URL
(provider URLs embed API keys). The engine treats it as no reading and skips
the copy.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import re
import time

import httpx

from backend.core.withdraw import PUSD, UNITS_PER_USD, USDC, USDC_E

POLYGON_CHAIN_ID = 137
CASH_TOKENS = (PUSD, USDC_E, USDC)
# ERC-20 balanceOf(address)
BALANCE_OF_SELECTOR = "70a08231"

LEADER_CASH_CACHE_SECONDS = float(os.environ.get("LEADER_CASH_CACHE_SECONDS", "30"))
# A cached reading sizes a trade only when it was taken this long after the
# trade's own timestamp: the block has to be on the node we read from.
SETTLE_SECONDS = 2.0
# A failed reading is remembered this long, so a dead RPC costs at most one
# timeout per leader in that time, however many of its trades arrive.
FAILURE_MEMORY_SECONDS = 5.0
_MAX_ENTRIES = 2048

_HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class LeaderCashError(RuntimeError):
    """The chain could not say how much free cash the wallet holds."""


class LeaderCashReader:
    """Reads and briefly shares a wallet's free cash (USD) over JSON-RPC."""

    def __init__(self, rpc_url: str, *, ttl: float = LEADER_CASH_CACHE_SECONDS,
                 timeout: float = 8.0, transport: httpx.AsyncBaseTransport | None = None,
                 clock=time.time) -> None:
        if not rpc_url:
            raise ValueError("an RPC URL is required")
        self._url = rpc_url
        self._ttl = float(ttl)
        self._clock = clock
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._ids = itertools.count(1)
        self._verified = False
        # wallet -> (time the read started, usd)
        self._cache: dict[str, tuple[float, float]] = {}
        # wallet -> (time it failed, error)
        self._failed: dict[str, tuple[float, LeaderCashError]] = {}
        # wallet -> (time the read started, task)
        self._inflight: dict[str, tuple[float, asyncio.Task]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def cash_usd(self, wallet: str, *, after: float | None = None) -> float:
        """Free cash `wallet` holds, in USD.

        after  unix time of the trade being sized. A cached reading counts
               only if it was taken SETTLE_SECONDS or more after it; otherwise
               the chain is read again.
        """
        if not _HEX_ADDRESS.match(str(wallet or "")):
            raise LeaderCashError("not a wallet address")
        key = wallet.lower()
        now = self._clock()
        due = None if after is None else float(after) + SETTLE_SECONDS
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < self._ttl and (due is None or hit[0] >= due):
            return hit[1]
        failed = self._failed.get(key)
        if failed is not None and now - failed[0] < FAILURE_MEMORY_SECONDS:
            raise failed[1]
        # Followers of one trade share the read already on its way, unless it
        # started too early to see this trade.
        flight = self._inflight.get(key)
        if flight is None or (due is not None and flight[0] < due):
            task = asyncio.ensure_future(self._read(key, now))
            flight = (now, task)
            self._inflight[key] = flight
            task.add_done_callback(lambda done, k=key, f=flight: self._landed(k, f, done))
        return await asyncio.shield(flight[1])

    def _landed(self, key: str, flight: tuple, task: asyncio.Task) -> None:
        if self._inflight.get(key) is flight:
            self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()   # retrieved, so a failure no caller awaited is not reported as lost

    async def _read(self, wallet: str, started: float) -> float:
        try:
            await self._verify()
            units = await asyncio.gather(
                *(self._balance(token, wallet) for token in CASH_TOKENS))
        except LeaderCashError as exc:
            self._remember(self._failed, wallet, (self._clock(), exc))
            raise
        usd = sum(units) / UNITS_PER_USD
        self._failed.pop(wallet, None)
        self._remember(self._cache, wallet, (started, usd))
        return usd

    def _remember(self, store: dict, key: str, value: tuple) -> None:
        store[key] = value
        if len(store) > _MAX_ENTRIES:
            cutoff = self._clock() - max(self._ttl, FAILURE_MEMORY_SECONDS)
            for k in [k for k, v in store.items() if v[0] < cutoff]:
                store.pop(k, None)

    async def _call(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method,
                   "params": params}
        try:
            response = await self._http.post(self._url, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise LeaderCashError(f"{method}: HTTP {exc.response.status_code}") from None
        except Exception as exc:      # the exception text may carry the URL
            raise LeaderCashError(f"{method}: {type(exc).__name__}") from None
        if not isinstance(body, dict):
            raise LeaderCashError(f"{method}: malformed response")
        error = body.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            raise LeaderCashError(f"{method}: RPC error {code}")
        if "result" not in body:
            raise LeaderCashError(f"{method}: response has no result")
        return body["result"]

    async def _verify(self) -> None:
        """Refuse readings from the wrong chain or a node without pUSD. Either
        would read 0 for every wallet, and 0 cash sizes every bet as all-in."""
        if self._verified:
            return
        chain = await self._call("eth_chainId", [])
        try:
            chain_id = int(str(chain), 16)
        except ValueError:
            raise LeaderCashError("eth_chainId: malformed quantity") from None
        if chain_id != POLYGON_CHAIN_ID:
            raise LeaderCashError(f"RPC serves chain {chain_id}, expected {POLYGON_CHAIN_ID}")
        code = await self._call("eth_getCode", [PUSD, "latest"])
        if not isinstance(code, str) or not code.removeprefix("0x"):
            raise LeaderCashError("no contract code at the pUSD address")
        self._verified = True

    async def _balance(self, token: str, wallet: str) -> int:
        data = "0x" + BALANCE_OF_SELECTOR + wallet[2:].lower().rjust(64, "0")
        raw = await self._call("eth_call", [{"to": token, "data": data}, "latest"])
        if not isinstance(raw, str) or len(raw.removeprefix("0x")) != 64:
            raise LeaderCashError("eth_call: balanceOf returned malformed data")
        try:
            return int(raw, 16)
        except ValueError:
            raise LeaderCashError("eth_call: balanceOf returned malformed data") from None
