"""Regressions for the 2026-11-10 review: input hardening in the withdraw,
user, telemetry and client-identity code.

Each defect here was reproduced against main@e51225b before it was fixed. No
network and no funds: the Polygon RPC is an httpx.MockTransport that answers
in the JSON-RPC shapes, and the bridge and the relayer are fakes.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from eth_utils import to_checksum_address
from fastapi import HTTPException, Response
from pydantic import ValidationError

from backend.api import routes_public_screener, routes_telemetry, routes_user
from backend.core import withdraw as w
from backend.core.client_identity import client_identity, is_trusted_proxy
from backend.core.telemetry import enforce_product_event_limits
from backend.db.database import Database

WALLET = to_checksum_address("0x" + "1a" * 20)
SIGNER = to_checksum_address("0x" + "2b" * 20)
DEST = to_checksum_address("0x" + "3c" * 20)
BRIDGE = to_checksum_address("0x" + "4d" * 20)
SOLVER = to_checksum_address("0x" + "5e" * 20)
OTHER_WALLET = to_checksum_address("0x" + "6f" * 20)
OTHER_SIGNER = to_checksum_address("0x" + "7a" * 20)
TG = 4242
SEND_TX = "0x" + "f7" * 32
DELIVERY_TX = "0x" + "e8" * 32
OFF_POLYGON_TX = "0x" + "ab" * 32
NOW = 1_800_000_000.0
HEAD = 1000
RPC_URL = "https://rpc.example/secret-key-123"
LIMITS = w.Limits(floor_units=2_000_000, max_units=500_000_000, daily_units=1_000_000_000,
                  daily_count=3, bridge_timeout=1800.0)
BAD_AMOUNT = "Enter an amount in dollars, for example 25.50."
# What eth_blockNumber and eth_getLogs must never be read from.
UNUSABLE_HEADS = (None, "", "0x", "0xzz", "latest", "1000", 1000, 0, True, [], {"number": "0x3e8"})
UNUSABLE_LOGS = (None, {}, "0x", 0)


def transfer_log(token: str, sender: str, recipient: str, amount: int, tx: str = SEND_TX,
                 block: int = HEAD - 5) -> dict:
    return {"address": token.lower(), "transactionHash": tx, "blockNumber": hex(block),
            "topics": [w.TRANSFER_TOPIC, w._topic(sender), w._topic(recipient)],
            "data": "0x" + format(amount, "064x")}


def receipt(*logs) -> dict:
    return {"status": "0x1", "logs": list(logs)}


class Rpc:
    """A Polygon node behind httpx.MockTransport. `head` and `logs` go back as
    the result of eth_blockNumber and eth_getLogs exactly as set, so a test
    can make either answer unusable; `logs_null_from` answers null for every
    log query reaching that block."""

    def __init__(self) -> None:
        self.head = hex(HEAD)
        self.logs = []
        self.logs_null_from: int | None = None
        self.receipts: dict[str, dict] = {}
        self.balance = 100_000_000
        self.log_ranges: list[tuple[int, int]] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._answer))

    def _answer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_blockNumber":
            result = self.head
        elif method == "eth_call":
            result = "0x" + format(self.balance, "064x")
        elif method == "eth_getTransactionReceipt":
            result = self.receipts.get(params[0])
        else:
            assert method == "eth_getLogs", method
            result = self._logs(params[0])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def _logs(self, query: dict):
        first, last = int(query["fromBlock"], 16), int(query["toBlock"], 16)
        self.log_ranges.append((first, last))
        if self.logs_null_from is not None and last >= self.logs_null_from:
            return None
        if not isinstance(self.logs, list):
            return self.logs
        return [entry for entry in self.logs
                if entry["address"] == query["address"].lower()
                and first <= int(entry["blockNumber"], 16) <= last]

    def reader(self) -> w.PolygonReader:
        return w.PolygonReader(RPC_URL, client=self.http)


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class Handle:
    """A relayer handle: `wait` answers with the transfer's hash, or raises."""
    transaction_id = "relayer-1"
    transaction_hash = None

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def wait(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(transaction_hash=SEND_TX, transaction_id=self.transaction_id)


class Client:
    """The slice of AsyncSecureClient a withdrawal touches."""
    wallet_type = "DEPOSIT_WALLET"
    wallet = WALLET

    def __init__(self, handle: Handle | None = None) -> None:
        self.handle = handle or Handle()
        self.sent = 0

    async def get_balance_allowance(self, asset_type):
        return SimpleNamespace(balance=100_000_000, allowances={})

    async def execute_transaction(self, *, calls, metadata=None):
        self.sent += 1
        return self.handle


class Bridge:
    def __init__(self) -> None:
        self.history: list[dict] = []
        self.created = 0

    async def minimum_units(self):
        return 2_000_000

    async def quote(self, units, recipient):
        return w.Quote("quote-1", units, units * 9998 // 10000, 27)

    async def create_address(self, *, wallet, recipient):
        self.created += 1
        return BRIDGE

    async def transactions(self, address):
        return list(self.history)


class QuietChain:
    """A chain with nothing on it, for the tests that stay clear of the RPC."""

    def __init__(self, balance_error: Exception | None = None) -> None:
        self.balance_error = balance_error

    async def erc20_balance(self, token, owner):
        if self.balance_error is not None:
            raise self.balance_error
        return 100_000_000

    async def block_number(self):
        return HEAD

    async def receipt(self, tx_hash):
        return None

    async def transfer_logs(self, token, sender, recipient, from_block, to_block):
        return []


class NoDatabase:
    """Stands in for the database where a request must be refused before it."""

    def __getattr__(self, name):
        raise AssertionError(f"the database was asked for {name}")


class WithdrawCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        for wallet, signer, telegram in ((WALLET, SIGNER, TG), (OTHER_WALLET, OTHER_SIGNER, TG + 1)):
            await self.db.execute(
                "INSERT INTO users(id, signer_address, telegram_user_id, private_key_enc, "
                "created_at) VALUES(?,?,?,?,?)",
                (wallet, signer, telegram, "encrypted", w._iso(NOW - 86400 * 30)))
        self.user = await self.db.fetchone("SELECT * FROM users WHERE id=?", (WALLET,))
        self.rpc = Rpc()
        self.service = None
        routes_user._withdraw_hits.clear()
        routes_user._preview_hits.clear()

    async def asyncTearDown(self):
        if self.service is not None:
            await self.service.aclose()
        await self.rpc.http.aclose()
        await self.db.close()
        os.unlink(self.path)
        routes_user._withdraw_hits.clear()
        routes_user._preview_hits.clear()

    def make(self, *, chain=None, client: Client | None = None) -> w.Withdrawer:
        """A Withdrawer over the mock RPC's real PolygonReader unless `chain`
        is given."""
        self.clock = Clock()
        self.bridge = Bridge()
        self.client = client or Client()
        self.alerts: list[dict] = []

        async def notify(event):
            self.alerts.append(event)

        async def client_for(user):
            return self.client

        self.service = w.Withdrawer(
            self.db, client_for=client_for, chain=chain or self.rpc.reader(), bridge=self.bridge,
            notify=notify, limits=LIMITS, clock=self.clock, sleep=self.clock.sleep)
        return self.service

    async def request(self, amount: str = "25.00"):
        return await self.service.request(
            self.user, amount=amount, destination=DEST, confirm_last4=DEST[-4:],
            idempotency_key="key-00000001")

    async def insert(self, status: str, units: int, *, key: str, age: float = 3600.0,
                     user_id: str = WALLET, **fields) -> str:
        withdrawal_id = uuid.uuid4().hex
        stamp = w._iso(NOW - age)
        values = {"id": withdrawal_id, "user_id": user_id, "idempotency_key": key,
                  "amount_units": units, "destination": DEST, "to_chain_id": 137,
                  "to_token": w.USDC, "status": status, "requested_at": stamp,
                  "updated_at": stamp, **fields}
        cols = ",".join(values)
        marks = ",".join("?" for _ in values)
        await self.db.execute(f"INSERT INTO withdrawals({cols}) VALUES({marks})",
                              tuple(values.values()))
        return withdrawal_id

    async def row(self, withdrawal_id: str) -> dict:
        return await self.db.fetchone("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,))

    def http_request(self):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(withdrawer=self.service)))


# ---------------------------------------------------------------------------
# M11: an amount the decimal context cannot hold answered HTTP 500
# ---------------------------------------------------------------------------

class AmountParsingTests(unittest.TestCase):
    def test_an_amount_past_the_decimal_context_is_refused_as_a_bad_amount(self):
        for raw in ("1e30", "1e26", "1E+400", "1e999999999", "9" * 32, 1e30):
            with self.subTest(raw=raw):
                with self.assertRaises(w.WithdrawRefused) as caught:
                    w.parse_amount(raw)
                self.assertEqual((caught.exception.status, caught.exception.message),
                                 (400, BAD_AMOUNT))

    def test_a_non_finite_or_unparsable_amount_is_refused_as_a_bad_amount(self):
        for raw in ("NaN", "sNaN", "Infinity", "-Infinity", "1e99999999999999999999", "0x10",
                    "12,50", "", None, float("nan"), float("inf")):
            with self.subTest(raw=raw):
                with self.assertRaises(w.WithdrawRefused) as caught:
                    w.parse_amount(raw)
                self.assertEqual((caught.exception.status, caught.exception.message),
                                 (400, BAD_AMOUNT))

    def test_amounts_that_parsed_before_parse_the_same(self):
        for raw, units in (("25.50", 25_500_000), ("25.500", 25_500_000), (" 3 ", 3_000_000),
                           ("2.5e1", 25_000_000), ("1e3", 1_000_000_000),
                           ("1e25", 10 ** 31)):
            with self.subTest(raw=raw):
                self.assertEqual(w.parse_amount(raw), units)
        for raw in ("0.005", "1e-30", "2.001"):
            with self.subTest(raw=raw):
                with self.assertRaises(w.WithdrawRefused) as caught:
                    w.parse_amount(raw)
                self.assertEqual(caught.exception.message, "Amounts are in whole cents.")


class AmountRouteTests(WithdrawCase):
    async def test_preview_answers_400_for_an_amount_past_the_decimal_context(self):
        self.make()
        for amount in ("1e30", "1E+400", "9" * 32):
            with self.subTest(amount=amount):
                body = routes_user.WithdrawalPreviewBody(amount=amount, destination=DEST)
                with self.assertRaises(HTTPException) as caught:
                    await routes_user.preview_withdrawal(body, self.http_request(),
                                                         user=self.user)
                self.assertEqual((caught.exception.status_code, caught.exception.detail),
                                 (400, BAD_AMOUNT))

    async def test_create_answers_400_and_reserves_nothing(self):
        self.make()
        body = routes_user.WithdrawalBody(
            amount="1e30", destination=DEST, confirm_last4=DEST[-4:],
            idempotency_key="key-00000001", init_data="signed")
        with patch("backend.core.auth.validate_init_data", return_value={"id": TG}), \
                self.assertNoLogs("routes_user", level="ERROR"):
            with self.assertRaises(HTTPException) as caught:
                await routes_user.create_withdrawal(body, self.http_request(), user=self.user)
        self.assertEqual((caught.exception.status_code, caught.exception.detail),
                         (400, BAD_AMOUNT))
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM withdrawals"), 0)
        self.assertEqual((self.client.sent, self.bridge.created), (0, 0))


# ---------------------------------------------------------------------------
# M2: an unusable RPC answer was read as block 0 or as an empty log list
# ---------------------------------------------------------------------------

class PolygonReaderAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rpc = Rpc()
        self.reader = self.rpc.reader()

    async def asyncTearDown(self):
        await self.rpc.http.aclose()

    async def test_a_hex_head_block_is_read(self):
        for answer, block in (("0x3e8", 1000), ("0x0", 0), ("0x4C4B40", 5_000_000)):
            with self.subTest(answer=answer):
                self.rpc.head = answer
                self.assertEqual(await self.reader.block_number(), block)

    async def test_a_head_block_answer_that_is_no_hex_number_raises(self):
        for answer in UNUSABLE_HEADS:
            with self.subTest(answer=answer):
                self.rpc.head = answer
                with self.assertRaises(w.ChainReadError) as caught:
                    await self.reader.block_number()
                self.assertNotIn("secret-key-123", str(caught.exception))

    async def test_log_lists_are_joined_across_chunks(self):
        self.rpc.logs = [transfer_log(w.PUSD, WALLET, BRIDGE, 1, block=905),
                         transfer_log(w.PUSD, WALLET, BRIDGE, 2, block=1100)]
        logs = await self.reader.transfer_logs(w.PUSD, WALLET, BRIDGE, 900, 1150)
        self.assertEqual([int(entry["data"], 16) for entry in logs], [1, 2])
        self.assertEqual(self.rpc.log_ranges, [(900, 999), (1000, 1099), (1100, 1150)])
        self.rpc.logs = []
        self.assertEqual(await self.reader.transfer_logs(w.PUSD, WALLET, BRIDGE, 900, 1150), [])

    async def test_a_log_answer_that_is_no_list_raises(self):
        for answer in UNUSABLE_LOGS:
            with self.subTest(answer=answer):
                self.rpc.logs = answer
                with self.assertRaises(w.ChainReadError) as caught:
                    await self.reader.transfer_logs(w.PUSD, WALLET, BRIDGE, 900, 950)
                self.assertNotIn("secret-key-123", str(caught.exception))

    async def test_one_unusable_chunk_fails_the_whole_search(self):
        """The transfer may sit in the chunk that went unread."""
        self.rpc.logs = [transfer_log(w.PUSD, WALLET, BRIDGE, 1, block=905)]
        self.rpc.logs_null_from = 1000
        with self.assertRaises(w.ChainReadError):
            await self.reader.transfer_logs(w.PUSD, WALLET, BRIDGE, 900, 1150)
        self.assertEqual(self.rpc.log_ranges, [(900, 999), (1000, 1099)])


class UnusableChainAnswerTests(WithdrawCase):
    """Every caller of block_number and transfer_logs treats an unusable
    answer as it treats an RPC that is down: the withdrawal stays as it is
    and the next pass asks again."""

    def bridge_entry(self, amount: int) -> dict:
        return {"fromChainId": "137", "fromAmountBaseUnit": str(amount), "status": "COMPLETED",
                "txHash": OFF_POLYGON_TX}

    async def test_a_request_is_refused_before_anything_is_reserved(self):
        self.make()
        for answer in UNUSABLE_HEADS:
            with self.subTest(answer=answer):
                self.rpc.head = answer
                with self.assertLogs("withdraw", level="WARNING") as logs:
                    with self.assertRaises(w.WithdrawRefused) as caught:
                        await self.request()
                self.assertEqual(caught.exception.status, 503)
                self.assertIn("head block unavailable", "\n".join(logs.output))
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM withdrawals"), 0)
        self.assertEqual((self.client.sent, self.bridge.created), (0, 0))

    async def test_a_request_goes_through_once_the_head_block_is_readable(self):
        self.make(client=Client(handle=Handle(error=w.pm_errors.TransactionFailedError("x"))))
        out = await self.request()
        self.assertEqual(out["status"], "submitted")
        self.assertEqual((await self.row(out["id"]))["submit_block"], HEAD)
        with self.assertLogs("withdraw", level="WARNING"):
            await self.service._tasks[out["id"]]

    async def test_an_expired_send_stays_held_while_the_head_block_is_unusable(self):
        self.make()
        held = await self.insert("submitting", 25_000_000, key="held-0001",
                                 bridge_address=BRIDGE, submit_block=HEAD - 10,
                                 submitting_at=w._iso(NOW - 3600))
        self.rpc.head = None
        with self.assertLogs("withdraw", level="ERROR") as logs:
            await self.service.reconcile()
        text = "\n".join(logs.output)
        self.assertIn("reconcile step failed", text)
        self.assertIn("eth_blockNumber returned no block number", text)
        self.assertNotIn("secret-key-123", text)
        stored = await self.row(held)
        self.assertEqual((stored["status"], stored["error"], stored["failed_at"]),
                         ("submitting", None, None))
        self.assertEqual(await w.held_units(self.db, WALLET), 25_000_000)
        self.assertEqual(self.alerts, [])
        # The node answers again, and the transfer was on-chain all along.
        self.rpc.head = hex(HEAD)
        sent = transfer_log(w.PUSD, WALLET, BRIDGE, 25_000_000)
        self.rpc.logs = [sent]
        self.rpc.receipts[SEND_TX] = receipt(sent)
        await self.service.reconcile()
        stored = await self.row(held)
        self.assertEqual((stored["status"], stored["tx_hash"]), ("bridging", SEND_TX))

    async def test_an_expired_send_stays_held_while_the_log_answer_is_unusable(self):
        self.make()
        held = await self.insert("submitting", 25_000_000, key="held-0002",
                                 bridge_address=BRIDGE, submit_block=HEAD - 10,
                                 submitting_at=w._iso(NOW - 3600))
        for answer in UNUSABLE_LOGS:
            with self.subTest(answer=answer):
                self.rpc.logs = answer
                with self.assertLogs("withdraw", level="ERROR") as logs:
                    await self.service.reconcile()
                self.assertIn("eth_getLogs returned no list", "\n".join(logs.output))
                self.assertEqual((await self.row(held))["status"], "submitting")
        # A list with no transfer in it is the proof a failure needs.
        self.rpc.logs = []
        with self.assertLogs("withdraw", level="WARNING"):
            await self.service.reconcile()
        stored = await self.row(held)
        self.assertEqual(stored["status"], "failed")
        self.assertIn("never executed", stored["error"])

    async def test_a_delivered_transfer_stays_bridging_while_the_chain_is_unusable(self):
        self.make()
        bridging = await self.insert("bridging", 3_000_000, key="brdg-0001",
                                     bridge_address=BRIDGE, bridge_baseline=0,
                                     submit_block=HEAD - 10, bridging_at=w._iso(NOW - 3600),
                                     min_out_units=2_999_406)
        self.bridge.history = [self.bridge_entry(3_000_000)]
        payment = transfer_log(w.USDC, SOLVER, DEST, 3_000_000, DELIVERY_TX)
        self.rpc.receipts[DELIVERY_TX] = receipt(payment)
        for head, logs_answer in ((None, [payment]), (hex(HEAD), None)):
            with self.subTest(head=head, logs=logs_answer):
                self.rpc.head, self.rpc.logs = head, logs_answer
                with self.assertLogs("withdraw", level="ERROR"):
                    await self.service.reconcile()
                stored = await self.row(bridging)
                self.assertEqual((stored["status"], stored["attention_at"]), ("bridging", None))
        self.assertEqual(self.alerts, [])
        # The node answers again: the bridge's payment is found and confirmed.
        self.rpc.head, self.rpc.logs = hex(HEAD), [payment]
        await self.service.reconcile()
        stored = await self.row(bridging)
        self.assertEqual((stored["status"], stored["delivery_tx_hash"], stored["delivered_units"]),
                         ("confirmed", DELIVERY_TX, 3_000_000))

    async def test_the_pass_reaches_the_next_withdrawal_after_an_unreadable_one(self):
        self.make()
        unreadable = await self.insert("submitting", 25_000_000, key="held-0003",
                                       bridge_address=BRIDGE, submit_block=HEAD - 10,
                                       submitting_at=w._iso(NOW - 3600))
        stale = await self.insert("requested", 2_000_000, key="stale-001", age=600.0,
                                  user_id=OTHER_WALLET)
        self.rpc.head = None
        with self.assertLogs("withdraw", level="WARNING"):
            await self.service.reconcile()
        self.assertEqual((await self.row(unreadable))["status"], "submitting")
        self.assertEqual((await self.row(stale))["status"], "failed")

    async def test_a_follow_task_hands_an_unreadable_withdrawal_to_the_reconciler(self):
        self.make()
        out = await self.request()
        self.assertEqual(out["status"], "submitted")
        task = self.service._tasks[out["id"]]
        self.rpc.head = None
        with self.assertLogs("withdraw", level="ERROR") as logs:
            await task
        self.assertIn("the reconciler continues", "\n".join(logs.output))
        stored = await self.row(out["id"])
        self.assertEqual((stored["status"], stored["tx_hash"]), ("submitted", SEND_TX))
        self.assertNotIn(out["id"], self.service._busy)
        self.assertEqual(self.service._tasks, {})
        self.rpc.head = hex(HEAD)
        self.rpc.receipts[SEND_TX] = receipt(transfer_log(w.PUSD, WALLET, BRIDGE, 25_000_000))
        await self.service.reconcile()
        self.assertEqual((await self.row(out["id"]))["status"], "bridging")


# ---------------------------------------------------------------------------
# M9b: three log lines named the exception type and dropped its message
# ---------------------------------------------------------------------------

class ErrorCauseLoggingTests(WithdrawCase):
    async def test_a_failed_balance_read_logs_its_cause_with_addresses_shortened(self):
        self.make(chain=QuietChain(RuntimeError(f"upstream refused eth_call for {WALLET}")))
        with self.assertLogs("withdraw", level="WARNING") as logs:
            out = await self.service.summary(self.user)
        self.assertIsNone(out["balance"])
        self.assertEqual(out["balance_error"],
                         "Your balance could not be read right now. Try again shortly.")
        text = "\n".join(logs.output)
        self.assertIn(f"(RuntimeError: upstream refused eth_call for {w.short(WALLET)})", text)
        self.assertNotIn(WALLET[2:], text)

    async def test_a_failed_balance_read_still_refuses_with_503(self):
        self.make(chain=QuietChain(RuntimeError("x" * 500)))
        with self.assertLogs("withdraw", level="WARNING") as logs:
            with self.assertRaises(w.WithdrawRefused) as caught:
                await self.service.preview(self.user, "25.00", DEST)
        self.assertEqual(caught.exception.status, 503)
        text = "\n".join(logs.output)
        self.assertIn("x" * 200, text)
        self.assertNotIn("x" * 201, text)

    async def test_a_relayer_wait_without_an_answer_logs_its_cause(self):
        lost = RuntimeError(f"socket closed while polling for {BRIDGE}")
        self.make(chain=QuietChain(), client=Client(handle=Handle(error=lost)))
        out = await self.request()
        with self.assertLogs("withdraw", level="WARNING") as logs:
            await self.service._tasks[out["id"]]
        text = "\n".join(logs.output)
        self.assertIn("relayer wait ended without an answer "
                      f"(RuntimeError: socket closed while polling for {w.short(BRIDGE)})", text)
        self.assertNotIn(BRIDGE[2:], text)
        # The follow-up went on as before: held to the batch expiry, then failed.
        stored = await self.row(out["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertGreaterEqual(self.clock.t - NOW, w.BATCH_EXPIRY_SECONDS)

    async def test_a_withdrawal_task_failure_logs_its_cause(self):
        async def fail(error):
            raise error

        async def finished(error) -> asyncio.Task:
            task = asyncio.ensure_future(fail(error))
            await asyncio.wait([task])
            return task

        task = await finished(RuntimeError(f"relayer lost {DEST} " + "y" * 500))
        with self.assertLogs("routes_user", level="ERROR") as logs:
            self.assertIsNone(routes_user._retrieve(task))
        text = "\n".join(logs.output)
        self.assertIn(f"(RuntimeError: relayer lost {w.short(DEST)} yyy", text)
        self.assertNotIn(DEST[2:], text)
        self.assertNotIn("y" * 200, text)
        # A refusal is the request's own answer and is logged nowhere.
        refused = await finished(w.WithdrawRefused(400, "Amounts are in whole cents."))
        with self.assertNoLogs("routes_user", level="ERROR"):
            self.assertIsNone(routes_user._retrieve(refused))


# ---------------------------------------------------------------------------
# A14: wallet creation stored a display name of any length
# ---------------------------------------------------------------------------

class DisplayNameCapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def stored_name(self, telegram_user: dict, **body):
        """Create a wallet for `telegram_user` and return the name stored."""
        client = AsyncMock()
        client.wallet = "0xfunder"
        request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"), headers={})
        values = {"init_data": "signed-init-data", "terms_accepted": True,
                  "terms_version": routes_user.CURRENT_TERMS_VERSION, **body}
        with patch("backend.api.routes_user.TELEGRAM_BOT_TOKEN", "bot-token"), \
                patch("backend.api.routes_user.auth.validate_init_data",
                      return_value={"id": 42, **telegram_user}), \
                patch("backend.api.routes_user.wallet.create_signer",
                      return_value={"address": "0xsigner", "private_key": "secret"}), \
                patch("backend.api.routes_user.wallet.make_clob_client", return_value=client), \
                patch("backend.api.routes_user.wallet.encrypt_private_key",
                      return_value="encrypted"), \
                patch("backend.api.routes_user._create_rate_limited", return_value=False):
            result = await routes_user.create_wallet(
                routes_user.CreateWallet(**values), request, Response(), db=self.db)
        self.assertTrue(result["created"])
        return await self.db.fetchval("SELECT display_name FROM users WHERE id=?", ("0xfunder",))

    async def test_a_long_telegram_first_name_is_stored_cut_to_the_limit(self):
        name = "".join(chr(0x430 + i % 32) for i in range(200))     # Cyrillic letters
        stored = await self.stored_name({"first_name": name})
        self.assertEqual(stored, name[:80])
        self.assertEqual(len(stored), routes_user.DISPLAY_NAME_MAX_LENGTH)

    async def test_a_long_telegram_username_is_stored_cut_to_the_limit(self):
        self.assertEqual(await self.stored_name({"username": "u" * 81, "first_name": "Ann"}),
                         "u" * 80)

    async def test_a_long_requested_name_is_stored_cut_to_the_limit(self):
        stored = await self.stored_name({"username": "alice"}, display_name="r" * 5000)
        self.assertEqual(stored, "r" * 80)

    async def test_a_name_within_the_limit_is_stored_whole(self):
        self.assertEqual(await self.stored_name({"username": "alice"}), "alice")

    async def test_a_name_of_exactly_the_limit_is_stored_whole(self):
        self.assertEqual(await self.stored_name({"first_name": "n" * 80}), "n" * 80)

    async def test_an_account_without_a_name_stores_none(self):
        self.assertIsNone(await self.stored_name({}))

    def test_settings_and_wallet_creation_share_one_limit(self):
        limit = routes_user.DISPLAY_NAME_MAX_LENGTH
        self.assertEqual(limit, 80)
        self.assertEqual(routes_user.SettingsBody(display_name="s" * limit).display_name,
                         "s" * limit)
        with self.assertRaises(ValidationError):
            routes_user.SettingsBody(display_name="s" * (limit + 1))


# ---------------------------------------------------------------------------
# A2: every telemetry POST reached the database before any limit applied
# ---------------------------------------------------------------------------

class TelemetryRateLimitTests(unittest.IsolatedAsyncioTestCase):
    ACCOUNT = {"id": "0x" + "f" * 40}
    OTHER_ACCOUNT = {"id": "0x" + "e" * 40}

    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        routes_telemetry.reset_rate_limits()

    async def asyncTearDown(self):
        routes_telemetry.reset_rate_limits()
        await self.db.close()
        os.unlink(self.path)

    def event(self) -> routes_telemetry.TelemetryEvent:
        """An event under a fresh session id, which the database's per-session
        limit counts as a new sender every time."""
        return routes_telemetry.TelemetryEvent(
            session_id=str(uuid.uuid4()), event_name="close_modal_opened",
            properties={"source": "positions"})

    async def send(self, account: dict, db=None) -> dict:
        return await routes_telemetry.record_event(self.event(), user=account, db=db or self.db)

    async def events_stored(self) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM product_events"))

    async def test_an_account_over_its_limit_is_refused_before_any_database_work(self):
        with patch.object(routes_telemetry, "EVENT_RATE_LIMIT", "3/60"):
            for _ in range(3):
                self.assertEqual(await self.send(self.ACCOUNT), {"accepted": True})
            for _ in range(5):
                with self.assertRaises(HTTPException) as caught:
                    await self.send(self.ACCOUNT, db=NoDatabase())
                self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(await self.events_stored(), 3)

    async def test_the_refusal_is_the_one_the_database_limit_gives(self):
        await self.send(self.ACCOUNT)
        session_id = await self.db.fetchval("SELECT session_id FROM product_events")
        with self.assertRaises(HTTPException) as from_database:
            await enforce_product_event_limits(self.db, session_id, session_limit=1)
        with patch.object(routes_telemetry, "EVENT_RATE_LIMIT", "1/60"):
            with self.assertRaises(HTTPException) as from_memory:
                await self.send(self.ACCOUNT, db=NoDatabase())
        for field in ("status_code", "detail", "headers"):
            self.assertEqual(getattr(from_memory.exception, field),
                             getattr(from_database.exception, field), field)
        self.assertEqual((from_memory.exception.status_code, from_memory.exception.detail),
                         (429, "telemetry rate limit exceeded"))

    async def test_another_account_keeps_its_own_budget(self):
        with patch.object(routes_telemetry, "EVENT_RATE_LIMIT", "2/60"):
            for _ in range(2):
                await self.send(self.ACCOUNT)
            with self.assertRaises(HTTPException):
                await self.send(self.ACCOUNT, db=NoDatabase())
            self.assertEqual(await self.send(self.OTHER_ACCOUNT), {"accepted": True})
        self.assertEqual(await self.events_stored(), 3)

    async def test_the_budget_returns_once_the_window_has_passed(self):
        with patch.object(routes_telemetry, "EVENT_RATE_LIMIT", "2/60"):
            for _ in range(2):
                await self.send(self.ACCOUNT)
            with self.assertRaises(HTTPException):
                await self.send(self.ACCOUNT, db=NoDatabase())
            key = self.ACCOUNT["id"]
            routes_telemetry._event_hits[key] = [
                t - 61 for t in routes_telemetry._event_hits[key]]
            self.assertEqual(await self.send(self.ACCOUNT), {"accepted": True})

    async def test_reset_rate_limits_drops_every_bucket(self):
        with patch.object(routes_telemetry, "EVENT_RATE_LIMIT", "1/60"):
            await self.send(self.ACCOUNT)
            await self.send(self.OTHER_ACCOUNT)
            self.assertEqual(sorted(routes_telemetry._event_hits),
                             sorted([self.ACCOUNT["id"], self.OTHER_ACCOUNT["id"]]))
            routes_telemetry.reset_rate_limits()
            self.assertEqual(routes_telemetry._event_hits, {})
            self.assertEqual(await self.send(self.ACCOUNT), {"accepted": True})

    async def test_the_account_key_stays_out_of_the_stored_events(self):
        await self.send(self.ACCOUNT)
        row = await self.db.fetchone("SELECT * FROM product_events")
        self.assertNotIn(self.ACCOUNT["id"], json.dumps(row))

    def test_the_limit_sits_far_above_what_the_mini_app_sends(self):
        limit_text, _, window_text = routes_telemetry.EVENT_RATE_LIMIT.partition("/")
        limit, window = int(limit_text), float(window_text)
        defaults = inspect.signature(enforce_product_event_limits).parameters
        self.assertEqual(window, 60.0)
        # One tab session is refused by the database's own limit first, so a
        # request the database accepts today is still accepted.
        self.assertGreaterEqual(limit, 2 * defaults["session_limit"].default)
        # One account alone stays under what every caller shares.
        self.assertLess(limit, defaults["global_limit"].default)

    def test_the_limit_is_counted_by_the_helper_the_withdrawal_routes_use(self):
        self.assertIs(routes_telemetry._rate_limited, routes_user._rate_limited)


# ---------------------------------------------------------------------------
# A4: one IPv6 /64 customer line owned 2^64 rate-limit identities
# ---------------------------------------------------------------------------

def proxied(forwarded: str | None, peer: str = "172.20.0.3"):
    headers = {} if forwarded is None else {"x-forwarded-for": forwarded}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


class Ipv6IdentityTests(unittest.TestCase):
    def test_addresses_on_one_64_share_an_identity(self):
        for address in ("2001:db8:1:2::1", "2001:db8:1:2:ffff:ffff:ffff:ffff",
                        "2001:DB8:1:2:0:0:0:99", "[2001:db8:1:2::7]"):
            with self.subTest(address=address):
                self.assertEqual(client_identity(proxied(address)), "2001:db8:1:2::/64")

    def test_addresses_on_different_64s_stay_apart(self):
        self.assertEqual(client_identity(proxied("2001:db8:1:3::1")), "2001:db8:1:3::/64")
        self.assertNotEqual(client_identity(proxied("2001:db8:1:2::1")),
                            client_identity(proxied("2001:db8:1:3::1")))

    def test_a_direct_ipv6_caller_is_keyed_by_its_64_and_its_header_ignored(self):
        self.assertEqual(
            client_identity(proxied("203.0.113.9", peer="2001:db8:aa:bb:1:2:3:4")),
            "2001:db8:aa:bb::/64")

    def test_ipv4_identities_are_unchanged(self):
        self.assertEqual(client_identity(proxied("203.0.113.9, 172.20.0.3")), "203.0.113.9")
        self.assertEqual(client_identity(proxied(None)), "172.20.0.3")
        self.assertEqual(client_identity(proxied("10.0.0.1", peer="203.0.113.9")), "203.0.113.9")

    def test_ipv4_mapped_ipv6_is_treated_as_ipv4(self):
        for address in ("::ffff:203.0.113.9", "[::ffff:203.0.113.9]", "::FFFF:203.0.113.9"):
            with self.subTest(address=address):
                self.assertEqual(client_identity(proxied(address)), "203.0.113.9")
        self.assertEqual(client_identity(proxied(None, peer="::ffff:172.20.0.3")), "172.20.0.3")

    def test_malformed_input_falls_back_as_before(self):
        self.assertEqual(client_identity(proxied("not-an-ip")), "172.20.0.3")
        self.assertEqual(client_identity(proxied("2001:db8::zz, 203.0.113.9")), "203.0.113.9")
        self.assertEqual(client_identity(proxied("2001:db8::1/64")), "172.20.0.3")
        self.assertEqual(client_identity(proxied(None, peer="testclient")), "unknown")
        self.assertEqual(client_identity(SimpleNamespace(client=None, headers={})), "unknown")

    def test_the_proxy_check_reads_the_whole_address(self):
        self.assertTrue(is_trusted_proxy("::1"))
        self.assertFalse(is_trusted_proxy("2001:db8::1"))
        self.assertFalse(is_trusted_proxy("2001:db8::/64"))


class Ipv6LimitWiringTests(unittest.TestCase):
    def setUp(self):
        routes_public_screener.reset_rate_limits()
        routes_user._create_hits.clear()

    def tearDown(self):
        routes_public_screener.reset_rate_limits()
        routes_user._create_hits.clear()

    def test_one_ipv6_line_gets_one_wallet_creation_budget(self):
        limit = int(routes_user.CREATE_WALLET_RATE_LIMIT.split("/")[0])
        for host in range(limit):
            caller = proxied(f"2001:db8:1:2::{host + 1:x}")
            self.assertFalse(routes_user._create_rate_limited(routes_user._client_ip(caller)))
        fresh_address = proxied("2001:db8:1:2:dead:beef:0:1")
        self.assertTrue(routes_user._create_rate_limited(routes_user._client_ip(fresh_address)))
        neighbour = proxied("2001:db8:1:3::1")
        self.assertFalse(routes_user._create_rate_limited(routes_user._client_ip(neighbour)))

    def test_one_ipv6_line_gets_one_public_screener_bucket(self):
        for host in range(1, 9):
            routes_public_screener._enforce_rate_limit(proxied(f"2001:db8:1:2::{host:x}"))
        routes_public_screener._enforce_rate_limit(proxied("203.0.113.9"))
        self.assertEqual(sorted(routes_public_screener.rate_limit_keys()),
                         ["2001:db8:1:2::/64", "203.0.113.9"])


if __name__ == "__main__":
    unittest.main()
