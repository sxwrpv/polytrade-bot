"""Redeem resolved winnings back into spendable pUSD.

A copied position that wins does not turn back into cash by itself. The market
resolves, the outcome tokens stay in the custodial deposit wallet as
`redeemable` (the dashboard's "claimable"), and only a redeem transaction turns
them into pUSD. Nothing did that: wallet.py never grants the auto-redeem
operator, because our builder key's relayer rejects it. So spendable balance
never recovered from a win, and the copy engine, which sizes every copy from
that balance, traded smaller the more an account won (review finding E5,
2026-10-18).

How a deposit wallet redeems with polymarket-client 0.6.0 (read from its
source; AsyncSecureClient.redeem_positions builds this same call):

  - Polymarket's collateral adapters (CtfCollateralAdapter for binary markets,
    NegRiskCtfCollateralAdapter for neg-risk ones) expose
    redeemPositions(collateral, parent, conditionId, indexSets). It pulls the
    CALLER's whole YES and NO balance with CTF.safeBatchTransferFrom, redeems
    it on the CTF, and wraps the USDC.e payout into pUSD for the caller.
  - So the wallet must first approve that adapter as an ERC-1155 operator on
    the CTF. The SDK's own trading-approval set includes both adapters;
    wallet.ensure_allowances does not, because order placement never needed
    them. Both adapters are plain contracts, not proxies, and only ever move
    msg.sender's own tokens (verified source on Polygonscan, 2026-10-18), so
    the grant lets the wallet redeem and nothing else. A missing grant rides
    in the same batch as the redeem.
  - The calls run as one gasless relayer transaction signed by the wallet's
    signer: the path ensure_allowances' approvals already take.

Safety, in the order the checks run:

  1. Opt-in. main.py starts this only with REDEEM_AUTOSTART=1.
  2. Deposit wallets only. An EOA "wallet" would broadcast and need gas.
  3. Never a market the copy ledger still manages. A copy_positions row on
     either outcome token in any status but closed/resolved, or any BUY claim
     on one, leaves the market alone until the engine has finished with it.
     Rows already 'resolved' are read, never written: this job turns tokens
     into pUSD and books nothing.
  4. Resolution is proven ON-CHAIN (CTF payoutDenominator > 0) rather than
     taken from the data-api flag, and the payout of what the wallet holds
     on-chain must be positive. Losing tokens are not worth a transaction.
  5. The adapter is picked from two sources that must agree: the data-api's
     negativeRisk and Gamma's negRisk (the SDK's own source for this routing).
  6. One relayer transaction per user per pass, waited to a terminal state.
     The relayer refuses a second action while one is in flight, and a
     redeemed market holds no tokens on-chain, so a repeat pass finds nothing
     to do. That, not bookkeeping, is what makes the job idempotent.
  7. A failed or unconfirmed attempt cools down before it is retried. A relayer
     that refuses the batch on policy ("operator is not in the allowed list")
     halts the job for the rest of the process.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from dataclasses import dataclass

import httpx
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address
from polymarket import errors as pm_errors
from polymarket.calls import ctf_redeem_positions_call, erc1155_set_approval_for_all_call

log = logging.getLogger("redeem")

# pUSD and outcome tokens both carry 6 decimals.
UNITS_PER_USD = 1_000_000
# Below this a market is not worth a relayer transaction.
MIN_PAYOUT_UNITS = 10_000                  # $0.01
# A holding at or under this many shares is dust (the equity snapshot's floor).
DUST_SHARES = 0.01
# Markets redeemed per relayer transaction. The rest wait for the next pass.
MAX_CONDITIONS_PER_TX = 10
# How long a failed or unconfirmed attempt waits before it is tried again.
DEFAULT_RETRY_SECONDS = 6 * 3600.0

# Every copy_positions status except these still belongs to the engine. Listed
# as the settled ones, so a status added later is fenced until proven safe.
_SETTLED_STATUSES = ("closed", "resolved")

# Relayer answers about this wallet or this moment rather than policy. The SDK
# has already retried them; they cool down like any other failure.
_TRANSIENT_REJECTION = re.compile(
    r"wallet busy|in-flight action|does not match on-chain nonce", re.IGNORECASE)
# Answers that will be the same for every wallet: the relayer will not relay
# this call for our builder key. Retrying per user every pass would only spam
# it, so they halt the job instead.
_POLICY_REJECTION = re.compile(
    r"allowed list|allowlist|whitelist|not allowed|not permitted|unauthori[sz]ed|forbidden",
    re.IGNORECASE)

_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


_PAYOUT_DENOMINATOR = _selector("payoutDenominator(bytes32)")
_PAYOUT_NUMERATORS = _selector("payoutNumerators(bytes32,uint256)")
_BALANCE_OF = _selector("balanceOf(address,uint256)")
_IS_APPROVED_FOR_ALL = _selector("isApprovedForAll(address,address)")


# ---------------------------------------------------------------------------
# Contract addresses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Contracts:
    ctf: str                           # ConditionalTokens: balances and payouts
    collateral: str                    # pUSD
    collateral_adapter: str            # redeems binary markets
    neg_risk_collateral_adapter: str   # redeems neg-risk markets


def _environment_config(environment):
    # polymarket-client 0.6.0 exposes contract addresses only through this
    # internal helper: client.environment is the bare Environment, which has
    # no address attributes.
    from polymarket._internal.environment import get_environment_config
    return get_environment_config(environment)


def contracts_for(environment) -> Contracts:
    """The addresses the SDK itself trades and redeems against."""
    cfg = _environment_config(environment)
    return Contracts(
        ctf=cfg.conditional_tokens,
        collateral=cfg.collateral_token,
        collateral_adapter=cfg.collateral_adapter,
        neg_risk_collateral_adapter=cfg.neg_risk_collateral_adapter,
    )


def default_rpc_url() -> str:
    """The SDK's public Polygon endpoint, for when POLYGON_RPC_URL is unset."""
    from polymarket.environments import PRODUCTION
    return _environment_config(PRODUCTION).rpc_url


