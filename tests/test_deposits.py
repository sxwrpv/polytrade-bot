"""Deposit alerts (backend/core/deposits.py): one Telegram message for each
deposit that lands in a user's wallet, and none for payouts or sale proceeds.

No network. A fake Polygon node answers the JSON-RPC calls DepositChain makes
(the finalized head, block times, receipts, and eth_getLogs with a node's
filter semantics) and refuses log ranges over 101 blocks, as the SDK's public
endpoint does. Transactions are built in the shapes read on Polygon on
2026-10-22 and 2026-10-23: a deposit through the onramp, the auto-redeem
operator's two payout paths, an adapter redemption, a merge sell and a plain
sell.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from eth_utils import to_checksum_address
from fastapi.testclient import TestClient
from polymarket._internal.environment import get_environment_config
from polymarket.environments import PRODUCTION

from backend.core import deposits as d
from backend.core import redeem
from backend.core import withdraw as w
from backend.core.health import Heartbeats
from backend.core.telegram_alerts import TelegramPositionNotifier
from backend.db.database import Database
from backend.db.models import PG_SCHEMA_SQL, SCHEMA_SQL, TABLES

ROOT = Path(__file__).resolve().parents[1]
WALLET = to_checksum_address("0x" + "1a" * 20)
OTHER = to_checksum_address("0x" + "2b" * 20)
DEPOSIT_ACCOUNT = "0x" + "3c" * 20       # the user's ERC-4337 deposit account
BUYER = "0x" + "4d" * 20
REDEEMER = "0x" + "5e" * 20              # hands the operator its USDC.e
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POSITION_MANAGER = "0x006F54F7f9A22e0000CC2AB60031000000ae9fEF"
COMBINATORIAL_MODULE = "0x30000034706c7d8e12009dab006be20000c031a8"
ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
MERGE_ADAPTER = "0xada1" + "0" * 32 + "9718"
VAULT = "0xC417fD8E9661c0d2120B64a04Bb3278C17E99DB1"
TG, OTHER_TG = 4242, 5151
BASE_TIME = 1_790_000_000


def block_time(number: int) -> int:
    return BASE_TIME + number * 3 // 2           # a block every 1.5 s


def _amount(units: int) -> str:
    return "0x" + format(units, "064x")


def transfer(token: str, sender: str, recipient: str, units: int) -> dict:
    return {"address": token.lower(), "data": _amount(units),
            "topics": [w.TRANSFER_TOPIC, w._topic(sender), w._topic(recipient)]}


def mint(recipient: str, units: int) -> dict:
    return transfer(w.PUSD, d.ZERO_ADDRESS, recipient, units)


def wrapped(caller: str, recipient: str, units: int) -> dict:
    return {"address": w.PUSD.lower(), "data": _amount(units),
            "topics": [d.WRAPPED_TOPIC, w._topic(caller), w._topic(w.USDC_E),
                       w._topic(recipient)]}


def position_move(contract: str, sender: str, recipient: str, *, operator: str,
                  batch: bool = False) -> dict:
    topic = d.TRANSFER_BATCH_TOPIC if batch else d.TRANSFER_SINGLE_TOPIC
    return {"address": contract.lower(), "data": "0x" + "00" * 64,
            "topics": [topic, w._topic(operator), w._topic(sender), w._topic(recipient)]}


def deposit_logs(wallet: str = WALLET, units: int = 44_000_000) -> list[dict]:
    """The deposit account's USDC.e, wrapped by the onramp for the wallet."""
    return [transfer(w.USDC_E, DEPOSIT_ACCOUNT, w.PUSD, units), mint(wallet, units),
            transfer(w.USDC_E, w.PUSD, VAULT, units),
            wrapped(d.COLLATERAL_ONRAMP, wallet, units)]


def operator_payout_logs(wallet: str = WALLET, units: int = 18_520_000) -> list[dict]:
    """The operator's older path: it pulls the positions, and its USDC.e is
    wrapped through the onramp, a mint like a deposit's."""
    return [position_move(CTF, wallet, d.AUTO_REDEEM_OPERATOR, operator=d.AUTO_REDEEM_OPERATOR),
            transfer(w.USDC_E, REDEEMER, d.AUTO_REDEEM_OPERATOR, units),
            transfer(w.USDC_E, d.AUTO_REDEEM_OPERATOR, w.PUSD, units), mint(wallet, units),
            transfer(w.USDC_E, w.PUSD, VAULT, units),
            wrapped(d.COLLATERAL_ONRAMP, wallet, units)]


