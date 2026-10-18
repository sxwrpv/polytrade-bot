"""wallet.ensure_allowances against the real SDK environment object.

On polymarket-client 0.6.0, ``AsyncSecureClient.environment`` is the bare
``Environment`` (a ``name`` and a private ``_config``), so reading
``environment.collateral_token`` raised AttributeError every time. Wallet
creation logged and swallowed it, and new wallets got no exchange approvals.
The fake client below carries the real ``PRODUCTION`` object for that reason.

It accepts either way of submitting approvals: one ``approve_*`` call per
approval, or one ``execute_transaction`` batch. Each call is decoded from its
calldata as the token contract would read it. No network, no funds.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from eth_abi import decode
from eth_utils import keccak, to_checksum_address
from polymarket.errors import RequestRejectedError, TransactionFailedError
from polymarket.environments import PRODUCTION

from backend.core import wallet

MAX_UINT256 = (1 << 256) - 1
APPROVE = keccak(text="approve(address,uint256)")[:4]
SET_APPROVAL_FOR_ALL = keccak(text="setApprovalForAll(address,bool)")[:4]

# Polygon mainnet contracts, written out so that a changed address in an SDK
# upgrade fails here and gets reviewed rather than approved silently.
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
OPERATORS = (
    "0xE111180000d2663C0091e4f400237545B87B996B",  # standard exchange
    "0xe2222d279d744050d28e00520010520000310F59",  # neg-risk exchange
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # neg-risk adapter
)
EXPECTED = (
    [("approve", to_checksum_address(PUSD), to_checksum_address(op), MAX_UINT256)
     for op in OPERATORS]
    + [("setApprovalForAll", to_checksum_address(CTF), to_checksum_address(op), True)
       for op in OPERATORS]
)


def decoded(to: str, data: str) -> tuple:
    raw = bytes.fromhex(data.removeprefix("0x"))
    selector, args = raw[:4], raw[4:]
    if selector == APPROVE:
        spender, amount = decode(["address", "uint256"], args)
        return ("approve", to_checksum_address(to), to_checksum_address(spender), amount)
    if selector == SET_APPROVAL_FOR_ALL:
        operator, approved = decode(["address", "bool"], args)
        return ("setApprovalForAll", to_checksum_address(to),
                to_checksum_address(operator), approved)
    raise AssertionError(f"unexpected call selector 0x{selector.hex()}")


class FakeClient:
    """The slice of AsyncSecureClient that ensure_allowances touches."""

    def __init__(self, *, reject=None, fail=None):
        self.environment = PRODUCTION
        self.actions: list[list[tuple]] = []   # one entry per relayer action
        self.waited: list[int] = []
        self.reject, self.fail = reject, fail

    def _submit(self, approvals: list[tuple]):
        if self.reject is not None:
            raise self.reject
        self.actions.append(approvals)
        index, client = len(self.actions) - 1, self

        class Handle:
            async def wait(self):
                client.waited.append(index)
                if client.fail is not None:
                    raise client.fail
                return SimpleNamespace(transaction_hash="0x" + "f" * 64,
                                       transaction_id=str(index))
        return Handle()

    async def execute_transaction(self, *, calls, metadata=None):
        return self._submit([decoded(str(c.to), str(c.data)) for c in calls])

    async def approve_erc20(self, *, token_address, spender_address, amount, metadata=None):
        return self._submit([("approve", to_checksum_address(token_address),
                              to_checksum_address(spender_address),
                              MAX_UINT256 if amount == "max" else amount)])

    async def approve_erc1155_for_all(self, *, token_address, operator_address,
                                      approved=True, metadata=None):
        return self._submit([("setApprovalForAll", to_checksum_address(token_address),
                              to_checksum_address(operator_address), approved)])


class EnsureAllowancesTests(unittest.IsolatedAsyncioTestCase):
    async def test_approves_the_three_operators_on_the_real_sdk_environment(self):
        client = FakeClient()
        await wallet.ensure_allowances(client)
        submitted = [approval for action in client.actions for approval in action]
        # Exactly the six: pUSD approve and CTF setApprovalForAll for each
        # operator, nothing else (no auto-redeem operator, no duplicates).
        self.assertCountEqual(EXPECTED, submitted)
        self.assertEqual(list(range(len(client.actions))), sorted(client.waited))

    async def test_all_six_approvals_go_in_one_relayer_action(self):
        # The relayer runs one action per wallet at a time ("wallet has
        # in-flight action"). Six separate submissions would queue behind each
        # other in the SDK's submit retry and could stop part-way.
        client = FakeClient()
        await wallet.ensure_allowances(client)
        self.assertEqual(1, len(client.actions))
        self.assertCountEqual(EXPECTED, client.actions[0])
        self.assertEqual([0], client.waited)

    async def test_a_rejected_or_failed_batch_raises(self):
        for name, client, error in (
            ("rejected at submit", FakeClient(reject=RequestRejectedError(
                "operator is not in the allowed list", status=400)), RequestRejectedError),
            ("failed on chain", FakeClient(fail=TransactionFailedError("reverted")),
             TransactionFailedError),
        ):
            with self.subTest(name):
                with self.assertRaises(error):
                    await wallet.ensure_allowances(client)

    def test_environment_config_reads_addresses_from_the_sdk(self):
        cfg = wallet.environment_config(PRODUCTION)
        self.assertEqual(137, cfg.chain_id)
        self.assertEqual(to_checksum_address(PUSD), to_checksum_address(cfg.collateral_token))
        self.assertEqual(to_checksum_address(CTF), to_checksum_address(cfg.conditional_tokens))


if __name__ == "__main__":
    unittest.main()