# ---------------------------------------------------------------------------
# On-chain reads
# ---------------------------------------------------------------------------

class ChainReadError(Exception):
    """An eth_call that produced no usable answer. Never carries the RPC URL,
    which embeds the provider's API key."""


class ChainReader:
    """Read-only Polygon JSON-RPC for the four CTF views this job needs.

    A small client of its own rather than the SDK's internal RPC handle
    (private API) or web3 (the detector's provider is synchronous and would
    block the event loop).
    """

    def __init__(self, rpc_url: str, *, client: httpx.AsyncClient | None = None,
                 timeout: float = 10.0) -> None:
        self._rpc_url = rpc_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._ids = itertools.count(1)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, to: str, data: bytes) -> bytes:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": "eth_call",
                "params": [{"to": to, "data": "0x" + data.hex()}, "latest"]}
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
            raise ChainReadError(f"eth_call failed: {message}")
        result = reply.get("result")
        if not isinstance(result, str) or not result.startswith("0x"):
            raise ChainReadError("eth_call returned no result")
        raw = bytes.fromhex(result[2:])
        if len(raw) < 32:
            # "0x" is what a call to an address without code returns.
            raise ChainReadError("eth_call returned an empty result")
        return raw

    async def _uint(self, to: str, data: bytes) -> int:
        return decode(["uint256"], await self._call(to, data))[0]

    async def payout_denominator(self, ctf: str, condition_id: str) -> int:
        return await self._uint(
            ctf, _PAYOUT_DENOMINATOR + encode(["bytes32"], [_condition_bytes(condition_id)]))

    async def payout_numerator(self, ctf: str, condition_id: str, index: int) -> int:
        return await self._uint(ctf, _PAYOUT_NUMERATORS + encode(
            ["bytes32", "uint256"], [_condition_bytes(condition_id), index]))

    async def balance_of(self, ctf: str, owner: str, token_id: str) -> int:
        return await self._uint(ctf, _BALANCE_OF + encode(
            ["address", "uint256"], [to_checksum_address(owner), int(token_id)]))

    async def is_approved_for_all(self, ctf: str, owner: str, operator: str) -> bool:
        raw = await self._call(ctf, _IS_APPROVED_FOR_ALL + encode(
            ["address", "address"],
            [to_checksum_address(owner), to_checksum_address(operator)]))
        return bool(decode(["bool"], raw)[0])