def position_manager_payout_logs(wallet: str = WALLET, units: int = 617_743_235) -> list[dict]:
    """The operator's newer path: positions out of the position manager, pUSD
    minted to the operator and passed on."""
    return [position_move(POSITION_MANAGER, wallet, COMBINATORIAL_MODULE,
                          operator=d.AUTO_REDEEM_OPERATOR),
            mint(d.AUTO_REDEEM_OPERATOR, units),
            transfer(w.PUSD, d.AUTO_REDEEM_OPERATOR, wallet, units)]


def adapter_redemption_logs(wallet: str = WALLET, units: int = 5_000_000) -> list[dict]:
    """redeem.py's path: the adapter pulls both outcomes and wraps the payout."""
    return [position_move(CTF, wallet, ADAPTER, operator=ADAPTER, batch=True),
            mint(wallet, units), wrapped(ADAPTER, wallet, units)]


def merge_sell_logs(wallet: str = WALLET, units: int = 2_187_325) -> list[dict]:
    return [position_move(CTF, wallet, EXCHANGE, operator=EXCHANGE), mint(wallet, units),
            wrapped(MERGE_ADAPTER, wallet, units)]


def sell_logs(wallet: str = WALLET, units: int = 9_900_000) -> list[dict]:
    return [position_move(CTF, wallet, BUYER, operator=EXCHANGE),
            transfer(w.PUSD, BUYER, wallet, units)]


def matches(entry: dict, query: dict) -> bool:
    """eth_getLogs filter semantics: null matches anything, a list is an OR."""
    if entry["address"].lower() != query["address"].lower():
        return False
    topics = [t.lower() for t in entry["topics"]]
    for i, wanted in enumerate(query.get("topics") or []):
        if wanted is None:
            continue
        options = wanted if isinstance(wanted, list) else [wanted]
        if i >= len(topics) or topics[i] not in {o.lower() for o in options}:
            return False
    return True


class Node:
    """A Polygon JSON-RPC node behind httpx.MockTransport."""

    MAX_LOG_RANGE = 101                          # what the SDK's public endpoint accepts

    def __init__(self, head: int = 1000) -> None:
        self.head = head
        self.receipts: dict[str, dict] = {}
        self.ranges: list[tuple[int, int]] = []
        self.wallet_counts: list[int] = []
        self.hidden_receipts: set[str] = set()
        self.logs_fail_from: int | None = None    # eth_getLogs fails for ranges reaching this block
        self.down = False
        self.calls = 0
        self._hashes = (f"0x{n:064x}" for n in itertools.count(0xd0))
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def add(self, block: int, logs: list[dict]) -> str:
        tx = next(self._hashes)
        self.receipts[tx] = {
            "transactionHash": tx, "blockNumber": hex(block), "status": "0x1",
            "logs": [dict(entry, transactionHash=tx, blockNumber=hex(block), logIndex=hex(i))
                     for i, entry in enumerate(logs)]}
        return tx

    def chain(self) -> d.DepositChain:
        return d.DepositChain("https://polygon.invalid", client=self.client)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        method, params = body["method"], body["params"]

        def reply(result) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        if self.down:
            return httpx.Response(503)
        if method == "eth_getBlockByNumber":
            number = self.head if params[0] == "finalized" else int(params[0], 16)
            return reply({"number": hex(number), "timestamp": hex(block_time(number))})
        if method == "eth_getTransactionReceipt":
            tx = params[0]
            return reply(None if tx in self.hidden_receipts else self.receipts.get(tx))
        if method == "eth_getLogs":
            query = params[0]
            low, high = int(query["fromBlock"], 16), int(query["toBlock"], 16)
            if high - low + 1 > self.MAX_LOG_RANGE:
                return httpx.Response(400, json={"jsonrpc": "2.0", "id": body["id"], "error": {
                    "code": 35, "message": "ranges over 10000 blocks are not supported on free plan"}})
            if self.logs_fail_from is not None and high >= self.logs_fail_from:
                return httpx.Response(503)
            self.ranges.append((low, high))
            self.wallet_counts.append(len(query["topics"][3]))
            return reply([entry for receipt in self.receipts.values()
                          if low <= int(receipt["blockNumber"], 16) <= high
                          for entry in receipt["logs"] if matches(entry, query)])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "error": {"message": f"unexpected {method}"}})


