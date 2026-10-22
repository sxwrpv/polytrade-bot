"""Withdrawals: cash out of a custodial deposit wallet as native USDC on Polygon.

A user's cash is pUSD in their Polymarket deposit wallet. Polymarket's bridge
(bridge.polymarket.com, run with fun.xyz) turns pUSD into native USDC and pays
it to an address the user enters. The flow, approved 2026-10-22 (bridge facts
read live from its API and OpenAPI spec that day):

  1. Quote. POST /quote prices pUSD on Polygon into native USDC on Polygon for
     the destination. Near 1:1 with a tight minimum is the normal answer
     (2026-10-22: $500 quoted 499.999989, minimum 499.899489). A quote more
     than 10 bp short of the amount, or with a minimum more than 50 bp short,
     is refused: Polymarket's own app holds withdrawals to under 10 bp, and a
     short quote usually means the Uniswap pool behind the bridge is drained.
  2. Reserve. Under the per-user lock the engine holds for a whole BUY, the
     free balance is read (see free balance below) and the withdrawal row is
     inserted in one write transaction.
  3. Bridge address. POST /withdraw returns the address the bridge watches for
     this wallet, chain, token and recipient. It is created seconds before the
     send (the docs warn against generating one in advance). GET /status must
     answer 200 for it (an address the bridge does not know answers an error),
     and its transaction count becomes the baseline our transfer is matched
     against.
  4. Send. One gasless relayer transaction from the deposit wallet,
     pUSD.transfer(bridge address, amount), signed by the wallet's signer: the
     path wallet.ensure_allowances and redeem.py already use. A plain transfer
     names no operator or spender for the relayer's policy to refuse.
  5. Track. GET /status until the bridge reports COMPLETED with its delivery
     transaction, which must pay at least the quoted minimum of native USDC to
     the destination on Polygon, or FAILED.

The bridge address cannot be proven on-chain before the send. It is trusted
as bridge.polymarket.com's answer over TLS, and the per-withdrawal and daily
caps bound what that trust can cost. What happens to funds after a FAILED
status is undocumented (the spec points to the provider's support), so such a
withdrawal goes to 'attention' for an operator, and two unresolved ones pause
withdrawals for everyone.

Free balance: the lower of the wallet's on-chain pUSD balance and the CLOB's,
minus what open BUY claims reserve (reserved, submitting, uncertain), minus
BUY fills from the last SETTLING_SECONDS (the engine books a BUY when the
exchange matches it; its pUSD leaves the wallet at on-chain settlement a few
seconds later), minus withdrawals whose pUSD is still in the wallet. The
engine subtracts those same withdrawals when it sizes a BUY.

Statuses move forward only, each by a compare-and-set on the current one:
  requested   reserved; nothing sent
  submitting  the relayer call is in progress, or its outcome is unknown
  submitted   the relayer accepted the transaction
  bridging    the pUSD transfer is confirmed on-chain; the bridge is delivering
  confirmed   the delivery is verified on Polygon
  failed      proven: nothing left the wallet
  attention   the pUSD left the wallet and delivery is unconfirmed
  refunded    an operator recorded that the bridge returned the funds

Proof rules. Failed means nothing was sent, or the batch can no longer
execute and the chain shows no transfer: DepositWallet batches carry a
deadline (the SDK signs now + 600 s and re-signs on each submit retry), so
BATCH_EXPIRY_SECONDS after submitting began, a pUSD transfer from the wallet
to the bridge address either exists on-chain or never will. Bridging needs the
transfer's receipt, confirmed needs the delivery's. A withdrawal's pUSD is
sent once: nothing here resubmits it.

Operator tool, inside the app container:
    docker compose exec app python -m backend.core.withdraw list
    docker compose exec app python -m backend.core.withdraw resolve <id> --delivered <tx_hash> --yes
    docker compose exec app python -m backend.core.withdraw resolve <id> --refunded "<note>" --yes
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import itertools
import logging
import math
import re
import sys
import time
import uuid
from decimal import ROUND_DOWN, Decimal, InvalidOperation

import aiosqlite
import httpx
from eth_utils import keccak, to_checksum_address
from polymarket import errors as pm_errors
from polymarket.calls import erc20_transfer_call

from backend import config

log = logging.getLogger("withdraw")

# Whether this process offers withdrawals to every linked account, for
# /api/version and the wording that tells users they can withdraw. main.py
# sets it when it starts the service without an allowlist.
STATUS: dict = {"open": False}

UNITS_PER_USD = 1_000_000          # pUSD and USDC both carry 6 decimals
CENT_UNITS = 10_000
POLYGON = 137
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"     # native USDC on Polygon
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGONSCAN_TX = "https://polygonscan.com/tx/"

# Polymarket contracts the SDK's environment config does not list
# (docs.polymarket.com/resources/contracts and pUSD's immutables, 2026-10-22),
# and both USDC tokens. Funds sent to any of them cannot be recovered by us.
_EXTRA_PROTOCOL_ADDRESSES = (
    "0x93070a847efEf7F70739046A929D47a521F5B8ee",   # CollateralOnramp
    "0x2957922Eb93258b93368531d39fAcCA3B4dC5854",   # CollateralOfframp
    "0xebC2459Ec962869ca4c0bd1E06368272732BCb08",   # PermissionedRamp
    "0xCe84E053301A82937F90ee2C2c1889cAb1db25dE",   # pUSD implementation
    "0xC417fD8E9661c0d2120B64a04Bb3278C17E99DB1",   # pUSD vault
    "0x1000008dD9001B968442c1000017eaE6E0dA00Ba",   # BinaryModule
    "0x200000900045e3B6259600682756002200028933",   # NegRiskModule
    USDC,
    USDC_E,
)

# DepositWallet.execute rejects a batch past its deadline. The SDK signs now +
# 600 s and re-signs on each of its submit retries (about 20 s of them), so 15
# minutes after submitting began, the transfer exists on-chain or never will.
BATCH_EXPIRY_SECONDS = 15 * 60
# A 'requested' row this old lost the process that was about to send it.
STALE_REQUEST_SECONDS = 120
# The relayer runs one action per wallet at a time, and the redeemer's batch
# can be holding it. "Busy" means nothing was accepted, so it is retried.
BUSY_RETRY_SECONDS = 60.0
BUSY_RETRY_DELAY = 10.0
# BUY fills this recent may still be settling on-chain.
SETTLING_SECONDS = 120
# Quote guard, in basis points of the amount.
MAX_ESTIMATE_SHORTFALL_BP = 10
MAX_MINIMUM_SHORTFALL_BP = 50
# Two unresolved withdrawals in 'attention' pause withdrawals for everyone.
ATTENTION_BRAKE = 2
STATUS_POLL_SECONDS = 12.0
# How far past the submit block the pUSD transfer is searched for (about 35
# minutes; the batch deadline is under 500 blocks).
LOG_SEARCH_BLOCKS = 1_000
# How far past the submit block the bridge's USDC payment is searched for
# (about an hour).
DELIVERY_SEARCH_BLOCKS = 1_800
# Blocks per eth_getLogs call: the SDK's public Polygon endpoint (drpc's free
# plan) answers a 100-block range and refuses 500.
LOG_CHUNK_BLOCKS = 100
# A withdrawal in 'attention' that the bridge had not reported failed is checked
# again this often, for a day: its USDC payment may still show up on-chain.
ATTENTION_RECHECK_SECONDS = 300.0
ATTENTION_RECHECK_FOR_SECONDS = 24 * 3600
BRIDGE_PAGE_LIMIT = 100
BRIDGE_MAX_PAGES = 10
SUPPORTED_ASSETS_CACHE_SECONDS = 600.0

HOLDING = ("requested", "submitting", "submitted")    # the pUSD is still in the wallet
OPEN = HOLDING + ("bridging", "attention")
_IN_PROGRESS = HOLDING + ("bridging",)                # moved forward by this module
_STAMPS = {"submitting": "submitting_at", "submitted": "submitted_at",
           "bridging": "bridging_at", "confirmed": "confirmed_at",
           "failed": "failed_at", "attention": "attention_at"}

_HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BYTES32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
_BALANCE_OF = "0x" + keccak(text="balanceOf(address)").hex()[:8]

# Relayer answers about this wallet or this moment (the SDK has retried them
# already): nothing was accepted, so the send may be tried again shortly.
_BUSY = re.compile(r"wallet busy|in-flight action|does not match on-chain nonce", re.IGNORECASE)
# Answers that will be the same for every wallet: the relayer will not relay
# this call for our builder key.
_POLICY = re.compile(
    r"allowed list|allowlist|whitelist|not allowed|not permitted|unauthori[sz]ed|forbidden",
    re.IGNORECASE)

_HELD_SQL = ("SELECT COALESCE(SUM(amount_units),0) FROM withdrawals WHERE user_id=? "
             "AND status IN ('requested','submitting','submitted')")


class WithdrawRefused(Exception):
    """Refused, and nothing was sent. `status` is the HTTP status for the API."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class BridgeError(Exception):
    """The bridge answered with an error or a shape we do not trust. Messages
    carry no address and no URL."""