def _condition_bytes(condition_id: str) -> bytes:
    if not _CONDITION_ID.match(condition_id or ""):
        raise ValueError(f"not a condition id: {condition_id!r}")
    return bytes.fromhex(condition_id[2:])


# ---------------------------------------------------------------------------
# Selection (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """A resolved market the data-api says the wallet can redeem for value."""
    condition_id: str             # lower-case
    neg_risk: bool                # data-api negativeRisk; Gamma must agree
    token_ids: tuple[str, str]    # (outcome 0, outcome 1); "" where unknown
    value_usd: float              # data-api currentValue: ordering hint only
    title: str


def select_candidates(positions, *, blocked_tokens: set[str],
                      blocked_conditions: set[str], cooling: set[str],
                      limit: int = MAX_CONDITIONS_PER_TX) -> tuple[list[Candidate], int]:
    """Group the wallet's resolved, valuable holdings by market and drop every
    market this job must leave alone. Returns (candidates, fenced): fenced
    counts the markets held back because the copy ledger still manages them.

    Pure, so the gate can be tested without a wallet. Everything that passes
    here is still proven on-chain before any transaction is built.
    """
    grouped: dict[str, dict] = {}
    for p in positions:
        if not p.redeemable or p.size <= DUST_SHARES:
            continue
        cid = (p.condition_id or "").lower()
        if not _CONDITION_ID.match(cid):
            continue
        entry = grouped.setdefault(cid, {
            "tokens": ["", ""], "value": 0.0, "neg_risk": set(), "title": p.title,
            "held": set()})
        index = p.outcome_index if p.outcome_index in (0, 1) else None
        if index is not None:
            entry["tokens"][index] = entry["tokens"][index] or p.asset
            if p.opposite_asset:
                entry["tokens"][1 - index] = entry["tokens"][1 - index] or p.opposite_asset
        entry["held"].add(p.asset)
        entry["value"] += max(0.0, float(p.current_value or 0.0))
        entry["neg_risk"].add(bool(p.negative_risk))

    candidates: list[Candidate] = []
    fenced = 0
    for cid, entry in grouped.items():
        if entry["value"] < MIN_PAYOUT_UNITS / UNITS_PER_USD:
            continue          # a losing side: nothing to collect
        tokens = {t for t in entry["tokens"] if t} | entry["held"]
        if cid in blocked_conditions or tokens & blocked_tokens:
            fenced += 1
            continue
        if cid in cooling:
            continue
        if len(entry["neg_risk"]) != 1:
            continue          # the data-api contradicts itself; route nothing
        candidates.append(Candidate(
            condition_id=cid, neg_risk=entry["neg_risk"].pop(),
            token_ids=(entry["tokens"][0], entry["tokens"][1]),
            value_usd=round(entry["value"], 6), title=entry["title"] or ""))
    candidates.sort(key=lambda c: c.value_usd, reverse=True)
    return candidates[:max(0, limit)], fenced


def _is_policy_rejection(exc: pm_errors.RequestRejectedError) -> bool:
    message = str(exc)
    if _TRANSIENT_REJECTION.search(message):
        return False
    return exc.status in (401, 403) or bool(_POLICY_REJECTION.search(message))


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Redemption:
    """A candidate proven on-chain: resolved, held, and paying out."""
    condition_id: str
    adapter: str
    token_ids: tuple[str, str]
    payout_units: int
    title: str


@dataclass
class PassSummary:
    users: int = 0          # wallets whose positions were read
    redeemed: int = 0       # markets redeemed and seen empty on-chain afterwards
    payout_units: int = 0   # pUSD those paid, from the on-chain payout vectors
    fenced: int = 0         # markets the copy ledger still manages
    skipped: int = 0        # markets refused by an on-chain or routing check
    failed: int = 0         # transactions refused, failed or unconfirmed
    halted: str | None = None

    def describe(self) -> str:
        text = (f"{self.users} wallet(s) read, {self.redeemed} market(s) redeemed "
                f"for ${self.payout_units / UNITS_PER_USD:.2f}, {self.fenced} held by "
                f"the copy ledger, {self.skipped} skipped, {self.failed} failed")
        if self.halted:
            text += f"; HALTED: {self.halted}"
        return text