def receipt(logs: list[dict], ok: bool = True) -> dict:
    return {"status": "0x1" if ok else "0x0", "logs": logs}


class RuleTests(unittest.TestCase):
    def test_an_onramp_mint_with_no_position_moving_is_a_deposit(self):
        self.assertEqual(d.deposit_units(receipt(deposit_logs()), WALLET), (44_000_000, None))
        self.assertEqual(d.deposit_units(receipt(deposit_logs()), WALLET.lower()),
                         (44_000_000, None))

    def test_payouts_and_sales_are_set_aside(self):
        for name, logs in (("operator payout", operator_payout_logs()),
                           ("position manager payout", position_manager_payout_logs()),
                           ("adapter redemption", adapter_redemption_logs()),
                           ("merge sell", merge_sell_logs()), ("sell", sell_logs())):
            units, reason = d.deposit_units(receipt(logs), WALLET)
            self.assertEqual(units, 0, name)
            self.assertIn("positions left the wallet", reason, name)

    def test_a_wrap_funded_by_the_auto_redeem_operator_is_a_payout(self):
        logs = [entry for entry in operator_payout_logs()
                if entry["topics"][0] != d.TRANSFER_SINGLE_TOPIC]
        self.assertEqual(d.deposit_units(receipt(logs), WALLET),
                         (0, "the auto-redeem operator funded the wrap (a payout)"))

    def test_only_this_wallets_own_positions_count(self):
        logs = deposit_logs() + [position_move(CTF, OTHER, BUYER, operator=EXCHANGE)]
        self.assertEqual(d.deposit_units(receipt(logs), WALLET), (44_000_000, None))
        self.assertEqual(d.deposit_units(receipt(logs), OTHER)[0], 0)

    def test_a_failed_transaction_or_an_unmatched_wrap_is_no_deposit(self):
        self.assertEqual(d.deposit_units(receipt(deposit_logs(), ok=False), WALLET),
                         (0, "the transaction failed"))
        self.assertEqual(d.deposit_units(None, WALLET), (0, "the transaction failed"))
        self.assertEqual(d.deposit_units(receipt(deposit_logs(OTHER)), WALLET),
                         (0, "the onramp wrapped nothing for this wallet"))
        adapter_only = [mint(WALLET, 5_000_000), wrapped(ADAPTER, WALLET, 5_000_000)]
        self.assertEqual(d.deposit_units(receipt(adapter_only), WALLET),
                         (0, "the onramp wrapped nothing for this wallet"))
        no_mint = [e for e in deposit_logs() if e != mint(WALLET, 44_000_000)]
        self.assertEqual(d.deposit_units(receipt(no_mint), WALLET)[1],
                         "the wallet was minted less pUSD than the onramp reports")

    def test_constants_match_the_chain_and_the_sdk(self):
        cfg = get_environment_config(PRODUCTION)
        # topic0 of the pUSD Wrapped event as read on Polygon, 2026-10-22.
        self.assertEqual(d.WRAPPED_TOPIC,
                         "0xc00a5c84859ae82a7f5e6a2773283fb525335d5b3195f61174aa1ecc7e15dd84")
        self.assertEqual(d.AUTO_REDEEM_OPERATOR.lower(), cfg.auto_redeem_operator.lower())
        self.assertEqual(w.PUSD.lower(), cfg.collateral_token.lower())
        self.assertIn(d.COLLATERAL_ONRAMP.lower(), w.protocol_addresses(PRODUCTION))


class DepositTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        for wallet, telegram_id in ((WALLET, TG), (OTHER, OTHER_TG)):
            await self.db.execute(
                "INSERT INTO users(id, telegram_user_id, private_key_enc, created_at) "
                "VALUES(?,?,?,?)", (wallet, telegram_id, "encrypted", w._iso(BASE_TIME)))
        self.node = Node()
        self.chain = self.node.chain()
        self.alerts: list[dict] = []
        self.now = block_time(self.node.head) + 30.0

    async def asyncTearDown(self):
        await self.node.client.aclose()
        await self.db.close()
        os.unlink(self.path)

    async def notify(self, event: dict) -> None:
        self.alerts.append(event)

    def watcher(self, **kwargs) -> d.DepositWatcher:
        kwargs.setdefault("notify", self.notify)
        return d.DepositWatcher(self.db, self.chain, clock=lambda: self.now, **kwargs)

    def advance(self, blocks: int) -> None:
        self.node.head += blocks
        self.now = block_time(self.node.head) + 30.0

    async def start_at(self, block: int) -> None:
        await self.db.execute(
            "INSERT INTO chain_cursors(name, last_block, updated_at) VALUES(?,?,?)",
            (d.CURSOR, block, w._iso(BASE_TIME)))

    async def cursor(self) -> int | None:
        return await self.db.fetchval(
            "SELECT last_block FROM chain_cursors WHERE name=?", (d.CURSOR,))

    async def rows(self) -> int:
        return await self.db.fetchval("SELECT COUNT(*) FROM deposits")


