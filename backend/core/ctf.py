"""Outcome-share balances read straight from the chain.

Before the copy engine may release an uncertain BUY claim it needs evidence
that does not come from the data-api indexer: elapsed time plus indexer absence
is not proof that nothing filled (44a5b7d), because one "rejected" order filled
anyway. Every outcome share, standard and negative-risk markets alike, is an
ERC-1155 token on the ConditionalTokens (CTF) contract, so
balanceOf(deposit_wallet, token_id) is what the wallet really holds.

The contract address and chain id come from the pinned polymarket-client SDK's
production environment: the contract the SDK approves for SELLs, i.e. the
ledger the exchange settles against. Calls are plain JSON-RPC over
POLYGON_RPC_URL, and a balance is always read at one named block so the caller
can say WHEN the wallet held it.

Every failure raises CtfError, whose message never carries the RPC URL
(provider URLs embed API keys). Callers treat CtfError as "no evidence" and
keep the fence.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import httpx

# ERC-1155 balanceOf(address,uint256)
BALANCE_OF_SELECTOR = "00fdd58e"
# Outcome shares carry 6 decimals, like pUSD (OrderFilled amounts are 1e6 too).
SHARE_UNITS = 1_000_000


class CtfError(RuntimeError):
    """The chain could not answer. Treat it as no evidence."""


@dataclass(frozen=True)
class ChainHead:
    number: int
    timestamp: int        # unix seconds the block was mined


@dataclass(frozen=True)
class ChainBalance:
    shares: float         # outcome shares held as of `head`
    head: ChainHead


def sdk_ctf_contract() -> tuple[str, int]:
    """(ConditionalTokens address, chain id) of the SDK's production environment."""
    # client.environment carries only a name; the addresses sit behind the
    # SDK's own config accessor. requirements.txt pins the SDK exactly and a
    # test asserts this address, so a release that moves it fails the suite
    # instead of silently pointing the evidence at another ledger.
    from polymarket._internal.environment import get_environment_config
    from polymarket.environments import PRODUCTION
    config = get_environment_config(PRODUCTION)
    return config.conditional_tokens, int(config.chain_id)


class CtfReader:
    """Reads ConditionalTokens balances over JSON-RPC."""

    def __init__(self, rpc_url: str, *, contract: str | None = None,
                 chain_id: int | None = None, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not rpc_url:
            raise ValueError("an RPC URL is required")
        if contract is None or chain_id is None:
            sdk_contract, sdk_chain = sdk_ctf_contract()
            contract = contract or sdk_contract
            chain_id = chain_id or sdk_chain
        self.contract = _address(contract)
        self.chain_id = int(chain_id)
        self._url = rpc_url
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._ids = itertools.count(1)
        self._verified = False

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": next(self._ids),
                   "method": method, "params": params}
        try:
            response = await self._http.post(self._url, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise CtfError(f"{method}: HTTP {exc.response.status_code}") from None
        except Exception as exc:      # the exception text may carry the URL
            raise CtfError(f"{method}: {type(exc).__name__}") from None
        if not isinstance(body, dict):
            raise CtfError(f"{method}: malformed response")
        error = body.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            raise CtfError(f"{method}: RPC error {code}")
        if "result" not in body:
            raise CtfError(f"{method}: response has no result")
        return body["result"]

    async def _verify(self) -> None:
        """Refuse evidence from the wrong chain or from an address with no
        contract. Either would read 0 for every wallet, and a zero balance is
        exactly what releases a fence."""
        if self._verified:
            return
        chain = _quantity(await self._call("eth_chainId", []), "eth_chainId")
        if chain != self.chain_id:
            raise CtfError(f"RPC serves chain {chain}, expected {self.chain_id}")
        code = await self._call("eth_getCode", [self.contract, "latest"])
        if not isinstance(code, str) or not code.removeprefix("0x"):
            raise CtfError("no contract code at the ConditionalTokens address")
        self._verified = True

    async def head(self) -> ChainHead:
        """The newest block this node has, which every balance is read at."""
        await self._verify()
        block = await self._call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(block, dict):
            raise CtfError("eth_getBlockByNumber: malformed block")
        return ChainHead(number=_quantity(block.get("number"), "block number"),
                         timestamp=_quantity(block.get("timestamp"), "block timestamp"))

    async def balance(self, owner: str, token_id: str, head: ChainHead) -> ChainBalance:
        """Outcome shares `owner` holds of `token_id` as of block `head`."""
        await self._verify()
        try:
            token = int(str(token_id))
        except ValueError:
            raise CtfError("token id is not a uint256") from None
        if not 0 <= token < 2 ** 256:
            raise CtfError("token id is not a uint256")
        data = ("0x" + BALANCE_OF_SELECTOR
                + _address(owner)[2:].lower().rjust(64, "0")
                + format(token, "064x"))
        raw = await self._call(
            "eth_call", [{"to": self.contract, "data": data}, hex(head.number)])
        if not isinstance(raw, str) or len(raw.removeprefix("0x")) != 64:
            raise CtfError("eth_call: balanceOf returned malformed data")
        return ChainBalance(shares=int(raw, 16) / SHARE_UNITS, head=head)


def _address(value) -> str:
    body = str(value or "").removeprefix("0x")
    if len(body) != 40 or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise CtfError("not a 20-byte address")
    return "0x" + body


def _quantity(value, what: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise CtfError(f"{what}: malformed quantity")
    try:
        return int(value, 16)
    except ValueError:
        raise CtfError(f"{what}: malformed quantity") from None