class Redeemer:
    """Holds what one process remembers between passes: cool-downs, adapter
    grants already seen on-chain, and whether the relayer has refused us.

    `client_for(user_row)` returns the user's authenticated AsyncSecureClient
    (main.py passes the shared per-user cache); `chain` is a ChainReader.
    """

    def __init__(self, db, pm, client_for, chain, *,
                 retry_seconds: float = DEFAULT_RETRY_SECONDS,
                 max_per_tx: int = MAX_CONDITIONS_PER_TX,
                 clock=time.monotonic) -> None:
        self.db = db
        self.pm = pm
        self.client_for = client_for
        self.chain = chain
        self.retry_seconds = float(retry_seconds)
        self.max_per_tx = max(1, int(max_per_tx))
        self._clock = clock
        # (user_id, condition_id) -> monotonic time it may be tried again.
        self._cooldown: dict[tuple[str, str], float] = {}
        # (wallet, adapter), lower-case, already approved on-chain.
        self._approved: set[tuple[str, str]] = set()
        # Set once the relayer refuses on policy; no submission after that.
        self.halted: str | None = None

    async def run_pass(self, *, concurrency: int = 1,
                       stop: asyncio.Event | None = None) -> PassSummary:
        """One sweep over every user with a wallet. Best-effort per user: one
        failure never blocks the rest. Up to `concurrency` users at once; one
        user's work is strictly sequential (the relayer allows one in-flight
        action per wallet anyway)."""
        summary = PassSummary(halted=self.halted)
        if self.halted:
            return summary
        users = await self.db.fetchall("SELECT * FROM users")
        gate = asyncio.Semaphore(max(1, int(concurrency)))

        async def one(user) -> None:
            async with gate:
                if self.halted or (stop is not None and stop.is_set()):
                    return
                try:
                    await self._redeem_user(user, summary)
                except Exception:
                    summary.failed += 1
                    log.exception("redeem failed for %s", str(user["id"])[:10])

        await asyncio.gather(*(one(user) for user in users))
        summary.halted = self.halted
        return summary

    # --- per user -----------------------------------------------------------
    async def _redeem_user(self, user, summary: PassSummary) -> None:
        user_id = str(user["id"])
        positions, _complete = await self.pm.get_all_positions(user_id, size_threshold=0)
        summary.users += 1
        # Absence from a truncated page proves nothing, but nothing here acts
        # on absence: only holdings that are present are ever redeemed.
        if not any(p.redeemable and p.size > DUST_SHARES for p in positions):
            return
        blocked_tokens, blocked_conditions = await self._ledger_fences(user_id)
        candidates, fenced = select_candidates(
            positions, blocked_tokens=blocked_tokens,
            blocked_conditions=blocked_conditions, cooling=self._cooling(user_id),
            limit=self.max_per_tx)
        summary.fenced += fenced
        if not candidates:
            return

        client = await self.client_for(user)
        if getattr(client, "wallet_type", None) != "DEPOSIT_WALLET":
            log.warning("redeem skipped for %s: wallet type %s is not a gasless "
                        "deposit wallet", user_id[:10], getattr(client, "wallet_type", None))
            summary.skipped += len(candidates)
            return
        wallet = str(client.wallet)
        if wallet.lower() != user_id.lower():
            log.error("redeem skipped for %s: its client is bound to another wallet",
                      user_id[:10])
            summary.skipped += len(candidates)
            return
        contracts = contracts_for(client.environment)
        markets = await self._closed_markets(client, [c.condition_id for c in candidates])

        redemptions: list[Redemption] = []
        for candidate in candidates:
            proven = await self._prove(user_id, wallet, contracts, candidate,
                                       markets.get(candidate.condition_id))
            if proven is None:
                summary.skipped += 1
            else:
                redemptions.append(proven)
        if not redemptions:
            return
        await self._submit(user_id, client, wallet, contracts, redemptions, summary)

    async def _ledger_fences(self, user_id: str) -> tuple[set[str], set[str]]:
        """Tokens and markets the copy engine still owns for this user."""
        marks = ",".join("?" for _ in _SETTLED_STATUSES)
        rows = await self.db.fetchall(
            "SELECT token_id, condition_id FROM copy_positions "
            f"WHERE user_id = ? AND status NOT IN ({marks})",
            (user_id, *_SETTLED_STATUSES))
        claims = await self.db.fetchall(
            "SELECT token_id FROM copy_open_claims WHERE user_id = ?", (user_id,))
        tokens = {r["token_id"] for r in rows} | {c["token_id"] for c in claims}
        conditions = {(r["condition_id"] or "").lower() for r in rows} - {""}
        return tokens, conditions

    def _cooling(self, user_id: str) -> set[str]:
        now = self._clock()
        for key in [k for k, until in self._cooldown.items() if until <= now]:
            del self._cooldown[key]
        return {cid for (uid, cid) in self._cooldown if uid == user_id}

    def _cool(self, user_id: str, condition_ids) -> None:
        until = self._clock() + self.retry_seconds
        for cid in condition_ids:
            self._cooldown[(user_id, cid)] = until

    async def _closed_markets(self, client, condition_ids: list[str]) -> dict:
        """Gamma's view of each market, keyed by lower-case condition id.
        closed=True, as the SDK's own redeem lookup asks: a market Gamma does
        not list as closed is not redeemed."""
        page = await client.list_markets(
            condition_ids=condition_ids, closed=True,
            page_size=max(1, len(condition_ids))).first_page()
        return {str(m.condition_id).lower(): m for m in page.items
                if getattr(m, "condition_id", None)}

    async def _prove(self, user_id: str, wallet: str, contracts: Contracts,
                     c: Candidate, market) -> Redemption | None:
        """Turn a data-api candidate into a redemption only if the chain agrees:
        resolved, routed consistently, and paying out on what the wallet holds."""
        short = c.condition_id[:12]
        if market is None:
            log.info("redeem skip %s %s: Gamma does not list the market as closed",
                     user_id[:10], short)
            return None
        neg_risk = getattr(getattr(market, "state", None), "neg_risk", None)
        if neg_risk is None or bool(neg_risk) != c.neg_risk:
            log.warning("redeem skip %s %s: negRisk disagrees (data-api %s, Gamma %s)",
                        user_id[:10], short, c.neg_risk, neg_risk)
            return None
        outcomes = getattr(market, "outcomes", None)
        tokens = tuple(
            str(getattr(getattr(outcomes, side, None), "token_id", None) or "")
            for side in ("yes", "no"))
        if not all(t.isdigit() for t in tokens):
            log.warning("redeem skip %s %s: Gamma lists no outcome token ids",
                        user_id[:10], short)
            return None
        if not {t for t in c.token_ids if t} <= set(tokens):
            log.warning("redeem skip %s %s: data-api and Gamma name different tokens",
                        user_id[:10], short)
            return None

        ctf = contracts.ctf
        denominator = await self.chain.payout_denominator(ctf, c.condition_id)
        if denominator == 0:
            log.info("redeem skip %s %s: not resolved on-chain yet", user_id[:10], short)
            return None
        numerators = [await self.chain.payout_numerator(ctf, c.condition_id, i)
                      for i in (0, 1)]
        balances = [await self.chain.balance_of(ctf, wallet, t) for t in tokens]
        # The CTF pays each outcome separately: stake * numerator // denominator.
        payout = sum(b * n // denominator for b, n in zip(balances, numerators))
        if payout < MIN_PAYOUT_UNITS:
            log.info("redeem skip %s %s: on-chain payout $%.4f is below the floor",
                     user_id[:10], short, payout / UNITS_PER_USD)
            return None
        adapter = (contracts.neg_risk_collateral_adapter if neg_risk
                   else contracts.collateral_adapter)
        return Redemption(condition_id=c.condition_id, adapter=adapter,
                          token_ids=tokens, payout_units=payout, title=c.title)

    async def _missing_approvals(self, wallet: str, adapters: set[str],
                                 contracts: Contracts) -> list[str]:
        missing = []
        for adapter in sorted(adapters):
            key = (wallet.lower(), adapter.lower())
            if key in self._approved:
                continue
            if await self.chain.is_approved_for_all(contracts.ctf, wallet, adapter):
                self._approved.add(key)
            else:
                missing.append(adapter)
        return missing

    async def _submit(self, user_id: str, client, wallet: str, contracts: Contracts,
                      redemptions: list[Redemption], summary: PassSummary) -> None:
        approvals = await self._missing_approvals(
            wallet, {r.adapter for r in redemptions}, contracts)
        calls = [erc1155_set_approval_for_all_call(
                     token_address=contracts.ctf, operator=adapter, approved=True)
                 for adapter in approvals]
        calls += [ctf_redeem_positions_call(
                      ctf=r.adapter, collateral=contracts.collateral,
                      condition_id=r.condition_id)
                  for r in redemptions]
        condition_ids = [r.condition_id for r in redemptions]
        if approvals:
            log.info("redeem %s: granting %d collateral adapter approval(s) in the "
                     "same batch", user_id[:10], len(approvals))
        try:
            handle = await client.execute_transaction(
                calls=calls,
                metadata=f"Redeem {len(redemptions)} resolved position(s)")
            outcome = await handle.wait()
        except pm_errors.UserInputError as exc:
            # The SDK refused before anything was sent (no builder key, an EOA
            # wallet, ...). Every user's batch has the same shape, so it would
            # refuse them all the same way.
            summary.failed += 1
            self._halt(f"the SDK refused the redeem batch ({exc})")
            return
        except pm_errors.RequestRejectedError as exc:
            summary.failed += 1
            if _is_policy_rejection(exc):
                self._halt(f"relayer refused the redeem batch ({exc.status}: {exc})")
                return
            self._cool(user_id, condition_ids)
            log.warning("REDEEM REJECTED %s: %d market(s), relayer said %s: %s; retry "
                        "after %.1fh", user_id[:10], len(redemptions), exc.status, exc,
                        self.retry_seconds / 3600)
            return
        except Exception as exc:
            # Failed on-chain, timed out, or the transport broke mid-wait: the
            # outcome may be unknown, so the chain decides on the next attempt.
            summary.failed += 1
            self._cool(user_id, condition_ids)
            log.warning("REDEEM NOT CONFIRMED %s: %d market(s) (%s: %s); retry after "
                        "%.1fh", user_id[:10], len(redemptions), type(exc).__name__,
                        exc, self.retry_seconds / 3600)
            return

        for adapter in approvals:
            self._approved.add((wallet.lower(), adapter.lower()))
        tx = getattr(outcome, "transaction_hash", None)
        for r in redemptions:
            note = ""
            try:
                left = [await self.chain.balance_of(contracts.ctf, wallet, t)
                        for t in r.token_ids]
            except Exception as exc:
                # The relayer confirmed the transaction; only the re-read failed.
                left, note = [], f" (on-chain re-check unavailable: {exc})"
            if any(left):
                # Usually an RPC node behind the relayer's confirmation. Never
                # resubmitted at once: the chain is re-read after the cool-down.
                self._cool(user_id, [r.condition_id])
                summary.skipped += 1
                log.warning("redeem tx %s confirmed but %s still holds %s's tokens on "
                            "this RPC; re-checked after %.1fh", tx, user_id[:10],
                            r.condition_id[:12], self.retry_seconds / 3600)
                continue
            summary.redeemed += 1
            summary.payout_units += r.payout_units
            log.info("REDEEMED %s %s +$%.2f pUSD tx=%s (%s)%s", user_id[:10],
                     r.condition_id[:12], r.payout_units / UNITS_PER_USD, tx,
                     r.title[:60], note)

    def _halt(self, reason: str) -> None:
        self.halted = reason
        log.error("REDEEM HALTED: %s. No further redeems in this process; set "
                  "REDEEM_AUTOSTART=0 or get these calls allowed for the builder "
                  "key, then restart.", reason)
