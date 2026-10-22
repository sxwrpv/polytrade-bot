"""Withdrawals (backend/core/withdraw.py): pUSD out of a deposit wallet as
native USDC on Polygon through Polymarket's bridge.

No network and no funds. The bridge, the Polygon RPC and the relayer are
fakes: the relayer decodes each call as pUSD would read it, and the chain
answers receipts and Transfer logs in the JSON-RPC shapes, so a withdrawal is
followed from the request to a verified delivery, and every failure row of the
design is driven through the same code.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from eth_abi import decode
from eth_utils import keccak, to_checksum_address
from fastapi import HTTPException
from polymarket import errors as pm_errors
from polymarket.environments import PRODUCTION

from backend.api import routes_user
from backend.core import withdraw as w
from backend.db.database import Database
from backend.db.models import PG_SCHEMA_SQL, SCHEMA_SQL, TABLES

ROOT = Path(__file__).resolve().parents[1]
WALLET = to_checksum_address("0x" + "1a" * 20)
SIGNER = to_checksum_address("0x" + "2b" * 20)
DEST = to_checksum_address("0x" + "3c" * 20)
BRIDGE = to_checksum_address("0x" + "4d" * 20)
SOLVER = to_checksum_address("0x" + "5e" * 20)
TG = 4242
SEND_TX = "0x" + "f7" * 32
DELIVERY_TX = "0x" + "e8" * 32
TRANSFER = keccak(text="transfer(address,uint256)")[:4]
NOW = 1_800_000_000.0
LIMITS = w.Limits(floor_units=2_000_000, max_units=500_000_000, daily_units=1_000_000_000,
                  daily_count=3, bridge_timeout=1800.0)


def transfer_log(token: str, sender: str, recipient: str, amount: int, tx: str = SEND_TX) -> dict:
    return {"address": token.lower(), "transactionHash": tx,
            "topics": [w.TRANSFER_TOPIC, w._topic(sender), w._topic(recipient)],
            "data": "0x" + format(amount, "064x")}


def receipt(*logs, ok: bool = True) -> dict:
    return {"status": "0x1" if ok else "0x0", "logs": list(logs)}


def bridge_entry(amount: int, status: str = "COMPLETED", tx: str | None = DELIVERY_TX) -> dict:
    entry = {"fromChainId": "137", "fromTokenAddress": w.PUSD, "fromAmountBaseUnit": str(amount),
             "toChainId": "137", "toTokenAddress": w.USDC, "status": status}
    if tx and status == "COMPLETED":
        entry["txHash"] = tx
    return entry


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class Handle:
    def __init__(self, *, error: Exception | None = None, tx: str = SEND_TX) -> None:
        self.transaction_id = "relayer-1"
        self.transaction_hash = None
        self.error, self.tx = error, tx

    async def wait(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(transaction_hash=self.tx, transaction_id=self.transaction_id)


def decode_call(call) -> tuple:
    raw = bytes.fromhex(str(call.data).removeprefix("0x"))
    assert raw[:4] == TRANSFER, f"unexpected selector 0x{raw[:4].hex()}"
    recipient, amount = decode(["address", "uint256"], raw[4:])
    return to_checksum_address(call.to), to_checksum_address(recipient), amount


class Client:
    """The slice of AsyncSecureClient a withdrawal touches."""
    wallet_type = "DEPOSIT_WALLET"

    def __init__(self, *, balance: int = 100_000_000, failures=(), handle: Handle | None = None):
        self.wallet = WALLET
        self.environment = PRODUCTION
        self.balance = balance
        self.failures = list(failures)
        self.handle = handle or Handle()
        self.sent: list[list[tuple]] = []

    async def get_balance_allowance(self, asset_type):
        assert asset_type == "COLLATERAL"
        return SimpleNamespace(balance=self.balance, allowances={})

    async def execute_transaction(self, *, calls, metadata=None):
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        self.sent.append([decode_call(c) for c in calls])
        return self.handle


class Chain:
    def __init__(self, *, balance: int = 100_000_000, head: int = 1000) -> None:
        self.balance = balance
        self.head = head
        self.receipts: dict[str, dict] = {}
        self.logs: list[dict] = []
        self.log_queries: list[tuple] = []

    async def erc20_balance(self, token, owner):
        assert token == w.PUSD and owner == WALLET
        return self.balance

    async def block_number(self):
        return self.head

    async def receipt(self, tx_hash):
        return self.receipts.get(tx_hash)

    async def transfer_logs(self, token, sender, recipient, from_block, to_block):
        self.log_queries.append((token, sender, recipient, from_block, to_block))
        return [entry for entry in self.logs
                if entry["address"] == token.lower()
                and (sender is None or entry["topics"][1] == w._topic(sender))
                and entry["topics"][2] == w._topic(recipient)]


class Bridge:
    def __init__(self, *, minimum: int | None = 2_000_000, quote: w.Quote | None = None,
                 address: str = BRIDGE, history=(), fail: str | None = None) -> None:
        self.minimum = minimum
        self.quote_value = quote
        self.address = address
        self.history = list(history)
        self.fail = fail
        self.created = 0

    async def minimum_units(self):
        if self.fail == "minimum":
            raise w.BridgeError("bridge unreachable (ConnectError)")
        return self.minimum

    async def quote(self, units, recipient):
        if self.fail == "quote":
            raise w.BridgeError("bridge answered HTTP 500")
        return self.quote_value or w.Quote("quote-1", units, units * 9998 // 10000, 27)

    async def create_address(self, *, wallet, recipient):
        assert wallet == WALLET and recipient == DEST
        self.created += 1
        if self.fail == "create":
            raise w.BridgeError("bridge answered HTTP 500")
        return self.address

    async def transactions(self, address):
        assert address == self.address
        if self.fail == "status":
            raise w.BridgeError("bridge answered HTTP 500: cannot get transaction status")
        return list(self.history)


class WithdrawTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.db.execute(
            "INSERT INTO users(id, signer_address, telegram_user_id, private_key_enc, created_at) "
            "VALUES(?,?,?,?,?)", (WALLET, SIGNER, TG, "encrypted", w._iso(NOW - 86400 * 30)))
        self.user = await self.db.fetchone("SELECT * FROM users WHERE id=?", (WALLET,))
        self.service = None

    async def asyncTearDown(self):
        if self.service is not None:
            await self.service.aclose()
        await self.db.close()
        os.unlink(self.path)

    def make(self, *, chain: Chain | None = None, bridge: Bridge | None = None,
             client: Client | None = None, **kwargs) -> w.Withdrawer:
        self.clock = Clock()
        self.chain = chain or Chain()
        self.bridge = bridge or Bridge()
        self.client = client or Client()
        self.alerts: list[dict] = []
        self.moved: list[str] = []

        async def notify(event):
            self.alerts.append(event)

        async def client_for(user):
            return self.client

        self.service = w.Withdrawer(
            self.db, client_for=client_for, chain=self.chain, bridge=self.bridge,
            notify=notify, on_funds_moved=self.moved.append, limits=LIMITS,
            clock=self.clock, sleep=self.clock.sleep, **kwargs)
        return self.service

    async def request(self, amount: str = "25.00", *, key: str = "key-00000001",
                      destination: str = DEST, last4: str | None = None):
        return await self.service.request(
            self.user, amount=amount, destination=destination,
            confirm_last4=DEST[-4:] if last4 is None else last4, idempotency_key=key)

    async def row(self, withdrawal_id: str) -> dict:
        return await self.db.fetchone("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,))

    async def insert(self, status: str, units: int, *, key: str, hours_ago: float = 1.0,
                     user_id: str = WALLET, **fields) -> str:
        withdrawal_id = key.replace("-", "")[:32].ljust(32, "0")
        stamp = w._iso(NOW - hours_ago * 3600)
        values = {"id": withdrawal_id, "user_id": user_id, "idempotency_key": key,
                  "amount_units": units, "destination": DEST, "to_chain_id": 137,
                  "to_token": w.USDC, "status": status, "requested_at": stamp,
                  "updated_at": stamp, **fields}
        cols = ",".join(values)
        marks = ",".join("?" for _ in values)
        await self.db.execute(f"INSERT INTO withdrawals({cols}) VALUES({marks})",
                              tuple(values.values()))
        return withdrawal_id

    def kinds(self) -> list[str]:
        return [a["event"].removeprefix("withdrawal_") for a in self.alerts]


class PureRuleTests(unittest.TestCase):
    def test_amounts_are_whole_cents(self):
        self.assertEqual(w.parse_amount("25.5"), 25_500_000)
        self.assertEqual(w.parse_amount("2"), 2_000_000)
        for bad in ("2.001", "0", "-1", "abc", "", "NaN", "Infinity"):
            with self.assertRaises(w.WithdrawRefused, msg=bad):
                w.parse_amount(bad)

    def test_addresses_take_the_checksummed_form(self):
        self.assertEqual(w.normalize_address(DEST.lower()), DEST)
        self.assertEqual(w.normalize_address(DEST), DEST)
        self.assertEqual(w.normalize_address("0x" + DEST[2:].upper()), DEST)

    def test_a_mixed_case_address_must_pass_its_checksum(self):
        # The EIP-55 test vector with one letter's case flipped.
        with self.assertRaises(w.WithdrawRefused) as caught:
            w.normalize_address("0x5aAeb6053f3e94c9b9a09f33669435e7ef1beaed")
        self.assertIn("checksum", caught.exception.message)
        for bad in ("0x123", DEST + "00", "1a" * 20, "0x" + "zz" * 20):
            with self.assertRaises(w.WithdrawRefused, msg=bad):
                w.normalize_address(bad)

    def test_destinations_that_lose_funds_are_refused(self):
        protocol = w.protocol_addresses(PRODUCTION)
        user = {"id": WALLET, "signer_address": SIGNER}
        refused = ["0x" + "0" * 40, WALLET, SIGNER, w.PUSD, w.USDC, w.USDC_E,
                   "0x2957922Eb93258b93368531d39fAcCA3B4dC5854",    # offramp
                   "0xE111180000d2663C0091e4f400237545B87B996B",    # CTF exchange (SDK config)
                   "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"]    # deposit wallet factory
        for address in refused:
            self.assertIsNotNone(w.destination_refusal(to_checksum_address(address), user, protocol),
                                 address)
        self.assertIsNone(w.destination_refusal(DEST, user, protocol))

    def test_quote_guard_holds_to_ten_and_fifty_basis_points(self):
        amount = 100_000_000
        self.assertIsNone(w.quote_refusal(amount, w.Quote("q", 99_900_000, 99_500_000, 27)))
        self.assertIsNotNone(w.quote_refusal(amount, w.Quote("q", 99_899_999, 99_500_000, 27)))
        self.assertIsNotNone(w.quote_refusal(amount, w.Quote("q", 100_000_000, 99_499_999, 27)))

    def test_receipt_checks_need_success_and_the_exact_transfer(self):
        sent = receipt(transfer_log(w.PUSD, WALLET, BRIDGE, 25_000_000))
        self.assertTrue(w.sent_to_bridge(sent, WALLET, BRIDGE, 25_000_000))
        self.assertFalse(w.sent_to_bridge(sent, WALLET, BRIDGE, 25_000_001))
        self.assertFalse(w.sent_to_bridge(sent, WALLET, SOLVER, 25_000_000))
        self.assertFalse(w.sent_to_bridge(
            receipt(transfer_log(w.PUSD, WALLET, BRIDGE, 25_000_000), ok=False),
            WALLET, BRIDGE, 25_000_000))
        self.assertFalse(w.sent_to_bridge(
            receipt(transfer_log(w.USDC, WALLET, BRIDGE, 25_000_000)), WALLET, BRIDGE, 25_000_000))
        delivered = receipt(transfer_log(w.USDC, SOLVER, DEST, 24_990_000, DELIVERY_TX),
                            transfer_log(w.USDC, SOLVER, SIGNER, 1, DELIVERY_TX))
        self.assertEqual(w.delivered_to(delivered, DEST), 24_990_000)
        self.assertEqual(w.delivered_to(receipt(transfer_log(w.USDC_E, SOLVER, DEST, 5)), DEST), 0)

    def test_status_matching_skips_what_was_there_before(self):
        older = bridge_entry(25_000_000, tx="0x" + "96" * 32)
        newer = bridge_entry(25_000_000, status="PROCESSING")
        self.assertIsNone(w.match_transfer([older], 1, 25_000_000))
        self.assertEqual(w.match_transfer([newer, older], 1, 25_000_000), newer)
        self.assertIsNone(w.match_transfer([bridge_entry(24_000_000), older], 1, 25_000_000))

    def test_logs_and_alerts_use_short_addresses(self):
        self.assertEqual(w.short(DEST), f"{DEST[:6]}…{DEST[-4:]}")
        text = w.redact(f"wallet {WALLET} busy, last tx {SEND_TX}")
        self.assertNotIn(WALLET, text)
        self.assertIn(w.short(WALLET), text)
        self.assertIn(SEND_TX, text)


class BridgeClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, **kwargs) -> w.BridgeClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return w.BridgeClient("https://bridge.example", client=http, **kwargs)

    async def test_quote_is_read_in_base_units(self):
        seen = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={
                "estCheckoutTimeMs": 27000, "estToTokenBaseUnit": "499999989",
                "quoteId": "0xabc", "estFeeBreakdown": {"minReceived": 499.899489}})

        quote = await self.client(handler).quote(500_000_000, DEST)
        self.assertEqual(quote, w.Quote("0xabc", 499_999_989, 499_899_489, 27))
        self.assertEqual(seen, {"fromAmountBaseUnit": "500000000", "fromChainId": "137",
                                "fromTokenAddress": w.PUSD, "recipientAddress": DEST,
                                "toChainId": "137", "toTokenAddress": w.USDC})

    async def test_withdrawal_address_and_builder_code(self):
        headers = {}

        def handler(request):
            headers.update(request.headers)
            body = json.loads(request.content)
            self.assertEqual(body, {"address": WALLET, "toChainId": "137",
                                    "toTokenAddress": w.USDC, "recipientAddr": DEST})
            return httpx.Response(201, json={"address": {"evm": BRIDGE.lower(), "svm": "x"}})

        code = "0x" + "ab" * 32
        address = await self.client(handler, builder_code=code).create_address(
            wallet=WALLET, recipient=DEST)
        self.assertEqual(address, BRIDGE)
        self.assertEqual(headers.get("x-builder-code"), code)
        headers.clear()
        await self.client(handler, builder_code="0xshort").create_address(wallet=WALLET,
                                                                          recipient=DEST)
        self.assertNotIn("x-builder-code", headers)

    async def test_status_walks_every_page(self):
        def handler(request):
            if "cursor" not in request.url.params:
                return httpx.Response(200, json={"transactions": [{"a": 1}], "nextCursor": "c2"})
            return httpx.Response(200, json={"transactions": [{"a": 2}], "nextCursor": None})

        self.assertEqual(await self.client(handler).transactions(BRIDGE), [{"a": 1}, {"a": 2}])

    async def test_errors_carry_no_address_or_url(self):
        def handler(request):
            return httpx.Response(500, json={"error": "cannot get transaction status"})

        with self.assertRaises(w.BridgeError) as caught:
            await self.client(handler).transactions(BRIDGE)
        text = str(caught.exception)
        self.assertIn("500", text)
        self.assertNotIn(BRIDGE[2:].lower(), text.lower())
        self.assertNotIn("bridge.example", text)

    async def test_minimum_comes_from_supported_assets(self):
        def handler(request):
            return httpx.Response(200, json={"supportedAssets": [
                {"chainId": "137", "token": {"address": w.USDC}, "minCheckoutUsd": 2},
                {"chainId": "1", "token": {"address": w.USDC}, "minCheckoutUsd": 7}]})

        self.assertEqual(await self.client(handler).minimum_units(), 2_000_000)


class PolygonReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_call_and_errors_without_the_rpc_url(self):
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            if body["method"] == "eth_call":
                return httpx.Response(200, json={"result": "0x" + format(1234, "064x")})
            return httpx.Response(429)

        reader = w.PolygonReader("https://rpc.example/secret-key-123",
                                 client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        self.assertEqual(await reader.erc20_balance(w.PUSD, WALLET), 1234)
        self.assertEqual(calls[0]["params"][0]["data"],
                         "0x70a08231" + "0" * 24 + WALLET.lower()[2:])
        with self.assertRaises(w.ChainReadError) as caught:
            await reader.block_number()
        self.assertNotIn("secret-key-123", str(caught.exception))

    async def test_log_queries_stay_within_the_public_endpoints_range(self):
        asked = []

        def handler(request):
            query = json.loads(request.content)["params"][0]
            start, end = int(query["fromBlock"], 16), int(query["toBlock"], 16)
            if end - start >= w.LOG_CHUNK_BLOCKS:     # drpc's free plan refuses 500 blocks
                return httpx.Response(400, json={"error": {"message": "range too large"}})
            asked.append((start, end, query["topics"][1]))
            return httpx.Response(200, json={"result": [{"blockNumber": hex(start)}]})

        reader = w.PolygonReader("https://rpc.example",
                                 client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        logs = await reader.transfer_logs(w.USDC, None, DEST, 1000, 1250)
        self.assertEqual(asked, [(1000, 1099, None), (1100, 1199, None), (1200, 1250, None)])
        self.assertEqual(len(logs), 3)


class WithdrawFlowTests(WithdrawTestCase):
    async def test_a_withdrawal_runs_from_request_to_verified_delivery(self):
        self.make()
        self.chain.receipts[SEND_TX] = receipt(transfer_log(w.PUSD, WALLET, BRIDGE, 25_000_000))
        with self.assertLogs("withdraw", level="INFO") as logs:
            out = await self.request(destination=DEST.lower())
            self.assertEqual(out["status"], "submitted")
            # One relayer transaction, one call: pUSD.transfer(bridge, exactly the amount).
            self.assertEqual(self.client.sent, [[(w.PUSD, BRIDGE, 25_000_000)]])
            self.bridge.history.insert(0, bridge_entry(25_000_000))
            self.chain.receipts[DELIVERY_TX] = receipt(
                transfer_log(w.USDC, SOLVER, DEST, 24_999_000, DELIVERY_TX))
            await self.service._tasks[out["id"]]
        final = await self.service.get(self.user, out["id"])
        self.assertEqual(final["status"], "confirmed")
        self.assertEqual(final["destination"], DEST)
        self.assertEqual(final["tx_hash"], SEND_TX)
        self.assertEqual(final["delivery_tx_hash"], DELIVERY_TX)
        self.assertEqual(final["explorer_url"], w.POLYGONSCAN_TX + DELIVERY_TX)
        stored = await self.row(out["id"])
        self.assertEqual(stored["delivered_units"], 24_999_000)
        for column in ("requested_at", "submitting_at", "submitted_at", "bridging_at",
                       "confirmed_at"):
            self.assertTrue(stored[column], column)
        self.assertEqual(self.kinds(), ["requested", "bridging", "confirmed"])
        self.assertEqual(self.moved, [WALLET, WALLET])
        text = "\n".join(logs.output).lower()
        for address in (DEST, BRIDGE, WALLET):
            self.assertNotIn(address[2:].lower(), text)
        self.assertIn(w.short(DEST).lower(), text)

    async def test_free_balance_subtracts_claims_settling_fills_and_open_withdrawals(self):
        self.make(chain=Chain(balance=120_000_000), client=Client(balance=110_000_000))
        now = w._iso(NOW)
        for token, state, reserved in (("t1", "uncertain", 43.26), ("t2", "submitting", 5.0),
                                       ("t3", "reserved", 1.5)):
            await self.db.execute(
                "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,"
                "state,reserved_usd,risk_revision,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (WALLET, token, "0xtrader", token, "open", state, reserved, 0, now, now))
        events = (("e1", "open", 4.0, None, 30), ("e2", "partial", 2.0, None, 60),
                  ("e3", "partial", None, 1.0, 30),    # a SELL: pnl, no amount
                  ("e4", "open", 9.0, None, 600))      # settled long ago
        for event_id, kind, amount, pnl, ago in events:
            await self.db.execute(
                "INSERT INTO trade_events(id,user_id,position_id,event_type,amount_usd,pnl,ts) "
                "VALUES(?,?,?,?,?,?,?)", (event_id, WALLET, None, kind, amount, pnl,
                                         w._iso(NOW - ago)))
        await self.insert("submitted", 10_000_000, key="held-0001")
        summary = await self.service.summary(self.user)
        self.assertEqual(summary["balance"], {
            "cash": "110.00", "reserved_for_orders": "49.76", "settling": "6.00",
            "in_progress": "10.00", "available": "44.24"})
        self.assertEqual(await w.held_units(self.db, WALLET), 10_000_000)

    async def test_more_than_the_free_balance_is_refused_before_anything_is_reserved(self):
        self.make(client=Client(balance=30_000_000))
        await self.db.execute(
            "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,state,"
            "reserved_usd,risk_revision,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (WALLET, "t1", "0xtrader", "c1", "open", "uncertain", 10.0, 0, w._iso(NOW),
             w._iso(NOW)))
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request("25.00")
        self.assertIn("$20.00 available", caught.exception.message)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM withdrawals"), 0)
        self.assertEqual(self.client.sent, [])

    async def test_one_open_withdrawal_per_user(self):
        self.make()
        await self.insert("bridging", 5_000_000, key="open-0001", bridge_address=BRIDGE)
        for attempt in (self.request(), self.service.preview(self.user, "5.00", DEST)):
            with self.assertRaises(w.WithdrawRefused) as caught:
                await attempt
            self.assertEqual(caught.exception.status, 409)

    async def test_attention_keeps_the_user_fenced(self):
        self.make()
        await self.insert("attention", 5_000_000, key="stuck-001", bridge_address=BRIDGE)
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request()
        self.assertEqual(caught.exception.status, 409)

    async def test_daily_amount_cap_skips_failed_refunded_and_old_ones(self):
        self.make(chain=Chain(balance=300_000_000), client=Client(balance=300_000_000))
        await self.insert("confirmed", 400_000_000, key="day-00001", hours_ago=2)
        await self.insert("confirmed", 400_000_000, key="day-00002", hours_ago=3)
        await self.insert("failed", 400_000_000, key="day-00003", hours_ago=1)
        await self.insert("refunded", 400_000_000, key="day-00004", hours_ago=1)
        await self.insert("confirmed", 400_000_000, key="old-00001", hours_ago=30)
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request("250.00")
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("$200.00 is left", caught.exception.message)
        out = await self.request("200.00", key="key-00000002")
        self.assertEqual(out["status"], "submitted")

    async def test_three_withdrawals_a_day(self):
        self.make()
        for n in range(3):
            await self.insert("confirmed", 2_000_000, key=f"count-{n:04d}", hours_ago=n + 1)
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request("2.00")
        self.assertEqual(caught.exception.status, 429)

    async def test_the_same_key_replays_and_sends_once(self):
        self.make()
        first = await self.request()
        again = await self.request()
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.client.sent), 1)
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request("26.00")
        self.assertEqual(caught.exception.status, 409)

    async def test_last_four_must_match(self):
        self.make()
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request(last4="0000")
        self.assertIn("last 4", caught.exception.message)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM withdrawals"), 0)
        out = await self.request(last4=DEST[-4:].upper())
        self.assertEqual(out["status"], "submitted")

    async def test_a_refused_destination_is_stopped_before_anything_is_reserved(self):
        self.make()
        for n, address in enumerate((WALLET, w.PUSD)):
            with self.assertRaises(w.WithdrawRefused) as caught:
                await self.service.request(self.user, amount="25.00", destination=address,
                                           confirm_last4=address[-4:],
                                           idempotency_key=f"key-refused-{n}")
            self.assertEqual(caught.exception.status, 400)
            with self.assertRaises(w.WithdrawRefused):
                await self.service.preview(self.user, "25.00", address)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM withdrawals"), 0)
        self.assertEqual(self.client.sent, [])

    async def test_minimum_is_the_higher_of_floor_and_bridge_and_maximum_holds(self):
        self.make(bridge=Bridge(minimum=3_000_000))
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request("2.50")
        self.assertIn("$3.00", caught.exception.message)
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request("500.01")
        self.assertIn("$500.00", caught.exception.message)

    async def test_a_short_quote_is_refused_before_reserving(self):
        self.make(bridge=Bridge(quote=w.Quote("q", 24_000_000, 23_900_000, 27)))
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM withdrawals"), 0)
        self.assertEqual(self.bridge.created, 0)

    async def test_a_bridge_address_failure_sends_nothing(self):
        for n, fail in enumerate(("create", "status")):
            with self.subTest(fail=fail):
                self.make(bridge=Bridge(fail=fail))
                out = await self.request(key=f"key-{fail}-0001")
                self.assertEqual(out["status"], "failed")
                self.assertIn("Not sent", out["error"])
                self.assertEqual(self.client.sent, [])
                self.assertEqual(self.kinds(), ["requested", "failed"])

    async def test_a_bridge_address_equal_to_the_destination_is_not_used(self):
        self.make(bridge=Bridge(address=DEST))
        out = await self.request()
        self.assertEqual(out["status"], "failed")
        self.assertEqual(self.client.sent, [])

    async def test_relayer_busy_is_retried_then_sent(self):
        busy = pm_errors.RequestRejectedError("wallet has in-flight action", status=400)
        self.make(client=Client(failures=[busy, busy]))
        out = await self.request()
        self.assertEqual(out["status"], "submitted")
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.clock.t, NOW + 2 * w.BUSY_RETRY_DELAY)

    async def test_relayer_busy_for_too_long_fails_without_sending(self):
        busy = pm_errors.RequestRejectedError("wallet busy: active action", status=400)
        self.make(client=Client(failures=[busy] * 20))
        out = await self.request()
        self.assertEqual(out["status"], "failed")
        self.assertIn("busy", out["error"])
        self.assertEqual(self.client.sent, [])
        self.assertIsNone(self.service.halted)

    async def test_relayer_rate_limit_is_retried_then_fails_without_sending(self):
        limited = pm_errors.RateLimitError("rate limited")
        self.make(client=Client(failures=[limited] * 20))
        out = await self.request()
        self.assertEqual(out["status"], "failed")
        self.assertEqual(self.client.sent, [])

    async def test_relayer_text_naming_addresses_is_logged_shortened(self):
        refused = pm_errors.RequestRejectedError(
            f"invalid batch from {WALLET} to {BRIDGE}", status=400)
        self.make(client=Client(failures=[refused]))
        with self.assertLogs("withdraw", level="WARNING") as logs:
            out = await self.request()
        self.assertEqual(out["status"], "failed")
        text = "\n".join(logs.output)
        self.assertNotIn(WALLET[2:], text)
        self.assertNotIn(BRIDGE[2:], text)
        self.assertIn(w.short(BRIDGE), text)

    async def test_a_policy_refusal_fails_and_pauses_withdrawals(self):
        refused = pm_errors.RequestRejectedError("target is not in the allowed list", status=400)
        self.make(client=Client(failures=[refused]))
        out = await self.request()
        self.assertEqual(out["status"], "failed")
        self.assertIsNotNone(self.service.halted)
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request(key="key-00000002")
        self.assertEqual(caught.exception.status, 503)

    async def test_an_unknown_submit_outcome_is_held_until_the_chain_answers(self):
        self.make(client=Client(failures=[httpx.ReadTimeout("timed out")]))
        out = await self.request()
        self.assertEqual(out["status"], "submitting")
        self.assertIsNotNone(out["held_until"])
        self.assertEqual(await w.held_units(self.db, WALLET), 25_000_000)
        await self.service.reconcile()
        self.assertEqual((await self.row(out["id"]))["status"], "submitting")
        # The transfer did reach the chain: the log search finds it, the receipt proves it.
        self.chain.logs.append(transfer_log(w.PUSD, WALLET, BRIDGE, 25_000_000))
        self.chain.receipts[SEND_TX] = receipt(transfer_log(w.PUSD, WALLET, BRIDGE, 25_000_000))
        await self.service.reconcile()
        self.assertEqual((await self.row(out["id"]))["status"], "bridging")
        self.assertEqual(self.chain.log_queries[-1][3], 1000 - 5)

    async def test_an_unknown_outcome_fails_only_after_the_batch_has_expired(self):
        self.make(client=Client(failures=[httpx.ReadTimeout("timed out")]))
        out = await self.request()
        self.clock.t += w.BATCH_EXPIRY_SECONDS - 1
        await self.service.reconcile()
        self.assertEqual((await self.row(out["id"]))["status"], "submitting")
        self.clock.t += 2
        await self.service.reconcile()
        stored = await self.row(out["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertIn("never executed", stored["error"])
        self.assertEqual(await w.held_units(self.db, WALLET), 0)

    async def test_a_relayer_failure_is_held_until_expiry_then_proven(self):
        failed = pm_errors.TransactionFailedError("execution reverted")
        self.make(client=Client(handle=Handle(error=failed)))
        out = await self.request()
        await self.service._tasks[out["id"]]
        stored = await self.row(out["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertGreaterEqual(self.clock.t - NOW, w.BATCH_EXPIRY_SECONDS)
        self.assertEqual(self.kinds(), ["requested", "failed"])

    async def test_a_stale_request_from_a_dead_process_is_failed(self):
        self.make()
        stale = await self.insert("requested", 2_000_000, key="stale-001", hours_ago=0.1)
        await self.service.reconcile()
        stored = await self.row(stale)
        self.assertEqual(stored["status"], "failed")
        self.assertIn("Nothing left your wallet", stored["error"])

    async def test_a_restart_moves_a_confirmed_transfer_to_bridging(self):
        self.make()
        sent = await self.insert("submitted", 3_000_000, key="sent-0001", hours_ago=0.01,
                                 bridge_address=BRIDGE, tx_hash=SEND_TX,
                                 submitting_at=w._iso(NOW - 30))
        self.chain.receipts[SEND_TX] = receipt(transfer_log(w.PUSD, WALLET, BRIDGE, 3_000_000))
        await self.service.reconcile()
        self.assertEqual((await self.row(sent))["status"], "bridging")
        self.assertEqual(self.kinds(), ["bridging"])

    async def test_a_bridge_failure_needs_attention(self):
        self.make()
        bridging = await self.insert("bridging", 3_000_000, key="brdg-0001", hours_ago=0.01,
                                     bridge_address=BRIDGE, bridge_baseline=0,
                                     bridging_at=w._iso(NOW - 30), min_out_units=2_999_000)
        self.bridge.history = [bridge_entry(3_000_000, status="FAILED")]
        with self.assertLogs("withdraw", level="ERROR"):
            await self.service.reconcile()
        stored = await self.row(bridging)
        self.assertEqual(stored["status"], "attention")
        self.assertEqual(stored["bridge_status"], "FAILED")
        self.assertEqual(self.kinds(), ["attention"])

    async def test_a_slow_bridge_needs_attention_after_the_timeout(self):
        self.make()
        bridging = await self.insert("bridging", 3_000_000, key="slow-0001", hours_ago=0.01,
                                     bridge_address=BRIDGE, bridge_baseline=0,
                                     bridging_at=w._iso(NOW - 60))
        self.bridge.history = [bridge_entry(3_000_000, status="PROCESSING")]
        await self.service.reconcile()
        self.assertEqual((await self.row(bridging))["status"], "bridging")
        self.clock.t += LIMITS.bridge_timeout
        await self.service.reconcile()
        self.assertEqual((await self.row(bridging))["status"], "attention")

    async def test_a_short_delivery_needs_attention(self):
        self.make()
        bridging = await self.insert("bridging", 3_000_000, key="shrt-0001", hours_ago=0.01,
                                     bridge_address=BRIDGE, bridge_baseline=0,
                                     bridging_at=w._iso(NOW - 30), min_out_units=2_999_000)
        self.bridge.history = [bridge_entry(3_000_000)]
        self.chain.receipts[DELIVERY_TX] = receipt(
            transfer_log(w.USDC, SOLVER, DEST, 2_500_000, DELIVERY_TX))
        with self.assertLogs("withdraw", level="CRITICAL"):
            await self.service.reconcile()
        stored = await self.row(bridging)
        self.assertEqual(stored["status"], "attention")
        self.assertEqual(stored["delivered_units"], 2_500_000)

    async def test_the_delivery_is_read_from_the_chain_when_the_bridge_hash_is_not_on_polygon(self):
        # 2026-10-22: /status named txHashes Polygonscan did not know, and the
        # USDC payment was a transaction of its own.
        self.make()
        bridging = await self.insert("bridging", 3_000_000, key="hash-0001", hours_ago=0.01,
                                     bridge_address=BRIDGE, bridge_baseline=0, submit_block=990,
                                     bridging_at=w._iso(NOW - 30), min_out_units=2_999_406)
        self.bridge.history = [bridge_entry(3_000_000, tx="0x" + "ab" * 32)]
        payment = transfer_log(w.USDC, SOLVER, DEST, 3_000_000, DELIVERY_TX)
        self.chain.logs.append(payment)
        self.chain.receipts[DELIVERY_TX] = receipt(payment)
        await self.service.reconcile()
        stored = await self.row(bridging)
        self.assertEqual((stored["status"], stored["delivery_tx_hash"], stored["delivered_units"]),
                         ("confirmed", DELIVERY_TX, 3_000_000))
        self.assertEqual(self.chain.log_queries[-1][1:4], (None, DEST, 990))
        self.assertEqual(self.kinds(), ["confirmed"])

    async def test_a_transfer_that_cannot_be_the_delivery_is_left_alone(self):
        self.make()
        bridging = await self.insert("bridging", 3_000_000, key="near-0001", hours_ago=0.01,
                                     bridge_address=BRIDGE, bridge_baseline=0, submit_block=990,
                                     bridging_at=w._iso(NOW - 30), min_out_units=2_999_406)
        self.bridge.history = [bridge_entry(3_000_000, tx=None)]
        # Under the minimum, over the amount, and one another withdrawal already counts.
        claimed = "0x" + "96" * 32
        for amount, tx in ((2_000_000, "0x" + "c4" * 32), (40_000_000, "0x" + "d5" * 32),
                           (3_000_000, claimed)):
            payment = transfer_log(w.USDC, SOLVER, DEST, amount, tx)
            self.chain.logs.append(payment)
            self.chain.receipts[tx] = receipt(payment)
        await self.insert("confirmed", 3_000_000, key="done-0001", delivery_tx_hash=claimed)
        await self.service.reconcile()
        self.assertEqual((await self.row(bridging))["status"], "bridging")
        self.assertEqual(self.kinds(), [])

    async def test_a_withdrawal_past_the_timeout_confirms_once_its_payment_is_on_chain(self):
        self.make()
        late = await self.insert("attention", 3_000_000, key="late-0001", hours_ago=0.6,
                                 bridge_address=BRIDGE, bridge_baseline=0, submit_block=990,
                                 bridge_status="COMPLETED", min_out_units=2_999_406,
                                 attention_at=w._iso(NOW - 60),
                                 error="The bridge has not delivered after 30 minutes.")
        self.bridge.history = [bridge_entry(3_000_000, tx="0x" + "ab" * 32)]
        await self.service.reconcile()
        self.assertEqual((await self.row(late))["status"], "attention")
        payment = transfer_log(w.USDC, SOLVER, DEST, 3_000_000, DELIVERY_TX)
        self.chain.logs.append(payment)
        self.chain.receipts[DELIVERY_TX] = receipt(payment)
        # Checked again only once ATTENTION_RECHECK_SECONDS have passed.
        asked = len(self.chain.log_queries)
        await self.service.reconcile()
        self.assertEqual(len(self.chain.log_queries), asked)
        self.clock.t += w.ATTENTION_RECHECK_SECONDS
        await self.service.reconcile()
        stored = await self.row(late)
        self.assertEqual((stored["status"], stored["delivery_tx_hash"], stored["error"]),
                         ("confirmed", DELIVERY_TX, None))
        self.assertIn("after the bridge timeout", stored["operator_note"])
        self.assertEqual(self.kinds(), ["confirmed"])
        self.assertIsNone(await self.service.paused_reason())

    async def test_polytrade_wallets_are_refused_as_destinations(self):
        # 2026-10-22: $3 went to another account's deposit wallet, where native
        # USDC sits beside its pUSD and never shows up as cash.
        self.make()
        other_wallet = to_checksum_address("0x" + "6f" * 20)
        other_signer = to_checksum_address("0x" + "70" * 20)
        await self.db.execute(
            "INSERT INTO users(id, signer_address, telegram_user_id, private_key_enc, created_at) "
            "VALUES(?,?,?,?,?)", (other_wallet, other_signer, 77, "enc", w._iso(NOW)))
        for address in (other_wallet, other_signer.lower()):
            with self.assertRaises(w.WithdrawRefused) as caught:
                await self.service.preview(self.user, "5.00", address)
            self.assertEqual(caught.exception.status, 400)
            self.assertIn("PolyTrade wallet", caught.exception.message)
        self.assertEqual(self.bridge.created, 0)

    async def test_two_unresolved_attention_rows_pause_everyone(self):
        self.make()
        other = "0x" + "9" * 40
        await self.db.execute(
            "INSERT INTO users(id, signer_address, telegram_user_id, private_key_enc, created_at) "
            "VALUES(?,?,?,?,?)", (other, "0x" + "8" * 40, 77, "enc", w._iso(NOW)))
        await self.insert("attention", 3_000_000, key="att-00001", bridge_address=BRIDGE)
        await self.insert("attention", 3_000_000, key="att-00002", bridge_address=BRIDGE,
                          user_id=other)
        self.assertIn("two bridge transfers", await self.service.paused_reason())
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.service.preview(self.user, "5.00", DEST)
        self.assertEqual(caught.exception.status, 503)

    async def test_availability_follows_the_switch_and_the_allowlist(self):
        self.make(enabled=False)
        self.assertFalse((await self.service.summary(self.user))["enabled"])
        with self.assertRaises(w.WithdrawRefused) as caught:
            await self.request()
        self.assertEqual(caught.exception.status, 503)
        self.make(allowlist=frozenset({TG + 1}))
        self.assertFalse(self.service.offered_to(self.user))
        self.make(allowlist=frozenset({TG}))
        self.assertTrue(self.service.offered_to(self.user))
        self.assertFalse(self.service.offered_to({**self.user, "telegram_user_id": None}))
        self.assertFalse(self.service.offered_to({**self.user, "signer_address": WALLET}))

    async def test_preview_quotes_without_reserving(self):
        self.make()
        out = await self.service.preview(self.user, "25.00", DEST.lower())
        self.assertEqual(out["destination"], DEST)
        self.assertEqual(out["receive_estimate"], "25.00")
        self.assertEqual(out["receive_minimum"], "24.99")
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM withdrawals"), 0)
        self.assertEqual(self.bridge.created, 0)


class OperatorToolTests(WithdrawTestCase):
    async def test_resolve_marks_a_verified_delivery_or_a_refund(self):
        self.make()
        first = await self.insert("attention", 3_000_000, key="att-00001", bridge_address=BRIDGE)
        self.chain.receipts[DELIVERY_TX] = receipt(
            transfer_log(w.USDC, SOLVER, DEST, 2_999_500, DELIVERY_TX))
        lines: list[str] = []
        self.assertEqual(await w.resolve_withdrawal(self.db, self.chain, first,
                                                    delivered=DELIVERY_TX, out=lines.append), 0)
        self.assertEqual((await self.row(first))["status"], "attention")      # a dry run
        self.assertEqual(await w.resolve_withdrawal(self.db, self.chain, first,
                                                    delivered=DELIVERY_TX, yes=True,
                                                    out=lines.append), 0)
        stored = await self.row(first)
        self.assertEqual((stored["status"], stored["delivered_units"]), ("confirmed", 2_999_500))
        second = await self.insert("attention", 3_000_000, key="att-00002", bridge_address=BRIDGE)
        self.assertEqual(await w.resolve_withdrawal(self.db, self.chain, second,
                                                    delivered="0x" + "87" * 32, yes=True,
                                                    out=lines.append), 1)
        self.assertEqual(await w.resolve_withdrawal(self.db, self.chain, second,
                                                    refunded="returned to the wallet as pUSD",
                                                    yes=True, out=lines.append), 0)
        self.assertEqual((await self.row(second))["status"], "refunded")
        await w.list_withdrawals(self.db, everything=True, out=lines.append)
        self.assertFalse(any(DEST[2:].lower() in line.lower() for line in lines))


class RouteTests(WithdrawTestCase):
    def body(self, **overrides) -> routes_user.WithdrawalBody:
        values = {"amount": "25.00", "destination": DEST, "confirm_last4": DEST[-4:],
                  "idempotency_key": "key-00000001", "init_data": "signed", **overrides}
        return routes_user.WithdrawalBody(**values)

    def request_with(self, service):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(withdrawer=service)))

    async def test_create_requires_a_fresh_step_up_from_the_linked_account(self):
        service = self.make()
        routes_user._withdraw_hits.clear()
        for answer in (None, {"id": TG + 1}):
            with patch("backend.core.auth.validate_init_data", return_value=answer) as check:
                with self.assertRaises(HTTPException) as caught:
                    await routes_user.create_withdrawal(self.body(), self.request_with(service),
                                                        user=self.user)
                self.assertEqual(caught.exception.status_code, 403)
                self.assertEqual(check.call_args.kwargs["max_age"], 300)
        self.assertEqual(self.client.sent, [])
        with patch("backend.core.auth.validate_init_data", return_value={"id": TG}):
            out = await routes_user.create_withdrawal(self.body(), self.request_with(service),
                                                      user=self.user)
        self.assertEqual(out["status"], "submitted")
        routes_user._withdraw_hits.clear()

    async def test_refusals_become_http_errors_and_attempts_are_rate_limited(self):
        service = self.make()
        routes_user._withdraw_hits.clear()
        with patch("backend.core.auth.validate_init_data", return_value={"id": TG}):
            for _ in range(5):
                with self.assertRaises(HTTPException) as caught:
                    await routes_user.create_withdrawal(self.body(confirm_last4="0000"),
                                                        self.request_with(service),
                                                        user=self.user)
                self.assertEqual(caught.exception.status_code, 400)
            with self.assertRaises(HTTPException) as caught:
                await routes_user.create_withdrawal(self.body(), self.request_with(service),
                                                    user=self.user)
            self.assertEqual(caught.exception.status_code, 429)
        routes_user._withdraw_hits.clear()

    async def test_a_switched_off_service_hides_the_panel(self):
        out = await routes_user.withdrawals(self.request_with(None), user=self.user)
        self.assertFalse(out["enabled"])
        with self.assertRaises(HTTPException) as caught:
            await routes_user.get_withdrawal("x" * 32, self.request_with(None), user=self.user)
        self.assertEqual(caught.exception.status_code, 404)


class SchemaTests(unittest.TestCase):
    def block(self, sql: str, header: str) -> str:
        body = sql.split(header, 1)[1].split(");", 1)[0]
        return " ".join(body.replace("public.users", "users").split())

    def test_migration_matches_the_boot_schema_and_locks_the_table(self):
        migration = (ROOT / "supabase/migrations/0012_withdrawals.sql").read_text()
        self.assertEqual(
            self.block(migration, "CREATE TABLE IF NOT EXISTS public.withdrawals ("),
            self.block(PG_SCHEMA_SQL, "CREATE TABLE IF NOT EXISTS withdrawals ("))
        for line in ("ALTER TABLE public.withdrawals ENABLE ROW LEVEL SECURITY",
                     "REVOKE ALL ON TABLE public.withdrawals FROM anon, authenticated",
                     "CREATE POLICY no_api_access ON public.withdrawals",
                     "uq_withdrawals_open_per_user"):
            self.assertIn(line, migration)
        self.assertIn("ALTER TABLE withdrawals      ENABLE ROW LEVEL SECURITY", PG_SCHEMA_SQL)
        self.assertIn("withdrawals", TABLES)

    def test_sqlite_and_postgres_name_the_same_columns(self):
        def columns(sql, header):
            body = sql.split(header, 1)[1].split(");", 1)[0]
            return [line.split()[0] for line in body.splitlines()
                    if line.strip() and not line.strip().startswith(("'", "UNIQUE", "--"))]

        self.assertEqual(columns(SCHEMA_SQL, "CREATE TABLE IF NOT EXISTS withdrawals ("),
                         columns(PG_SCHEMA_SQL, "CREATE TABLE IF NOT EXISTS withdrawals ("))


if __name__ == "__main__":
    unittest.main()