class WatcherTests(DepositTestCase):
    async def test_the_first_pass_starts_at_the_head_and_announces_nothing_older(self):
        self.node.add(900, deposit_logs())
        summary = await self.watcher().run_pass()
        self.assertTrue(summary.started)
        self.assertEqual(await self.cursor(), 1000)
        self.advance(20)
        await self.watcher().run_pass()
        self.assertEqual(self.alerts, [])
        self.assertEqual(await self.rows(), 0)

    async def test_a_deposit_is_recorded_and_announced_once(self):
        await self.start_at(1000)
        tx = self.node.add(1010, deposit_logs())
        self.advance(20)
        summary = await self.watcher().run_pass()
        self.assertEqual((summary.deposits, summary.set_aside, summary.blocks, summary.to_block),
                         (1, 0, 20, 1020))
        [alert] = self.alerts
        row = await self.db.fetchone("SELECT * FROM deposits")
        self.assertEqual(alert, {"event": "deposit_received", "user_id": WALLET,
                                 "deposit_id": row["id"], "amount_usd": "44.00", "tx_hash": tx})
        self.assertEqual((row["user_id"], row["tx_hash"], row["amount_units"], row["block_number"],
                          row["landed_at"]),
                         (WALLET, tx, 44_000_000, 1010, w._iso(block_time(1010))))
        self.advance(20)
        await self.watcher().run_pass()
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(await self.cursor(), 1040)

    async def test_payouts_and_sales_send_nothing(self):
        await self.start_at(1000)
        for block, logs in enumerate((operator_payout_logs(), position_manager_payout_logs(),
                                      adapter_redemption_logs(), merge_sell_logs(),
                                      sell_logs()), start=1001):
            self.node.add(block, logs)
        self.advance(20)
        summary = await self.watcher().run_pass()
        self.assertEqual(self.alerts, [])
        self.assertEqual(await self.rows(), 0)
        # Only the older operator path wraps through the onramp, so it is the
        # one candidate the receipt check has to set aside.
        self.assertEqual((summary.deposits, summary.set_aside), (0, 1))

    async def test_a_restart_or_a_second_process_sends_no_second_alert(self):
        await self.start_at(1000)
        self.node.add(1010, deposit_logs())
        self.advance(20)
        await asyncio.gather(self.watcher().run_pass(), self.watcher().run_pass())
        self.assertEqual(len(self.alerts), 1)
        # A process that stopped before saving its block reads the blocks again.
        await self.db.execute("UPDATE chain_cursors SET last_block=1000")
        await self.watcher().run_pass()
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(await self.rows(), 1)
        # A process that is behind never moves the saved block back.
        await self.watcher()._advance(900)
        self.assertEqual(await self.cursor(), 1020)

    async def test_each_log_query_stays_within_the_public_endpoints_range(self):
        await self.start_at(1000)
        self.node.add(1250, deposit_logs())
        self.advance(350)
        summary = await self.watcher().run_pass()
        self.assertEqual(self.node.ranges, [(1001, 1100), (1101, 1200), (1201, 1300),
                                            (1301, 1350)])
        self.assertEqual((summary.blocks, len(self.alerts), await self.cursor()), (350, 1, 1350))
        self.advance(200)
        with self.assertRaises(d.ChainReadError):
            await self.watcher(chunk_blocks=102).run_pass()

    async def test_a_failed_read_leaves_its_blocks_for_the_next_pass(self):
        await self.start_at(1000)
        self.node.add(1150, deposit_logs())
        self.advance(200)
        self.node.logs_fail_from = 1101
        with self.assertRaises(d.ChainReadError):
            await self.watcher().run_pass()
        self.assertEqual((await self.cursor(), self.alerts), (1100, []))
        self.node.logs_fail_from = None
        await self.watcher().run_pass()
        self.assertEqual((await self.cursor(), len(self.alerts)), (1200, 1))

    async def test_a_receipt_the_node_does_not_have_yet_is_read_again(self):
        await self.start_at(1000)
        tx = self.node.add(1010, deposit_logs())
        self.advance(20)
        self.node.hidden_receipts.add(tx)
        with self.assertRaises(d.ChainReadError):
            await self.watcher().run_pass()
        self.assertEqual(await self.cursor(), 1000)
        self.node.hidden_receipts.clear()
        await self.watcher().run_pass()
        self.assertEqual(len(self.alerts), 1)

    async def test_after_a_long_outage_it_reads_the_last_window_and_says_when_deposits_landed(self):
        await self.start_at(1000)
        self.node.add(5000, deposit_logs(units=17_480_000))
        late = self.node.add(9100, deposit_logs(units=114_730_000))
        self.node.head, self.now = 10_000, block_time(10_000) + 30.0
        with self.assertLogs("deposits", level="WARNING") as logs:
            summary = await self.watcher(max_catchup_blocks=1000).run_pass()
        self.assertEqual(summary.skipped_blocks, 8000)
        self.assertIn("resumes at block 9001", "\n".join(logs.output))
        [alert] = self.alerts
        self.assertEqual((alert["tx_hash"], alert["amount_usd"], alert["landed_at"]),
                         (late, "114.73", w._iso(block_time(9100))))

    async def test_wallets_are_queried_in_groups(self):
        for i in range(450):
            await self.db.execute(
                "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
                (to_checksum_address(f"0x{i + 1:040x}"), "encrypted", w._iso(BASE_TIME)))
        await self.start_at(1000)
        self.advance(20)
        await self.watcher().run_pass()
        self.assertEqual(sorted(self.node.wallet_counts), [52, 200, 200])

    async def test_dust_below_a_cent_gets_no_alert(self):
        await self.start_at(1000)
        self.node.add(1010, deposit_logs(units=9_999))
        self.advance(20)
        await self.watcher().run_pass()
        self.assertEqual((self.alerts, await self.rows()), ([], 0))

    async def test_log_lines_name_wallets_in_short_form(self):
        await self.start_at(1000)
        self.node.add(1005, operator_payout_logs())
        self.node.add(1010, deposit_logs())
        self.advance(20)
        with self.assertLogs("deposits", level="INFO") as logs:
            await self.watcher().run_pass()
        text = "\n".join(logs.output)
        self.assertIn(f"DEPOSIT {WALLET[:10]} +$44.00 pUSD", text)
        self.assertIn(f"set aside {WALLET[:10]}", text)
        self.assertNotIn(WALLET[2:].lower(), text.lower())

    async def test_a_deposit_reaches_its_owner_on_telegram_once_and_payouts_send_nothing(self):
        http = AsyncMock()
        http.post.return_value = SimpleNamespace(status_code=200)
        notifier = TelegramPositionNotifier(self.db, "bot-token", http=http)
        await self.start_at(1000)
        for block, logs in ((1001, operator_payout_logs()), (1002, position_manager_payout_logs()),
                            (1003, adapter_redemption_logs()), (1005, merge_sell_logs()),
                            (1006, sell_logs()), (1007, operator_payout_logs(OTHER))):
            self.node.add(block, logs)
        tx = self.node.add(1004, deposit_logs())
        self.advance(20)
        await self.watcher(notify=notifier).run_pass()
        self.advance(20)
        await self.watcher(notify=notifier).run_pass()
        await self.db.execute("UPDATE chain_cursors SET last_block=1000")
        await self.watcher(notify=notifier).run_pass()

        http.post.assert_awaited_once()
        sent = http.post.await_args.kwargs["json"]
        self.assertEqual(sent["chat_id"], TG)
        for part in ("DEPOSIT RECEIVED", "$44.00", "pUSD", f"{WALLET[:6]}…{WALLET[-4:]}",
                     f"https://polygonscan.com/tx/{tx}"):
            self.assertIn(part, sent["text"])
        self.assertNotIn(WALLET[2:].lower(), sent["text"].lower())


