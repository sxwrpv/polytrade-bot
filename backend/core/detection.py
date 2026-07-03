"""Leader trade detection — the fast path that shrinks copy latency.

A `TradeDetector` reports a leader's *new* trades (with the exact fill price)
since a cursor. The engine reacts immediately with a market order (slippage-
guarded against that fill price), instead of waiting for the 30s
position-diff sweep.

Two tiers behind one interface:
  - ActivityPollDetector (now): polls /activity?type=TRADE per leader. ~3-8s
    end-to-end, bounded by Polymarket's indexer lag. No extra dependencies.
  - OnChainDetector (later): subscribe to Polygon OrderFilled logs filtered by
    the leader's address. ~2-4s and attributed. Drops in here unchanged — needs
    a Polygon WebSocket RPC endpoint. Stub left below.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

from backend.core.polymarket import Trade

# Polymarket v2 settlement contracts on Polygon (verified on-chain 2026-06: current
# trading routes through these, not the legacy v1 CTF/NegRisk exchanges).
CTF_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"      # binary markets
NEGRISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"  # multi-outcome
# v2 order-fill event topic0 (data layout: makerAssetId, takerAssetId,
# makerAmountFilled, takerAmountFilled, ...; maker = indexed topics[2]).
ORDERFILLED_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"


class TradeDetector(ABC):
    @abstractmethod
    async def new_trades(self, trader_address: str, since_ts: int) -> list:
        """Leader trades with timestamp > since_ts (each carries side + price)."""
        ...


class ActivityPollDetector(TradeDetector):
    def __init__(self, pm, limit: int = 50) -> None:
        self.pm = pm
        self.limit = limit

    async def new_trades(self, trader_address: str, since_ts: int) -> list:
        trades = await self.pm.get_trade_history(trader_address, limit=self.limit)
        return [t for t in trades if t.timestamp > since_ts]


class OnChainDetector(TradeDetector):
    """Polls Polygon `OrderFilled` logs filtered by the leader (maker). ~2-4s and
    attributed — the leader's exact fill price comes straight from the event.

    Decoding (per the exchange ABI): OrderFilled(orderHash, maker, taker,
    makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled, fee).
    makerAssetId==0 means the maker provided USDC -> a BUY of takerAssetId;
    otherwise a SELL of makerAssetId. Price = USDC-leg / shares-leg (both 1e6).
    Order metadata (title/slug/condition) isn't in the event — left empty; the
    reconciler backfills from positions.
    """

    def __init__(self, rpc_url: str, *, exchanges=None, max_block_span: int = 2000) -> None:
        from web3 import Web3
        self._Web3 = Web3
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.exchanges = [Web3.to_checksum_address(a)
                          for a in (exchanges or [CTF_EXCHANGE, NEGRISK_EXCHANGE])]
        self.max_block_span = max_block_span
        self._last_block: dict[str, int] = {}

    @staticmethod
    def _maker_topic(address: str) -> str:
        return "0x" + address.lower().removeprefix("0x").rjust(64, "0")

    def _decode(self, log, leader: str) -> Trade:
        data = bytes(log["data"])
        w = [int.from_bytes(data[i:i + 32], "big") for i in range(0, 160, 32)]
        maker_asset, taker_asset, maker_amt, taker_amt = w[0], w[1], w[2], w[3]
        if maker_asset == 0:                       # maker gave USDC -> BUY
            side, token, usdc, shares = "BUY", taker_asset, maker_amt, taker_amt
        else:                                      # maker gave tokens -> SELL
            side, token, usdc, shares = "SELL", maker_asset, taker_amt, maker_amt
        price = (usdc / shares) if shares else 0.0
        return Trade(
            proxy_wallet=leader, timestamp=int(time.time()), condition_id="",
            side=side, asset=str(token), outcome="", outcome_index=0,
            price=round(price, 6), size=shares / 1e6, usd_size=usdc / 1e6,
            title="", slug="", tx_hash=self._Web3.to_hex(log["transactionHash"]))

    def _scan(self, leader: str, from_block: int, to_block: int) -> list[Trade]:
        logs = self.w3.eth.get_logs({
            "fromBlock": from_block, "toBlock": to_block, "address": self.exchanges,
            "topics": [ORDERFILLED_TOPIC, None, self._maker_topic(leader)],
        })
        logs = sorted(logs, key=lambda lg: (lg["blockNumber"], lg["logIndex"]))
        return [self._decode(lg, leader) for lg in logs]

    async def new_trades(self, trader_address: str, since_ts: int) -> list:
        def _run():
            latest = self.w3.eth.block_number
            last = self._last_block.get(trader_address)
            if last is None:                       # first sight: start now
                self._last_block[trader_address] = latest
                return []
            from_block = max(last + 1, latest - self.max_block_span)
            if from_block > latest:
                return []
            trades = self._scan(trader_address, from_block, latest)
            self._last_block[trader_address] = latest
            return trades
        return await asyncio.to_thread(_run)
