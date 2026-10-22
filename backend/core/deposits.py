"""Deposit alerts: a Telegram message from the bot when a deposit lands.

How a deposit lands (read on Polygon on 2026-10-22 and 2026-10-23, from
PolyTrade's own deposits and from live traffic):

  1. Polymarket's bridge pays USDC.e on Polygon into the user's personal
     deposit account, the ERC-4337 smart account behind the addresses that
     Fund wallet shows.
  2. A sponsored UserOperation hands that USDC.e to Polymarket's
     CollateralOnramp, which wraps it: pUSD is minted from 0x0 to the user's
     wallet (users.id), and pUSD logs Wrapped(caller=onramp, asset=USDC.e,
     recipient=wallet, amount).

Payouts and sales raise the same balance, so a balance increase, or even a
pUSD mint, proves nothing by itself:

  - Auto-redeem payouts by Polymarket's operator. The older path wraps the
    payout through the onramp as well, so it logs the same Wrapped event and
    mint; in that transaction the operator pulls the wallet's resolved
    positions and passes the USDC.e it redeemed to the wrap. The newer path
    pulls the positions from the position manager and pays the wallet with a
    plain pUSD transfer from the operator.
  - Redemptions through the collateral adapters (redeem.py) and sells that
    settle as a merge: pUSD minted to the wallet with an adapter as the
    Wrapped caller, positions out of the wallet in the same transaction.
  - Other sells: pUSD transferred from the buyer or the exchange as the
    shares leave the wallet.

The rule. A deposit is pUSD the onramp minted to the wallet (a Wrapped event
with the onramp as caller and the wallet as recipient, and its mint) in a
successful transaction that moves no ERC-1155 token out of the wallet
(TransferSingle or TransferBatch, from any contract, with the wallet as
`from`) and takes no USDC.e from the auto-redeem operator. Every payout and
sale above moves positions out of the wallet in the transaction that pays it.

Reading the chain. eth_getLogs for those Wrapped events over finalized blocks
only, CHUNK_BLOCKS at a time: the SDK's public endpoint, used while
POLYGON_RPC_URL is unset, answers at most 101 blocks per query. Each matching
transaction's receipt is then checked against the rule. Nothing here signs or
sends a transaction.

Once per deposit. Each deposit is a row in `deposits`, unique per wallet and
transaction, and its alert is queued only by the insert that creates the row:
a rescan, a restart or a second process finds the row and sends nothing.
`chain_cursors` keeps the last block scanned. The first pass ever starts at
the finalized head, so deposits from before deposit alerts existed get none.
After downtime the scan resumes after the saved block, at most
MAX_CATCHUP_BLOCKS (24 hours) back, and an alert sent more than LATE_SECONDS
after its deposit says when the deposit landed.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass

from eth_utils import keccak

from backend.core.withdraw import (
    PUSD, USDC_E, ChainReadError, PolygonReader, _int, _iso, _topic, _topic_address,
    receipt_ok, token_transfers, usd,
)

log = logging.getLogger("deposits")

COLLATERAL_ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
# The SDK's auto_redeem_operator (polymarket-client 0.6.0, production config).
AUTO_REDEEM_OPERATOR = "0xa1200000d0002264C9a1698e001292D00E1b00af"
ZERO_ADDRESS = "0x" + "0" * 40

# pUSD logs one of these per wrap: caller, asset and recipient indexed, the
# amount in data.
WRAPPED_TOPIC = "0x" + keccak(text="Wrapped(address,address,address,uint256)").hex()
TRANSFER_SINGLE_TOPIC = "0x" + keccak(
    text="TransferSingle(address,address,address,uint256,uint256)").hex()
TRANSFER_BATCH_TOPIC = "0x" + keccak(
    text="TransferBatch(address,address,address,uint256[],uint256[])").hex()
_POSITION_MOVES = frozenset({TRANSFER_SINGLE_TOPIC, TRANSFER_BATCH_TOPIC})

CURSOR = "deposits"
# Blocks per eth_getLogs. The SDK's public endpoint (polygon.drpc.org) refuses
# a range of more than 101 blocks (measured 2026-10-23).
CHUNK_BLOCKS = 100
WALLETS_PER_QUERY = 200
# Polygon makes a block about every 1.5 s, so this is 24 hours.
MAX_CATCHUP_BLOCKS = 57_600
# Below a cent the alert would read $0.00.
MIN_DEPOSIT_UNITS = 10_000
# An alert sent this long after its deposit landed says when it landed.
LATE_SECONDS = 600.0

_ONRAMP = COLLATERAL_ONRAMP.lower()
_OPERATOR = AUTO_REDEEM_OPERATOR.lower()
_PUSD = PUSD.lower()
_HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH = re.compile(r"^0x[0-9a-f]{64}$")


def _topics(entry: dict) -> list[str]:
    return [str(t).lower() for t in entry.get("topics") or []]


def deposit_units(receipt: dict | None, wallet: str) -> tuple[int, str | None]:
    """pUSD this receipt deposited into `wallet` by the rule in the module
    docstring: (units, None) for a deposit and (0, reason) for anything else."""
    if not receipt_ok(receipt):
        return 0, "the transaction failed"
    wallet = wallet.lower()
    logs = receipt.get("logs") or []
    for entry in logs:
        topics = _topics(entry)
        if len(topics) == 4 and topics[0] in _POSITION_MOVES and _topic_address(topics[2]) == wallet:
            return 0, "positions left the wallet in the same transaction (a payout or a sale)"
    if any(sender == _OPERATOR for sender, _, _ in token_transfers(receipt, USDC_E)):
        return 0, "the auto-redeem operator funded the wrap (a payout)"
    wrapped = 0
    for entry in logs:
        topics = _topics(entry)
        if (str(entry.get("address") or "").lower() == _PUSD and len(topics) == 4
                and topics[0] == WRAPPED_TOPIC and _topic_address(topics[1]) == _ONRAMP
                and _topic_address(topics[3]) == wallet):
            data = str(entry.get("data") or "0x")
            wrapped += int(data, 16) if len(data) > 2 else 0
    if not wrapped:
        return 0, "the onramp wrapped nothing for this wallet"
    minted = sum(amount for sender, to, amount in token_transfers(receipt, PUSD)
                 if sender == ZERO_ADDRESS and to == wallet)
    if minted < wrapped:
        return 0, "the wallet was minted less pUSD than the onramp reports"
    return wrapped, None


class DepositChain(PolygonReader):
    """The withdrawals' Polygon reader with the reads a deposit scan needs:
    the finalized head, a block's time and the onramp's Wrapped logs.
    POLYGON_RPC_URL when set, else the SDK's public endpoint (main.py
    decides, as it does for the redeemer)."""

    async def finalized_block(self) -> int:
        block = await self._rpc("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(block, dict) or not block.get("number"):
            raise ChainReadError("the node reported no finalized block")
        return _int(block["number"])

    async def block_time(self, number: int) -> float:
        block = await self._rpc("eth_getBlockByNumber", [hex(number), False])
        if not isinstance(block, dict) or not block.get("timestamp"):
            raise ChainReadError(f"block {number} is unavailable")
        return float(_int(block["timestamp"]))

    async def onramp_wraps(self, wallets: list[str], from_block: int,
                           to_block: int) -> list[dict]:
        """pUSD Wrapped logs with the onramp as caller and one of `wallets`
        as recipient. An answer that is not a list raises: the blocks are
        read again on the next pass."""
        result = await self._rpc("eth_getLogs", [{
            "address": PUSD, "fromBlock": hex(from_block), "toBlock": hex(to_block),
            "topics": [WRAPPED_TOPIC, _topic(COLLATERAL_ONRAMP), None,
                       [_topic(w) for w in wallets]]}])
        if not isinstance(result, list):
            raise ChainReadError("eth_getLogs returned no list")
        return result


@dataclass
class PassSummary:
    to_block: int | None = None   # the last block read
    blocks: int = 0               # blocks read in this pass
    deposits: int = 0             # new deposits, recorded and announced
    set_aside: int = 0            # onramp mints the rule found to be payouts
    skipped_blocks: int = 0       # left unread after downtime past MAX_CATCHUP_BLOCKS
    started: bool = False         # the first pass ever, which only sets the start

    def noteworthy(self) -> bool:
        return bool(self.started or self.deposits or self.set_aside or self.skipped_blocks)

    def describe(self) -> str:
        if self.started:
            return f"the deposit scan starts after block {self.to_block}"
        text = (f"{self.blocks} block(s) read to {self.to_block}: {self.deposits} "
                f"deposit(s), {self.set_aside} payout(s) set aside")
        if self.skipped_blocks:
            text += f"; {self.skipped_blocks} older block(s) left unread"
        return text


class DepositWatcher:
    """Finds deposits on Polygon and announces each one once.

    `chain` is a DepositChain and `notify(event)` queues a Telegram alert
    (main.py passes the shared notifier). Each pass reads every block
    finalized since the last block a pass finished.
    """

    def __init__(self, db, chain, *, notify=None, chunk_blocks: int = CHUNK_BLOCKS,
                 max_catchup_blocks: int = MAX_CATCHUP_BLOCKS, clock=time.time) -> None:
        self.db = db
        self.chain = chain
        self.notify = notify
        self.chunk_blocks = max(1, int(chunk_blocks))
        self.max_catchup_blocks = max(1, int(max_catchup_blocks))
        self._clock = clock

    async def run_pass(self, stop: asyncio.Event | None = None) -> PassSummary:
        summary = PassSummary()
        head = await self.chain.finalized_block()
        last = await self._cursor()
        if last is None:
            await self.db.execute(
                "INSERT INTO chain_cursors(name, last_block, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO NOTHING", (CURSOR, head, self._now()))
            summary.started, summary.to_block = True, head
            return summary
        start = last + 1
        if head - last > self.max_catchup_blocks:
            start = head - self.max_catchup_blocks + 1
            summary.skipped_blocks = start - last - 1
            log.warning("deposit scan was %d blocks behind; it resumes at block %d, so "
                        "deposits in the %d blocks before that get no alert",
                        head - last, start, summary.skipped_blocks)
            await self._advance(start - 1)
        wallets = await self._wallets()
        while start <= head and not (stop is not None and stop.is_set()):
            end = min(head, start + self.chunk_blocks - 1)
            if wallets:
                await self._scan(wallets, start, end, summary)
            await self._advance(end)
            summary.blocks += end - start + 1
            summary.to_block = end
            start = end + 1
        return summary

    async def _scan(self, wallets: dict[str, str], start: int, end: int,
                    summary: PassSummary) -> None:
        """Check every onramp wrap to one of `wallets` in blocks start..end
        against the rule; record and announce each deposit once."""
        addresses = sorted(wallets)
        found: dict[str, tuple[int, set[str]]] = {}
        for i in range(0, len(addresses), WALLETS_PER_QUERY):
            for entry in await self.chain.onramp_wraps(
                    addresses[i:i + WALLETS_PER_QUERY], start, end):
                tx = str(entry.get("transactionHash") or "").lower()
                topics = _topics(entry)
                if not _TX_HASH.match(tx) or len(topics) != 4:
                    continue
                block = _int(entry.get("blockNumber"))
                found.setdefault(tx, (block, set()))[1].add(_topic_address(topics[3]))
        for tx, (block, recipients) in sorted(found.items(), key=lambda item: item[1][0]):
            receipt = await self.chain.receipt(tx)
            if receipt is None:
                # A transaction in a finalized block has one; this node lags.
                raise ChainReadError(f"no receipt yet for {tx}")
            for wallet in sorted(recipients & wallets.keys()):
                user_id = wallets[wallet]
                units, reason = deposit_units(receipt, wallet)
                if reason is not None:
                    summary.set_aside += 1
                    log.info("deposit scan: set aside %s tx=%s: %s", user_id[:10], tx, reason)
                    continue
                if units >= MIN_DEPOSIT_UNITS:
                    await self._record(user_id, tx, block, units, summary)

    async def _record(self, user_id: str, tx: str, block: int, units: int,
                      summary: PassSummary) -> None:
        landed = await self.chain.block_time(block)
        deposit_id = uuid.uuid4().hex
        created = await self.db.execute(
            "INSERT INTO deposits(id, user_id, tx_hash, amount_units, block_number, "
            "landed_at, detected_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, tx_hash) DO NOTHING",
            (deposit_id, user_id, tx, units, block, _iso(landed), self._now()))
        if not created:
            return            # recorded before: its alert has gone out already
        summary.deposits += 1
        log.info("DEPOSIT %s +$%s pUSD tx=%s block=%d", user_id[:10], usd(units), tx, block)
        if self.notify is None:
            return
        event = {"event": "deposit_received", "user_id": user_id, "deposit_id": deposit_id,
                 "amount_usd": usd(units), "tx_hash": tx}
        if self._clock() - landed > LATE_SECONDS:
            event["landed_at"] = _iso(landed)
        try:
            await self.notify(event)
        except Exception:
            # The deposit is recorded; an alert is best-effort.
            log.exception("deposit %s: alert failed", deposit_id[:8])

    async def _wallets(self) -> dict[str, str]:
        """Lower-case address -> users.id, for every user with a wallet."""
        rows = await self.db.fetchall("SELECT id FROM users")
        return {str(r["id"]).lower(): str(r["id"]) for r in rows
                if _HEX_ADDRESS.match(str(r["id"] or ""))}

    async def _cursor(self) -> int | None:
        value = await self.db.fetchval(
            "SELECT last_block FROM chain_cursors WHERE name=?", (CURSOR,))
        return None if value is None else int(value)

    async def _advance(self, block: int) -> None:
        # Forward only, so a second process that is behind never moves it back.
        await self.db.execute(
            "UPDATE chain_cursors SET last_block=?, updated_at=? WHERE name=? AND last_block<?",
            (block, self._now(), CURSOR, block))

    def _now(self) -> str:
        return _iso(self._clock())