class SchemaTests(unittest.TestCase):
    def block(self, sql: str, header: str) -> str:
        body = sql.split(header, 1)[1].split(");", 1)[0]
        return " ".join(body.replace("public.users", "users").split())

    def test_the_migration_matches_the_boot_schema_and_locks_both_tables(self):
        migration = (ROOT / "supabase/migrations/0013_deposits.sql").read_text()
        for table in ("deposits", "chain_cursors"):
            self.assertEqual(
                self.block(migration, f"CREATE TABLE IF NOT EXISTS public.{table} ("),
                self.block(PG_SCHEMA_SQL, f"CREATE TABLE IF NOT EXISTS {table} ("))
            for line in (f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY",
                         f"REVOKE ALL ON TABLE public.{table} FROM anon, authenticated",
                         f"CREATE POLICY no_api_access ON public.{table}"):
                self.assertIn(line, migration)
            self.assertRegex(PG_SCHEMA_SQL, rf"ALTER TABLE {table}\s+ENABLE ROW LEVEL SECURITY")
            self.assertIn(table, TABLES)
        self.assertIn("UNIQUE(user_id, tx_hash)", migration)

    def test_sqlite_and_postgres_name_the_same_columns(self):
        def columns(sql: str, header: str) -> list[str]:
            body = sql.split(header, 1)[1].split(");", 1)[0]
            return [line.split()[0] for line in body.splitlines()
                    if line.strip() and not line.strip().startswith(("UNIQUE", "--"))]

        for table in ("deposits", "chain_cursors"):
            header = f"CREATE TABLE IF NOT EXISTS {table} ("
            self.assertEqual(columns(SCHEMA_SQL, header), columns(PG_SCHEMA_SQL, header))