class ChainReadError(Exception):
    """A JSON-RPC read without a usable answer. Never carries the RPC URL,
    which embeds the provider's API key."""


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def usd(units: int | None) -> str | None:
    """Base units as dollars, rounded down to the cent: a minimum or a
    delivery is never shown higher than it is."""
    if units is None:
        return None
    return str((Decimal(int(units)) / UNITS_PER_USD).quantize(Decimal("0.01"),
                                                              rounding=ROUND_DOWN))


def short(address) -> str:
    """0x1234…abcd: the only form an address takes in logs and alerts."""
    text = str(address or "")
    return f"{text[:6]}…{text[-4:]}" if len(text) > 12 else text


_ADDRESS_IN_TEXT = re.compile(r"\b0x[0-9a-fA-F]{40}\b")


def redact(text) -> str:
    """Error text from the relayer or the bridge with every address shortened
    (a 64-character transaction hash is left whole), so it can be logged."""
    return _ADDRESS_IN_TEXT.sub(lambda m: short(m.group(0)), str(text))


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def _epoch(value) -> float | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _usd_to_units_up(value) -> int:
    """Dollars (a REAL column) to base units, rounded up: the safe side for a
    reservation that is subtracted from what may leave."""
    return math.ceil(Decimal(str(value or 0)) * UNITS_PER_USD)


def parse_amount(value) -> int:
    """Dollars, as entered, to pUSD base units. Whole cents only."""
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise WithdrawRefused(400, "Enter an amount in dollars, for example 25.50.") from None
    if not amount.is_finite() or amount <= 0:
        raise WithdrawRefused(400, "Enter an amount in dollars, for example 25.50.")
    if amount != amount.quantize(Decimal("0.01")):
        raise WithdrawRefused(400, "Amounts are in whole cents.")
    return int(amount * UNITS_PER_USD)


def normalize_address(raw) -> str:
    """A 0x address in checksummed form. Lower- or upper-case input carries no
    checksum and is accepted; mixed case must pass EIP-55, since a mistyped
    character is exactly what the checksum exists to catch."""
    text = str(raw or "").strip()
    if not _HEX_ADDRESS.match(text):
        raise WithdrawRefused(400, "Enter a Polygon address: 0x followed by 40 letters and digits.")
    checksummed = to_checksum_address(text)
    body = text[2:]
    if body != body.lower() and body != body.upper() and text != checksummed:
        raise WithdrawRefused(400, "This address fails its checksum. Copy it again from the wallet.")
    return checksummed


def protocol_addresses(environment) -> frozenset[str]:
    """Every contract address the SDK trades, redeems or derives wallets
    against, plus the collateral contracts it does not list. Lower-case."""
    from polymarket._internal.environment import get_environment_config
    cfg = get_environment_config(environment)
    found = {a.lower() for a in _EXTRA_PROTOCOL_ADDRESSES}
    for source in (cfg, cfg.wallet_derivation):
        for field in dataclasses.fields(source):
            value = getattr(source, field.name)
            if isinstance(value, str) and _HEX_ADDRESS.match(value):
                found.add(value.lower())
    return frozenset(found)


def destination_refusal(destination: str, user: dict, protocol: frozenset[str]) -> str | None:
    address = destination.lower()
    if int(address, 16) == 0:
        return "The zero address burns funds. Enter your own Polygon address."
    if address == str(user.get("id") or "").lower():
        return "That is your PolyTrade wallet. Enter an address outside PolyTrade."
    if address == str(user.get("signer_address") or "").lower():
        return "That is your PolyTrade signer address. Enter an address outside PolyTrade."
    if address in protocol:
        return "That is a Polymarket contract, and funds sent there cannot be recovered."
    return None


@dataclasses.dataclass(frozen=True)
class Quote:
    quote_id: str
    est_out_units: int
    min_out_units: int
    est_seconds: int


def quote_refusal(units: int, quote: Quote) -> str | None:
    if (quote.est_out_units * 10_000 < units * (10_000 - MAX_ESTIMATE_SHORTFALL_BP)
            or quote.min_out_units * 10_000 < units * (10_000 - MAX_MINIMUM_SHORTFALL_BP)):
        return ("The bridge would deliver noticeably less than this amount right now. "
                "Try a smaller amount, or try again later.")
    return None


def _int(value) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "0x0")
    return int(text, 16) if text.startswith("0x") else int(text)


def _topic_address(topic) -> str:
    return ("0x" + str(topic)[-40:]).lower()


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower()[2:]


def token_transfers(receipt: dict | None, token: str) -> list[tuple[str, str, int]]:
    """(from, to, amount) for every ERC-20 Transfer `token` logged in a receipt."""
    token = token.lower()
    out = []
    for entry in (receipt or {}).get("logs") or []:
        topics = entry.get("topics") or []
        if (str(entry.get("address") or "").lower() != token or len(topics) != 3
                or str(topics[0]).lower() != TRANSFER_TOPIC):
            continue
        data = str(entry.get("data") or "0x")
        out.append((_topic_address(topics[1]), _topic_address(topics[2]),
                    int(data, 16) if len(data) > 2 else 0))
    return out


def receipt_ok(receipt: dict | None) -> bool:
    return isinstance(receipt, dict) and _int(receipt.get("status", "0x0")) == 1


def sent_to_bridge(receipt, wallet: str, bridge_address: str, units: int) -> bool:
    """The receipt moved exactly `units` pUSD from the wallet to the bridge."""
    return receipt_ok(receipt) and (
        (wallet.lower(), bridge_address.lower(), int(units)) in token_transfers(receipt, PUSD))


def delivered_to(receipt, destination: str) -> int:
    """Native USDC the receipt paid to the destination (0 for a failed one)."""
    if not receipt_ok(receipt):
        return 0
    target = destination.lower()
    return sum(amount for _, to, amount in token_transfers(receipt, USDC) if to == target)


def match_transfer(transactions: list[dict], baseline: int, units: int) -> dict | None:
    """The bridge's record of our transfer. /status lists newest first, so the
    entries past the baseline count are the ones that appeared after the
    address was created; the newest of those carrying exactly our amount."""
    fresh = transactions[: max(0, len(transactions) - int(baseline or 0))]
    for tx in fresh:
        if (str(tx.get("fromAmountBaseUnit") or "") == str(int(units))
                and str(tx.get("fromChainId") or "") == str(POLYGON)):
            return tx
    return None