class LoopWiringTests(unittest.TestCase):
    def boot(self, **env):
        from backend import main
        with patch.object(main, "_deposit_loop", new=AsyncMock()) as loop, \
                patch.dict(os.environ, env):
            if "DEPOSIT_ALERTS_AUTOSTART" not in env:
                os.environ.pop("DEPOSIT_ALERTS_AUTOSTART", None)
            with TestClient(main.app):
                pass
        return loop

    def test_it_runs_by_default_where_the_bot_can_send(self):
        self.boot().assert_called_once()

    def test_it_stays_off_when_switched_off_or_without_a_bot_token(self):
        from backend import main
        self.boot(DEPOSIT_ALERTS_AUTOSTART="0").assert_not_called()
        with patch.object(main, "TELEGRAM_BOT_TOKEN", ""):
            self.boot().assert_not_called()


class LoopTests(DepositTestCase):
    async def run_loop(self) -> tuple[dict, list, list]:
        from backend import main
        hb = Heartbeats()
        app = SimpleNamespace(state=SimpleNamespace(db=self.db, position_notifier=self.notify))
        stop = asyncio.Event()
        urls, closed = [], []
        client = self.node.client

        class Chain(d.DepositChain):
            def __init__(chain, url):
                urls.append(url)
                super().__init__(url, client=client)

            async def aclose(chain):
                closed.append(True)

        with patch.object(main, "heartbeats", hb), patch.object(d, "DepositChain", Chain), \
                patch.dict(os.environ, {"DEPOSIT_POLL_SECONDS": "3600"}):
            task = asyncio.create_task(main._deposit_loop(app, stop))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if self.node.calls:
                    break
            await asyncio.sleep(0.05)       # let the pass finish
            stop.set()
            await asyncio.wait_for(task, timeout=5)
        return hb.snapshot()["deposits"], urls, closed

    async def test_a_completed_pass_marks_the_heartbeat_on_the_sdks_public_endpoint(self):
        snap, urls, closed = await self.run_loop()
        self.assertTrue(snap["registered"])
        self.assertEqual((snap["successes"], snap["interval_seconds"]), (1, 3600))
        # POLYGON_RPC_URL is unset in the tests, as on production today.
        self.assertEqual(urls, [redeem.default_rpc_url()])
        self.assertEqual(closed, [True])
        self.assertEqual(await self.cursor(), 1000)

    async def test_a_failed_pass_leaves_the_heartbeat_stale(self):
        self.node.down = True
        with self.assertLogs("main", level="WARNING") as logs:
            snap, _, closed = await self.run_loop()
        self.assertEqual(snap["successes"], 0)
        self.assertTrue(snap["stale"])
        self.assertIn("deposit pass incomplete", "\n".join(logs.output))
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