# ---------------------------------------------------------------------------
# Polygon reads
# ---------------------------------------------------------------------------

class PolygonReader:
    """Read-only Polygon JSON-RPC: the head block, an ERC-20 balance, receipts
    and Transfer logs. POLYGON_RPC_URL when set, else the SDK's public endpoint
    (main.py decides, as it does for the redeemer)."""

    def __init__(self, rpc_url: str, *, client: httpx.AsyncClient | None = None,
                 timeout: float = 10.0) -> None:
        self._rpc_url = rpc_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._ids = itertools.count(1)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _rpc(self, method: str, params: list):
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            r = await self._client.post(self._rpc_url, json=body)
            r.raise_for_status()
            reply = r.json()
        except httpx.HTTPStatusError as exc:
            raise ChainReadError(f"rpc answered HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainReadError(f"rpc unreachable ({type(exc).__name__})") from None
        if not isinstance(reply, dict) or reply.get("error"):
            error = reply.get("error") if isinstance(reply, dict) else None
            message = error.get("message") if isinstance(error, dict) else error
            raise ChainReadError(f"{method} failed: {message}")
        return reply.get("result")

    async def block_number(self) -> int:
        return _int(await self._rpc("eth_blockNumber", []))

    async def erc20_balance(self, token: str, owner: str) -> int:
        result = await self._rpc("eth_call", [
            {"to": token, "data": _BALANCE_OF + _topic(owner)[2:]}, "latest"])
        if not isinstance(result, str) or len(result) < 66:
            raise ChainReadError("balanceOf returned no value")
        return int(result, 16)

    async def receipt(self, tx_hash: str) -> dict | None:
        result = await self._rpc("eth_getTransactionReceipt", [tx_hash])
        return result if isinstance(result, dict) else None

    async def transfer_logs(self, token: str, sender: str | None, recipient: str,
                            from_block: int, to_block: int) -> list[dict]:
        """`token` Transfer logs to `recipient` from `sender` (anyone when None),
        oldest first, asked for LOG_CHUNK_BLOCKS blocks at a time."""
        topics = [TRANSFER_TOPIC, _topic(sender) if sender else None, _topic(recipient)]
        out: list[dict] = []
        start = max(0, from_block)
        while start <= to_block:
            end = min(to_block, start + LOG_CHUNK_BLOCKS - 1)
            result = await self._rpc("eth_getLogs", [{
                "address": token, "fromBlock": hex(start), "toBlock": hex(end), "topics": topics}])
            if isinstance(result, list):
                out.extend(result)
            start = end + 1
        return out


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

class BridgeClient:
    """bridge.polymarket.com for withdrawals: minimum, quote, withdrawal
    address, status. Our builder code goes in X-Builder-Code when it is the
    bytes32 the spec requires (a malformed one is answered with 400)."""

    def __init__(self, base_url: str = config.BRIDGE_API, *,
                 client: httpx.AsyncClient | None = None,
                 builder_code: str = config.POLYMARKET_BUILDER_CODE,
                 timeout: float = config.HTTP_TIMEOUT, clock=time.monotonic) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": config.HTTP_USER_AGENT})
        self._headers = {"X-Builder-Code": builder_code} if _BYTES32.match(builder_code or "") else {}
        self._clock = clock
        self._minimums: dict | None = None
        self._minimums_at = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _json(self, method: str, path: str, **kwargs):
        try:
            r = await self._client.request(method, f"{self._base}{path}",
                                           headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise BridgeError(f"bridge unreachable ({type(exc).__name__})") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400:
            message = body.get("error") if isinstance(body, dict) else None
            raise BridgeError(f"bridge answered HTTP {r.status_code}"
                              + (f": {redact(message)[:120]}" if message else ""))
        if not isinstance(body, dict):
            raise BridgeError("bridge answered without a JSON object")
        return body

    async def minimum_units(self) -> int | None:
        """The bridge's minimum for native USDC on Polygon (minCheckoutUsd,
        which the spec applies to deposits and withdrawals), or None when the
        bridge does not list that asset."""
        now = self._clock()
        if self._minimums is None or now - self._minimums_at > SUPPORTED_ASSETS_CACHE_SECONDS:
            body = await self._json("GET", "/supported-assets")
            assets = body.get("supportedAssets")
            if not isinstance(assets, list):
                raise BridgeError("bridge supported assets had an unexpected shape")
            minimums = {}
            for asset in assets:
                token = asset.get("token") if isinstance(asset, dict) else None
                if isinstance(token, dict) and asset.get("minCheckoutUsd") is not None:
                    key = (str(asset.get("chainId")), str(token.get("address") or "").lower())
                    minimums[key] = asset["minCheckoutUsd"]
            self._minimums, self._minimums_at = minimums, now
        value = self._minimums.get((str(POLYGON), USDC.lower()))
        if value is None:
            return None
        return _usd_to_units_up(value)

    async def quote(self, units: int, recipient: str) -> Quote:
        body = await self._json("POST", "/quote", json={
            "fromAmountBaseUnit": str(int(units)), "fromChainId": str(POLYGON),
            "fromTokenAddress": PUSD, "recipientAddress": recipient,
            "toChainId": str(POLYGON), "toTokenAddress": USDC})
        try:
            fees = body.get("estFeeBreakdown") or {}
            return Quote(
                quote_id=str(body.get("quoteId") or "")[:80],
                est_out_units=int(body["estToTokenBaseUnit"]),
                min_out_units=int(Decimal(str(fees["minReceived"])) * UNITS_PER_USD),
                est_seconds=int(round(float(body.get("estCheckoutTimeMs") or 0) / 1000)))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise BridgeError("bridge quote had an unexpected shape") from None

    async def create_address(self, *, wallet: str, recipient: str) -> str:
        body = await self._json("POST", "/withdraw", json={
            "address": wallet, "toChainId": str(POLYGON), "toTokenAddress": USDC,
            "recipientAddr": recipient})
        addresses = body.get("address")
        address = addresses.get("evm") if isinstance(addresses, dict) else None
        if not isinstance(address, str) or not _HEX_ADDRESS.match(address):
            raise BridgeError("bridge returned no EVM withdrawal address")
        return to_checksum_address(address)

    async def transactions(self, bridge_address: str) -> list[dict]:
        """Every transaction the bridge lists for the address, newest first."""
        out: list[dict] = []
        cursor = None
        for _ in range(BRIDGE_MAX_PAGES):
            params: dict = {"limit": BRIDGE_PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            body = await self._json("GET", f"/status/{bridge_address}", params=params)
            page = body.get("transactions")
            if not isinstance(page, list):
                raise BridgeError("bridge status had an unexpected shape")
            out.extend(tx for tx in page if isinstance(tx, dict))
            cursor = body.get("nextCursor")
            if not cursor:
                return out
        raise BridgeError("bridge status history is longer than expected")


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Limits:
    floor_units: int       # under the bridge's own minimum; the higher one applies
    max_units: int
    daily_units: int
    daily_count: int
    bridge_timeout: float

    @classmethod
    def from_config(cls) -> "Limits":
        return cls(
            floor_units=_usd_to_units_up(config.WITHDRAW_MIN_USD),
            max_units=int(Decimal(str(config.WITHDRAW_MAX_USD)) * UNITS_PER_USD),
            daily_units=int(Decimal(str(config.WITHDRAW_DAILY_MAX_USD)) * UNITS_PER_USD),
            daily_count=int(config.WITHDRAW_DAILY_COUNT),
            bridge_timeout=float(config.WITHDRAW_BRIDGE_TIMEOUT))


def public(row: dict) -> dict:
    """A withdrawal as the account owner sees it."""
    status = row["status"]
    held_until = None
    if status in ("submitting", "submitted"):
        started = _epoch(row.get("submitting_at") or row.get("requested_at"))
        if started is not None:
            held_until = _iso(started + BATCH_EXPIRY_SECONDS)
    proof = row.get("delivery_tx_hash") or row.get("tx_hash")
    return {
        "id": row["id"], "status": status,
        "amount": usd(row["amount_units"]), "destination": row["destination"],
        "asset": "USDC", "chain": "Polygon",
        "receive_estimate": usd(row.get("quoted_out_units")),
        "receive_minimum": usd(row.get("min_out_units")),
        "delivered": usd(row.get("delivered_units")),
        "tx_hash": row.get("tx_hash"), "delivery_tx_hash": row.get("delivery_tx_hash"),
        "explorer_url": POLYGONSCAN_TX + proof if proof else None,
        "error": row.get("error"),
        "requested_at": row.get("requested_at"), "bridging_at": row.get("bridging_at"),
        "confirmed_at": row.get("confirmed_at"), "failed_at": row.get("failed_at"),
        "attention_at": row.get("attention_at"), "held_until": held_until,
    }


async def held_units(db, user_id: str) -> int:
    """pUSD that this user's withdrawals still hold in the wallet."""
    return int(await db.fetchval(_HELD_SQL, (user_id,)) or 0)


class Withdrawer:
    """Runs withdrawals for the API and moves open ones forward.

    `client_for(user_row)` returns the user's AsyncSecureClient (main.py passes
    the shared per-user cache), `chain` is a PolygonReader, `bridge` a
    BridgeClient, `notify(event)` queues a Telegram alert, `risk_lock` is the
    engine's per-user KeyedLocks, and `on_funds_moved(user_id)` drops the
    engine's cached balance for that user.
    """

    def __init__(self, db, *, client_for, chain, bridge, notify=None, risk_lock=None,
                 on_funds_moved=None, limits: Limits | None = None, enabled: bool = True,
                 allowlist: frozenset = frozenset(), environment=None,
                 clock=time.time, sleep=asyncio.sleep) -> None:
        from polymarket.environments import PRODUCTION
        self.db = db
        self.client_for = client_for
        self.chain = chain
        self.bridge = bridge
        self.notify = notify
        self.risk_lock = risk_lock
        self.on_funds_moved = on_funds_moved
        self.limits = limits or Limits.from_config()
        self.enabled = bool(enabled)
        self.allowlist = frozenset(int(x) for x in allowlist)
        self._protocol = protocol_addresses(environment or PRODUCTION)
        self._clock = clock
        self._sleep = sleep
        # Withdrawals this process is moving right now; the reconciler leaves
        # them alone. After a restart the set is empty, so it takes them all.
        self._busy: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        # When each 'attention' withdrawal is next checked for a late delivery.
        self._recheck_at: dict[str, float] = {}
        # Set once the relayer refuses on policy: no send after that.
        self.halted: str | None = None

    # --- availability -------------------------------------------------------
    def offered_to(self, user: dict) -> bool:
        """Whether this account gets the feature at all (the Mini App hides it
        otherwise)."""
        telegram = user.get("telegram_user_id")
        return bool(self.enabled and telegram
                    and (not self.allowlist or int(telegram) in self.allowlist)
                    and str(user.get("id") or "") != str(user.get("signer_address") or ""))

    async def paused_reason(self) -> str | None:
        if self.halted:
            return "Withdrawals are paused while PolyTrade checks the relayer."
        stuck = await self.db.fetchval(
            "SELECT COUNT(*) FROM withdrawals WHERE status='attention'")
        if int(stuck or 0) >= ATTENTION_BRAKE:
            return "Withdrawals are paused while PolyTrade checks two bridge transfers."
        return None

    async def _require_available(self, user: dict) -> None:
        if not self.enabled:
            raise WithdrawRefused(503, "Withdrawals are switched off.")
        if not user.get("telegram_user_id"):
            raise WithdrawRefused(403, "Open PolyTrade from the Telegram bot to withdraw.")
        if not self.offered_to(user):
            raise WithdrawRefused(403, "Withdrawals are not open for this account yet.")
        reason = await self.paused_reason()
        if reason:
            raise WithdrawRefused(503, reason)

    # --- reads ----------------------------------------------------------------
    async def _minimum_units(self) -> int:
        try:
            bridge_minimum = await self.bridge.minimum_units()
        except BridgeError as exc:
            log.warning("withdraw: bridge minimum unavailable (%s)", exc)
            raise WithdrawRefused(502, "The bridge is not answering. Try again shortly.") from None
        if bridge_minimum is None:
            raise WithdrawRefused(503, "The bridge does not list USDC on Polygon right now.")
        return max(self.limits.floor_units, bridge_minimum)

    async def _deposit_client(self, user: dict):
        client = await self.client_for(user)
        if (getattr(client, "wallet_type", None) != "DEPOSIT_WALLET"
                or str(getattr(client, "wallet", "")).lower() != str(user["id"]).lower()):
            raise WithdrawRefused(409, "Withdrawals need this account's gasless deposit wallet.")
        return client

    async def _spendable_units(self, user: dict, client) -> int:
        try:
            onchain = await self.chain.erc20_balance(PUSD, user["id"])
            clob = int((await client.get_balance_allowance(asset_type="COLLATERAL")).balance)
        except Exception as exc:
            log.warning("withdraw: balance read failed for %s (%s)", str(user["id"])[:10],
                        type(exc).__name__)
            raise WithdrawRefused(503, "Your balance could not be read right now. "
                                       "Try again shortly.") from None
        return min(onchain, clob)

    async def _deductions(self, store, user_id: str) -> dict:
        reserved = await store.fetchval(
            "SELECT COALESCE(SUM(reserved_usd),0) FROM copy_open_claims WHERE user_id=? "
            "AND state IN ('reserved','submitting','uncertain')", (user_id,))
        # BUY fills carry their spend and no pnl ('open', and 'partial' when
        # the position grew); SELL partials carry a pnl and no amount.
        settling = await store.fetchval(
            "SELECT COALESCE(SUM(amount_usd),0) FROM trade_events WHERE user_id=? AND ts >= ? "
            "AND event_type IN ('open','partial') AND pnl IS NULL",
            (user_id, _iso(self._clock() - SETTLING_SECONDS)))
        held = await store.fetchval(_HELD_SQL, (user_id,))
        return {"reserved": _usd_to_units_up(reserved), "settling": _usd_to_units_up(settling),
                "held": int(held or 0)}

    @staticmethod
    def _free(spendable: int, deductions: dict) -> int:
        free = max(0, int(spendable) - sum(deductions.values()))
        return free - free % CENT_UNITS

    async def _usage(self, store, user_id: str) -> tuple[int, int]:
        row = await store.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_units),0) AS total FROM withdrawals "
            "WHERE user_id=? AND status NOT IN ('failed','refunded') AND requested_at >= ?",
            (user_id, _iso(self._clock() - 86400)))
        return int(row["n"] or 0), int(row["total"] or 0)

    async def _row(self, withdrawal_id: str) -> dict | None:
        return await self.db.fetchone("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,))

    async def summary(self, user: dict) -> dict:
        """What the Withdraw panel shows: availability, the free balance and
        its parts, the limits and what is left of them today, recent rows."""
        offered = self.offered_to(user)
        out: dict = {"enabled": offered, "asset": "USDC", "chain": "Polygon",
                     "paused": None, "balance": None, "limits": None, "open": None,
                     "recent": []}
        if not offered:
            return out
        user_id = user["id"]
        rows = await self.db.fetchall(
            "SELECT * FROM withdrawals WHERE user_id=? ORDER BY requested_at DESC LIMIT 10",
            (user_id,))
        out["recent"] = [public(r) for r in rows]
        out["open"] = next((public(r) for r in rows if r["status"] in OPEN), None)
        out["paused"] = await self.paused_reason()
        count, total = await self._usage(self.db, user_id)
        try:
            minimum = await self._minimum_units()
        except WithdrawRefused:
            minimum = self.limits.floor_units
        out["limits"] = {
            "min": usd(minimum), "max": usd(self.limits.max_units),
            "daily_max": usd(self.limits.daily_units), "daily_count": self.limits.daily_count,
            "left_today": usd(max(0, self.limits.daily_units - total)),
            "count_left_today": max(0, self.limits.daily_count - count)}
        try:
            client = await self._deposit_client(user)
            spendable = await self._spendable_units(user, client)
        except WithdrawRefused as exc:
            out["balance_error"] = exc.message
            return out
        deductions = await self._deductions(self.db, user_id)
        out["balance"] = {
            "cash": usd(spendable), "reserved_for_orders": usd(deductions["reserved"]),
            "settling": usd(deductions["settling"]), "in_progress": usd(deductions["held"]),
            "available": usd(self._free(spendable, deductions))}
        return out

    async def get(self, user: dict, withdrawal_id: str) -> dict | None:
        row = await self.db.fetchone(
            "SELECT * FROM withdrawals WHERE id=? AND user_id=?", (withdrawal_id, user["id"]))
        return public(row) if row else None

    # --- checks shared by preview and request -----------------------------
    async def _validated(self, user: dict, amount, destination) -> tuple[int, str]:
        units = parse_amount(amount)
        dest = normalize_address(destination)
        refusal = destination_refusal(dest, user, self._protocol)
        if refusal:
            raise WithdrawRefused(400, refusal)
        # Another account's deposit wallet or signer: native USDC sent there sits
        # beside its pUSD and never shows up as cash.
        if await self.db.fetchval(
                "SELECT COUNT(*) FROM users WHERE lower(id)=? OR lower(signer_address)=?",
                (dest.lower(), dest.lower())):
            raise WithdrawRefused(400, "That is a PolyTrade wallet, and USDC sent there does not "
                                       "show up as cash. Enter an address outside PolyTrade.")
        minimum = await self._minimum_units()
        if units < minimum:
            raise WithdrawRefused(400, f"The minimum withdrawal is ${usd(minimum)}.")
        if units > self.limits.max_units:
            raise WithdrawRefused(400, f"The most you can withdraw at once is "
                                       f"${usd(self.limits.max_units)}.")
        return units, dest

    def _check_daily(self, units: int, count: int, total: int) -> None:
        if count >= self.limits.daily_count:
            raise WithdrawRefused(429, f"You can make {self.limits.daily_count} withdrawals "
                                       "in 24 hours. Try again later.")
        if total + units > self.limits.daily_units:
            left = max(0, self.limits.daily_units - total)
            raise WithdrawRefused(400, f"That is over the ${usd(self.limits.daily_units)} "
                                       f"daily limit. ${usd(left)} is left for the next 24 hours.")

    async def _quote(self, units: int, dest: str) -> Quote:
        try:
            quote = await self.bridge.quote(units, dest)
        except BridgeError as exc:
            log.warning("withdraw: bridge quote unavailable (%s)", exc)
            raise WithdrawRefused(502, "The bridge is not answering. Try again shortly.") from None
        refusal = quote_refusal(units, quote)
        if refusal:
            log.warning("withdraw: quote refused for $%s: estimate %s, minimum %s",
                        usd(units), usd(quote.est_out_units), usd(quote.min_out_units))
            raise WithdrawRefused(409, refusal)
        return quote

    async def preview(self, user: dict, amount, destination) -> dict:
        """Every check the request makes, plus a fresh quote. Reserves nothing."""
        await self._require_available(user)
        units, dest = await self._validated(user, amount, destination)
        if await self.db.fetchone(
                "SELECT id FROM withdrawals WHERE user_id=? AND status IN "
                "('requested','submitting','submitted','bridging','attention')", (user["id"],)):
            raise WithdrawRefused(409, "A withdrawal is already in progress.")
        count, total = await self._usage(self.db, user["id"])
        self._check_daily(units, count, total)
        client = await self._deposit_client(user)
        free = self._free(await self._spendable_units(user, client),
                          await self._deductions(self.db, user["id"]))
        if units > free:
            raise WithdrawRefused(400, f"That is more than you can withdraw right now "
                                       f"(${usd(free)} available).")
        quote = await self._quote(units, dest)
        return {"amount": usd(units), "destination": dest, "asset": "USDC", "chain": "Polygon",
                "receive_estimate": usd(quote.est_out_units),
                "receive_minimum": usd(quote.min_out_units), "seconds": quote.est_seconds}

    # --- the request ----------------------------------------------------------
    async def request(self, user: dict, *, amount, destination, confirm_last4: str,
                      idempotency_key: str) -> dict:
        """Validate, reserve, create the bridge address and send. Returns the
        row as `public` shows it; WithdrawRefused when nothing was sent."""
        await self._require_available(user)
        user_id = user["id"]
        key = str(idempotency_key or "")
        if not _IDEMPOTENCY_KEY.match(key):
            raise WithdrawRefused(400, "Missing confirmation key. Reopen the confirm screen.")
        units = parse_amount(amount)
        dest = normalize_address(destination)
        replay = await self.db.fetchone(
            "SELECT * FROM withdrawals WHERE user_id=? AND idempotency_key=?", (user_id, key))
        if replay:
            if int(replay["amount_units"]) != units or replay["destination"] != dest:
                raise WithdrawRefused(409, "This confirmation was already used for a "
                                           "different withdrawal.")
            return public(replay)
        if str(confirm_last4 or "").strip().lower() != dest[-4:].lower():
            raise WithdrawRefused(400, "The last 4 characters do not match the address.")
        units, dest = await self._validated(user, amount, destination)
        quote = await self._quote(units, dest)
        client = await self._deposit_client(user)

        handle = None
        async with self._user_lock(user_id):
            spendable = await self._spendable_units(user, client)
            try:
                head = await self.chain.block_number()
            except ChainReadError as exc:
                log.warning("withdraw: head block unavailable (%s)", exc)
                raise WithdrawRefused(503, "Polygon could not be read right now. "
                                           "Try again shortly.") from None
            row, created = await self._reserve(user_id, key, units, dest, quote, spendable, head)
            if not created:
                return public(row)          # a concurrent replay of the same key
            self._busy.add(row["id"])
            try:
                self._funds_moved(user_id)
                log.info("WITHDRAW REQUESTED %s $%s -> %s id=%s quote=%s", user_id[:10],
                         usd(units), short(dest), row["id"][:8], quote.quote_id[:12])
                await self._alert("requested", row)
                row = await self._prepare_bridge(row)
                if row["status"] != "requested":
                    return public(row)
                row, handle = await self._send(row, client)
            finally:
                if handle is None:
                    self._busy.discard(row["id"])
        if handle is not None:
            self._tasks[row["id"]] = asyncio.create_task(self._follow(row["id"], handle))
        return public(row)

    def _user_lock(self, user_id: str):
        hold = getattr(self.risk_lock, "hold", None)
        if hold is not None:
            return hold(user_id)
        return self.risk_lock if self.risk_lock is not None else _NoLock()

    async def _reserve(self, user_id: str, key: str, units: int, dest: str, quote: Quote,
                       spendable: int, head: int) -> tuple[dict, bool]:
        """Insert the withdrawal if every cap allows it. Returns (row, created);
        created is False when the same key won a race and its row is returned."""
        withdrawal_id = uuid.uuid4().hex
        now = self._now()
        try:
            async with self.db.transaction(write=True) as tx:
                user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if self.db.is_pg else "")
                await tx.fetchone(user_sql, (user_id,))
                if await tx.fetchone(
                        "SELECT id FROM withdrawals WHERE user_id=? AND status IN "
                        "('requested','submitting','submitted','bridging','attention')",
                        (user_id,)):
                    raise WithdrawRefused(409, "A withdrawal is already in progress.")
                count, total = await self._usage(tx, user_id)
                self._check_daily(units, count, total)
                free = self._free(spendable, await self._deductions(tx, user_id))
                if units > free:
                    raise WithdrawRefused(400, f"That is more than you can withdraw right now "
                                               f"(${usd(free)} available).")
                await tx.execute(
                    "INSERT INTO withdrawals(id,user_id,idempotency_key,amount_units,destination,"
                    "to_chain_id,to_token,status,quote_id,quoted_out_units,min_out_units,"
                    "submit_block,requested_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'requested',?,?,?,?,?,?)",
                    (withdrawal_id, user_id, key, units, dest, POLYGON, USDC, quote.quote_id,
                     quote.est_out_units, quote.min_out_units, head, now, now))
        except aiosqlite.IntegrityError:
            # Lost a race: the same key from a double tap, or another open
            # withdrawal committed first.
            existing = await self.db.fetchone(
                "SELECT * FROM withdrawals WHERE user_id=? AND idempotency_key=?", (user_id, key))
            if existing and int(existing["amount_units"]) == units and existing["destination"] == dest:
                return existing, False
            raise WithdrawRefused(409, "A withdrawal is already in progress.") from None
        return await self._row(withdrawal_id), True

    async def _prepare_bridge(self, row: dict) -> dict:
        """Create the bridge address, check it, record its baseline. Any doubt
        fails the withdrawal before a cent moves."""
        try:
            address = await self.bridge.create_address(wallet=row["user_id"],
                                                       recipient=row["destination"])
            refusal = destination_refusal(address, {"id": row["user_id"]}, self._protocol)
            if refusal or address.lower() == row["destination"].lower():
                raise BridgeError("bridge returned an address we will not send to")
            baseline = len(await self.bridge.transactions(address))
        except BridgeError as exc:
            log.warning("withdraw %s: bridge address step failed (%s)", row["id"][:8], exc)
            await self._fail(row, ("requested",), "Not sent: the bridge did not provide a "
                                                  "usable address. Try again shortly.")
            return await self._row(row["id"])
        await self.db.execute(
            "UPDATE withdrawals SET bridge_address=?,bridge_baseline=?,updated_at=? "
            "WHERE id=? AND status='requested'", (address, baseline, self._now(), row["id"]))
        return await self._row(row["id"])

    async def _send(self, row: dict, client):
        """requested -> submitting -> submitted. Returns (row, handle); handle
        is None when nothing was accepted or the outcome is unknown."""
        if not await self._move(row["id"], ("requested",), "submitting"):
            return await self._row(row["id"]), None
        call = erc20_transfer_call(token_address=PUSD, recipient=row["bridge_address"],
                                   amount=int(row["amount_units"]))
        give_up_at = self._clock() + BUSY_RETRY_SECONDS
        while True:
            try:
                handle = await client.execute_transaction(
                    calls=[call], metadata="PolyTrade withdrawal")
                break
            except pm_errors.UserInputError as exc:
                # Refused by the SDK before anything was sent, and it would
                # refuse every wallet the same way.
                self._halt(f"the SDK refused the withdrawal transfer ({redact(exc)})")
                await self._fail(row, ("submitting",), "Not sent: withdrawals are paused.")
                return await self._row(row["id"]), None
            except pm_errors.RateLimitError:
                # The relayer throttled the submit (after the SDK's own
                # retries): nothing was accepted.
                if self._clock() < give_up_at:
                    await self._sleep(BUSY_RETRY_DELAY)
                    continue
                await self._fail(row, ("submitting",), "Not sent: the relayer is busy. "
                                                       "Try again in a minute.")
                return await self._row(row["id"]), None
            except pm_errors.RequestRejectedError as exc:
                if _BUSY.search(str(exc)) and self._clock() < give_up_at:
                    await self._sleep(BUSY_RETRY_DELAY)
                    continue
                if exc.status in (401, 403) or _POLICY.search(str(exc)):
                    self._halt(f"relayer refused the withdrawal transfer "
                               f"({exc.status}: {redact(exc)})")
                    await self._fail(row, ("submitting",), "Not sent: withdrawals are paused.")
                elif _BUSY.search(str(exc)):
                    await self._fail(row, ("submitting",), "Not sent: the wallet is busy with "
                                                           "another transaction. Try again in "
                                                           "a minute.")
                else:
                    await self._fail(row, ("submitting",), "Not sent: the relayer refused the "
                                                           "transfer.")
                    log.warning("withdraw %s: relayer refused (%s: %s)", row["id"][:8],
                                exc.status, redact(exc))
                return await self._row(row["id"]), None
            except Exception as exc:
                # The submit may have reached the relayer. Only the chain can
                # tell; the reconciler asks it.
                log.warning("WITHDRAW OUTCOME UNKNOWN %s id=%s (%s: %s); held until the chain "
                            "shows it", row["user_id"][:10], row["id"][:8], type(exc).__name__,
                            redact(exc))
                return await self._row(row["id"]), None
        tx_hash = getattr(handle, "transaction_hash", None)
        await self._move(row["id"], ("submitting",), "submitted",
                         relayer_tx_id=str(getattr(handle, "transaction_id", "") or "")[:120],
                         tx_hash=str(tx_hash) if tx_hash else None)
        log.info("WITHDRAW SUBMITTED %s id=%s relayer=%s tx=%s", row["user_id"][:10],
                 row["id"][:8], getattr(handle, "transaction_id", None), tx_hash)
        return await self._row(row["id"]), handle

    async def _follow(self, withdrawal_id: str, handle) -> None:
        """Wait for the relayer, then keep moving the withdrawal forward until
        it is confirmed, failed or needs attention."""
        try:
            try:
                outcome = await handle.wait()
                tx_hash = getattr(outcome, "transaction_hash", None)
                if tx_hash:
                    await self._annotate(withdrawal_id, tx_hash=str(tx_hash))
            except pm_errors.TransactionFailedError as exc:
                log.warning("withdraw %s: relayer reports the transfer failed (%s); held until "
                            "the batch expires", withdrawal_id[:8], redact(exc))
                await self._annotate(withdrawal_id, error=f"Relayer: {redact(exc)[:200]}")
            except Exception as exc:
                log.warning("withdraw %s: relayer wait ended without an answer (%s)",
                            withdrawal_id[:8], type(exc).__name__)
            stop_at = self._clock() + BATCH_EXPIRY_SECONDS + self.limits.bridge_timeout
            while self._clock() < stop_at:
                row = await self._row(withdrawal_id)
                if row is None or row["status"] not in _IN_PROGRESS:
                    return
                await self._advance(row)
                row = await self._row(withdrawal_id)
                if row is None or row["status"] not in _IN_PROGRESS:
                    return
                await self._sleep(STATUS_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("withdraw %s: follow-up failed; the reconciler continues",
                          withdrawal_id[:8])
        finally:
            self._busy.discard(withdrawal_id)
            self._tasks.pop(withdrawal_id, None)

    # --- moving open withdrawals forward -------------------------------------
    async def reconcile(self) -> None:
        """One pass over every open withdrawal no task here is moving, and over
        the 'attention' ones still waiting on a delivery the bridge has not
        reported failed."""
        rows = await self.db.fetchall(
            "SELECT * FROM withdrawals WHERE status IN "
            "('requested','submitting','submitted','bridging') "
            "OR (status='attention' AND delivery_tx_hash IS NULL AND bridge_address IS NOT NULL "
            "AND COALESCE(bridge_status,'')<>'FAILED') ORDER BY requested_at")
        for row in rows:
            if row["id"] in self._busy or not self._recheck_due(row):
                continue
            try:
                await self._advance(row)
            except Exception:
                log.exception("withdraw %s: reconcile step failed", row["id"][:8])

    def _recheck_due(self, row: dict) -> bool:
        """Every pass for the ones in progress; an 'attention' one every
        ATTENTION_RECHECK_SECONDS during its first day there."""
        if row["status"] != "attention":
            return True
        if self._age(row.get("attention_at") or row["updated_at"]) > ATTENTION_RECHECK_FOR_SECONDS:
            return False
        now = self._clock()
        if now < self._recheck_at.get(row["id"], 0.0):
            return False
        self._recheck_at[row["id"]] = now + ATTENTION_RECHECK_SECONDS
        return True

    async def _advance(self, row: dict) -> None:
        status = row["status"]
        if status == "requested":
            if self._age(row["requested_at"]) > STALE_REQUEST_SECONDS:
                await self._fail(row, ("requested",), "Not sent: the server stopped before "
                                                      "sending. Nothing left your wallet.")
        elif status in ("submitting", "submitted"):
            await self._check_send(row)
        elif status in ("bridging", "attention"):
            await self._check_bridge(row)

    async def _check_send(self, row: dict) -> None:
        wallet, bridge, units = row["user_id"], row.get("bridge_address"), int(row["amount_units"])
        if not bridge:
            await self._fail(row, ("submitting", "submitted"), "Not sent.")
            return
        if row.get("tx_hash"):
            receipt = await self.chain.receipt(row["tx_hash"])
            if sent_to_bridge(receipt, wallet, bridge, units):
                await self._mark_bridging(row, row["tx_hash"])
                return
        found = await self._find_transfer(row)
        if found:
            await self._mark_bridging(row, found)
            return
        if self._age(row.get("submitting_at") or row["requested_at"]) > BATCH_EXPIRY_SECONDS:
            await self._fail(row, ("submitting", "submitted"),
                             "Not sent: the transfer never executed. Nothing left your wallet.")

    async def _find_transfer(self, row: dict) -> str | None:
        """A confirmed pUSD transfer of exactly this amount from the wallet to
        the bridge address since the submit block, by its transaction hash."""
        start = int(row.get("submit_block") or 0)
        head = await self.chain.block_number()
        if start <= 0:
            start = max(0, head - LOG_SEARCH_BLOCKS)
        logs = await self.chain.transfer_logs(PUSD, row["user_id"], row["bridge_address"],
                                              start - 5, min(head, start + LOG_SEARCH_BLOCKS))
        for entry in logs:
            data = str(entry.get("data") or "0x")
            if (len(data) > 2 and int(data, 16) == int(row["amount_units"])
                    and entry.get("transactionHash")):
                tx_hash = str(entry["transactionHash"])
                receipt = await self.chain.receipt(tx_hash)
                if sent_to_bridge(receipt, row["user_id"], row["bridge_address"],
                                  int(row["amount_units"])):
                    return tx_hash
        return None

    async def _mark_bridging(self, row: dict, tx_hash: str) -> None:
        if await self._move(row["id"], ("submitting", "submitted"), "bridging",
                            tx_hash=tx_hash, error=None):
            self._funds_moved(row["user_id"])
            log.info("WITHDRAW BRIDGING %s id=%s $%s left the wallet tx=%s",
                     row["user_id"][:10], row["id"][:8], usd(row["amount_units"]), tx_hash)
            await self._alert("bridging", await self._row(row["id"]))

    async def _check_bridge(self, row: dict) -> None:
        """Follow the bridge for a 'bridging' withdrawal, or for an 'attention'
        one it had not finished in time: a delivery proven on-chain confirms
        either."""
        units = int(row["amount_units"])
        try:
            transactions = await self.bridge.transactions(row["bridge_address"])
        except BridgeError as exc:
            log.warning("withdraw %s: bridge status unavailable (%s)", row["id"][:8], exc)
            transactions = []
        match = match_transfer(transactions, int(row.get("bridge_baseline") or 0), units)
        if match:
            state = str(match.get("status") or "").upper()[:40]
            if state != (row.get("bridge_status") or ""):
                await self._annotate(row["id"], bridge_status=state)
            if state == "COMPLETED":
                found = await self._delivery(row, str(match.get("txHash") or ""))
                if found:
                    await self._settle_delivery(row, *found)
                    return
            elif state == "FAILED":
                await self._attention(row, "The bridge reported the transfer as failed.")
                return
        if (row["status"] == "bridging"
                and self._age(row.get("bridging_at") or row["requested_at"]) > self.limits.bridge_timeout):
            minutes = round(self.limits.bridge_timeout / 60)
            await self._attention(row, f"The bridge has not delivered after {minutes} minutes.")

    async def _delivery(self, row: dict, bridge_tx: str) -> tuple[str, dict] | None:
        """The delivery transaction and its receipt. The bridge's txHash counts
        when it is a Polygon transaction paying the destination; the ones it
        reported on 2026-10-22 were no Polygon transactions at all, so the
        payment is otherwise looked up on-chain."""
        if _BYTES32.match(bridge_tx):
            receipt = await self.chain.receipt(bridge_tx)
            if delivered_to(receipt, row["destination"]) > 0:
                return bridge_tx, receipt
        return await self._find_delivery(row)

    async def _find_delivery(self, row: dict) -> tuple[str, dict] | None:
        """A native USDC transfer to the destination after the submit block
        that can be this withdrawal's delivery: at least the quoted minimum, at
        most the amount plus 1%, in a transaction no other withdrawal claims."""
        start = int(row.get("submit_block") or 0)
        if start <= 0:
            return None
        head = await self.chain.block_number()
        units = int(row["amount_units"])
        minimum = int(row.get("min_out_units") or units)
        logs = await self.chain.transfer_logs(USDC, None, row["destination"], start,
                                              min(head, start + DELIVERY_SEARCH_BLOCKS))
        for entry in logs:
            data = str(entry.get("data") or "0x")
            amount = int(data, 16) if len(data) > 2 else 0
            tx_hash = str(entry.get("transactionHash") or "")
            if not minimum <= amount <= units + units // 100 or not _BYTES32.match(tx_hash):
                continue
            if await self.db.fetchval(
                    "SELECT COUNT(*) FROM withdrawals WHERE delivery_tx_hash=? AND id<>?",
                    (tx_hash, row["id"])):
                continue
            receipt = await self.chain.receipt(tx_hash)
            if delivered_to(receipt, row["destination"]) >= minimum:
                return tx_hash, receipt
        return None

    async def _settle_delivery(self, row: dict, tx_hash: str, receipt: dict) -> None:
        got = delivered_to(receipt, row["destination"])
        minimum = int(row.get("min_out_units") or row["amount_units"])
        if got <= 0 or got < minimum:
            log.critical("WITHDRAW DELIVERY SHORT %s id=%s delivered %s USDC, quoted minimum %s, "
                         "tx=%s", row["user_id"][:10], row["id"][:8], usd(got), usd(minimum),
                         tx_hash)
            reason = (f"The bridge's delivery paid {usd(got)} USDC, below the quoted minimum "
                      f"of {usd(minimum)}.")
            if row["status"] == "attention":
                # Recorded, so the late-delivery check leaves it to the operator.
                await self._annotate(row["id"], error=reason, delivery_tx_hash=tx_hash,
                                     delivered_units=got)
            else:
                await self._attention(row, reason, delivery_tx_hash=tx_hash, delivered_units=got)
            return
        late = row["status"] == "attention"
        extra = {"operator_note": "delivery found after the bridge timeout"} if late else {}
        if await self._move(row["id"], (row["status"],), "confirmed",
                            delivery_tx_hash=tx_hash, delivered_units=got, error=None, **extra):
            log.info("WITHDRAW CONFIRMED %s id=%s %s USDC -> %s tx=%s%s", row["user_id"][:10],
                     row["id"][:8], usd(got), short(row["destination"]), tx_hash,
                     " (after attention)" if late else "")
            await self._alert("confirmed", await self._row(row["id"]))

    # --- state changes ---------------------------------------------------------
    async def _move(self, withdrawal_id: str, from_statuses: tuple, to_status: str,
                    **fields) -> bool:
        """Compare-and-set: moves only from one of `from_statuses`."""
        now = self._now()
        values = {**fields, "status": to_status, "updated_at": now}
        if to_status in _STAMPS:
            values[_STAMPS[to_status]] = now
        sets = ",".join(f"{column}=?" for column in values)
        marks = ",".join("?" for _ in from_statuses)
        changed = await self.db.execute(
            f"UPDATE withdrawals SET {sets} WHERE id=? AND status IN ({marks})",
            (*values.values(), withdrawal_id, *from_statuses))
        return changed == 1

    async def _annotate(self, withdrawal_id: str, **fields) -> None:
        values = {**fields, "updated_at": self._now()}
        sets = ",".join(f"{column}=?" for column in values)
        await self.db.execute(f"UPDATE withdrawals SET {sets} WHERE id=?",
                              (*values.values(), withdrawal_id))

    async def _fail(self, row: dict, from_statuses: tuple, reason: str) -> None:
        if await self._move(row["id"], from_statuses, "failed", error=reason[:300]):
            log.warning("WITHDRAW FAILED %s id=%s $%s -> %s: %s", row["user_id"][:10],
                        row["id"][:8], usd(row["amount_units"]), short(row["destination"]), reason)
            await self._alert("failed", await self._row(row["id"]))

    async def _attention(self, row: dict, reason: str, **fields) -> None:
        if await self._move(row["id"], ("bridging",), "attention", error=reason[:300], **fields):
            log.error("WITHDRAW ATTENTION %s id=%s $%s -> %s bridge=%s tx=%s: %s. Follow up with "
                      "the bridge provider, then resolve it with python -m backend.core.withdraw",
                      row["user_id"][:10], row["id"][:8], usd(row["amount_units"]),
                      short(row["destination"]), short(row.get("bridge_address")),
                      row.get("tx_hash"), reason)
            await self._alert("attention", await self._row(row["id"]))

    def _halt(self, reason: str) -> None:
        self.halted = reason
        log.error("WITHDRAW HALTED: %s. No further withdrawals in this process; fix the cause "
                  "or set WITHDRAWALS_ENABLED=0, then restart.", reason)

    def _funds_moved(self, user_id: str) -> None:
        if self.on_funds_moved is None:
            return
        try:
            self.on_funds_moved(user_id)
        except Exception:
            log.exception("withdraw: could not refresh the engine's balance for %s", user_id[:10])

    async def _alert(self, kind: str, row: dict | None) -> None:
        if self.notify is None or row is None:
            return
        try:
            await self.notify({
                "event": f"withdrawal_{kind}", "user_id": row["user_id"],
                "withdrawal_id": row["id"], "amount_usd": usd(row["amount_units"]),
                "destination": row["destination"], "delivered_usd": usd(row.get("delivered_units")),
                "tx_hash": row.get("delivery_tx_hash") or row.get("tx_hash"),
                "reason": row.get("error")})
        except Exception:
            # The withdrawal's state is already committed; an alert is best-effort.
            log.exception("withdraw %s: alert failed", row["id"][:8])

    def _now(self) -> str:
        return _iso(self._clock())

    def _age(self, value) -> float:
        started = _epoch(value)
        return float("inf") if started is None else self._clock() - started

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


class _NoLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Operator tool
# ---------------------------------------------------------------------------

async def list_withdrawals(db, *, everything: bool = False, out=print) -> int:
    where = "" if everything else (
        "WHERE status IN ('requested','submitting','submitted','bridging','attention') ")
    rows = await db.fetchall(f"SELECT * FROM withdrawals {where}ORDER BY requested_at DESC LIMIT 50")
    if not rows:
        out("no withdrawals" if everything else "no open withdrawals")
        return 0
    for r in rows:
        out(f"{r['id']}  {r['status']:<10} ${usd(r['amount_units']):>9}  user {str(r['user_id'])[:10]}  "
            f"to {short(r['destination'])}  bridge {short(r.get('bridge_address')) or '-'}  "
            f"requested {r['requested_at'][:19]}  send {r.get('tx_hash') or '-'}  "
            f"delivery {r.get('delivery_tx_hash') or '-'}  {r.get('error') or ''}")
    return 0


async def resolve_withdrawal(db, chain, withdrawal_id: str, *, delivered: str | None = None,
                             refunded: str | None = None, yes: bool = False, out=print) -> int:
    """Close a withdrawal in 'attention' once the operator knows what happened:
    delivered (proven by its delivery transaction on Polygon) or refunded."""
    row = await db.fetchone("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,))
    if row is None:
        out("no such withdrawal")
        return 1
    if row["status"] != "attention":
        out(f"not resolved: status is {row['status']}; only 'attention' is resolved by hand")
        return 1
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if delivered:
        if not _BYTES32.match(delivered):
            out("not resolved: --delivered needs a transaction hash")
            return 1
        got = delivered_to(await chain.receipt(delivered), row["destination"])
        if got <= 0:
            out("not resolved: that transaction pays no USDC to this withdrawal's destination")
            return 1
        out(f"delivery verified: {usd(got)} USDC to {short(row['destination'])}")
        if not yes:
            out("dry run; add --yes to mark it confirmed")
            return 0
        changed = await db.execute(
            "UPDATE withdrawals SET status='confirmed',delivery_tx_hash=?,delivered_units=?,"
            "confirmed_at=?,updated_at=?,operator_note=? WHERE id=? AND status='attention'",
            (delivered, got, now, now, "resolved by operator: delivered", withdrawal_id))
    else:
        if not yes:
            out(f"would mark {withdrawal_id} refunded ({refunded}); add --yes to do it")
            return 0
        changed = await db.execute(
            "UPDATE withdrawals SET status='refunded',updated_at=?,operator_note=? "
            "WHERE id=? AND status='attention'", (now, str(refunded)[:300], withdrawal_id))
    if changed != 1:
        out("not resolved: the withdrawal changed meanwhile; list again")
        return 1
    out(f"resolved {withdrawal_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backend.core.withdraw",
                                     description="List withdrawals and resolve stuck ones.")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="show open withdrawals")
    listing.add_argument("--all", action="store_true", help="the latest 50, any status")
    resolving = commands.add_parser("resolve", help="close a withdrawal in 'attention'")
    resolving.add_argument("withdrawal_id")
    outcome = resolving.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--delivered", metavar="TX_HASH",
                         help="the bridge's delivery transaction on Polygon (verified)")
    outcome.add_argument("--refunded", metavar="NOTE", help="what the provider returned, where")
    resolving.add_argument("--yes", action="store_true", help="really do it (default: dry run)")
    return parser


async def main(argv: list[str] | None = None) -> int:
    from backend.config import POLYGON_RPC_URL
    from backend.core.redeem import default_rpc_url
    from backend.db.database import Database

    args = build_parser().parse_args(argv)
    db = Database()
    await db.connect()
    chain = PolygonReader(POLYGON_RPC_URL or default_rpc_url()) if args.command == "resolve" else None
    try:
        if args.command == "list":
            return await list_withdrawals(db, everything=args.all)
        return await resolve_withdrawal(db, chain, args.withdrawal_id, delivered=args.delivered,
                                        refunded=args.refunded, yes=args.yes)
    finally:
        await db.close()
        if chain is not None:
            await chain.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
