"""Regressions for the 2026-10-18 review: engine correctness and multi-user load.

Each defect here was reproduced against main@866d154 before it was fixed.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from eth_account import Account
from fastapi import HTTPException

from backend.api import routes_auth, routes_positions, routes_traders
from backend.api.routes_user import ExportKeyBody, export_key
from backend.core import copy_engine as ce
from backend.core import dev_preview, wallet
from backend.core.copy_engine import Action, CopyEngine, plan_actions
from backend.core.execution import OrderResult, PreflightError, place_market_order
from backend.core.locks import KeyedLocks
from backend.core.polymarket import PolymarketClient, Position
from backend.core.telegram_alerts import TelegramPositionNotifier
from backend.db.database import Database, now_iso
from backend.db.models import PG_SCHEMA_SQL

USER = "0x" + "1" * 40
OTHER = "0x" + "3" * 40
TRADER = "0x" + "2" * 40
TOKEN = "token-1"
COND = "condition-1"


def leader_position(size: float, price: float = 0.5, token: str = TOKEN) -> Position:
    return Position(
        proxy_wallet=TRADER, asset=token, condition_id=COND, size=size,
        avg_price=price, cur_price=price, initial_value=size * price,
        current_value=size * price, cash_pnl=0.0, percent_pnl=0.0,
        realized_pnl=0.0, redeemable=False, mergeable=False,
        negative_risk=False, outcome="Yes", outcome_index=0,
        opposite_asset="", title="Market", slug="market", icon="",
        event_slug="event", end_date="")


def sell_trade(size: float, tx: str = "0xsell") -> SimpleNamespace:
    return SimpleNamespace(side="SELL", asset=TOKEN, size=size, price=0.5,
                           timestamp=int(time.time()), condition_id=COND,
                           outcome="Yes", slug="", title="", tx_hash=tx)


def http_404() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://clob.polymarket.com/book")
    return httpx.HTTPStatusError("404", request=request,
                                 response=httpx.Response(404, request=request))


async def client_factory(user):
    return object()


class EngineDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.add_user(USER)

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def add_user(self, user_id: str):
        await self.db.execute(
            "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
            (user_id, "encrypted", now_iso()))

    async def follow(self, user_id: str = USER, *, max_position_usd: float = 15.0,
                     follow_id: str = "f1") -> dict:
        await self.db.execute(
            "INSERT INTO followed_traders(id,user_id,trader_address,copy_ratio_pct,"
            "max_position_usd,paused,is_active,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (follow_id, user_id, TRADER, 1.0, max_position_usd, 0, 1, now_iso()))
        return await self.db.fetchone("SELECT * FROM followed_traders WHERE id=?", (follow_id,))

    async def open_row(self, *, shares=30.0, trader_shares=1000.0, notional=15.0,
                       status="open") -> dict:
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,"
            "opened_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("pos-1", USER, TRADER, COND, TOKEN, "Market", "YES", shares,
             trader_shares, 0.5, notional, status, now_iso()))
        return await self.row()

    async def row(self) -> dict:
        return await self.db.fetchone("SELECT * FROM copy_positions WHERE id='pos-1'")


# ---------------------------------------------------------------------------
# Pre-submission failures are not uncertain
# ---------------------------------------------------------------------------
class PreflightClassificationTests(unittest.IsolatedAsyncioTestCase):
    def pm(self, error):
        async def get_geoblock():
            return {"blocked": False}

        async def get_orderbook(token_id):
            raise error

        return SimpleNamespace(get_geoblock=get_geoblock, get_orderbook=get_orderbook)

    async def test_failed_book_read_raises_preflight_error(self):
        with self.assertRaises(PreflightError) as ctx:
            await place_market_order(object(), self.pm(httpx.ReadTimeout("slow")),
                                     TOKEN, "BUY", 5.0, reference_price=0.5)
        self.assertIsNone(ctx.exception.status_code)
        self.assertIsInstance(ctx.exception.__cause__, httpx.ReadTimeout)

    async def test_dead_book_keeps_its_status_code(self):
        with self.assertRaises(PreflightError) as ctx:
            await place_market_order(object(), self.pm(http_404()), TOKEN, "SELL", 5.0)
        self.assertEqual(404, ctx.exception.status_code)


class PreflightReleaseTests(EngineDb):
    def engine(self, *, book_error):
        async def get_geoblock():
            return {"blocked": False}

        async def get_orderbook(token_id):
            raise book_error

        async def get_positions(wallet_address, **kwargs):
            return []

        async def get_all_positions(wallet_address, **kwargs):
            return [], True

        pm = SimpleNamespace(get_geoblock=get_geoblock, get_orderbook=get_orderbook,
                             get_positions=get_positions, get_all_positions=get_all_positions)
        return CopyEngine(self.db, pm, client_factory=client_factory)

    def open_action(self) -> Action:
        p = SimpleNamespace(proxy_wallet=TRADER, asset=TOKEN, condition_id=COND,
                            size=2000.0, avg_price=0.5, cur_price=0.5,
                            current_value=1000.0, redeemable=False,
                            outcome="YES", slug="m", title="M")
        return Action(kind="open", token_id=TOKEN, condition_id=COND, outcome="YES",
                      side="BUY", amount=10.0, notional_usd=10.0, reference_price=0.5,
                      trader_shares=2000.0, position=p, trader_address=TRADER)

    async def test_book_outage_releases_the_claim_instead_of_freezing_the_token(self):
        await self.follow()
        engine = self.engine(book_error=httpx.ReadTimeout("book read timed out"))
        action = self.open_action()

        self.assertEqual(0.0, await engine._execute_buy(USER, object(), action))

        self.assertEqual(0, await self.db.fetchval("SELECT COUNT(*) FROM copy_open_claims"))
        self.assertEqual(0.0, engine._submitted_basis(USER, TOKEN))
        # The token is still copyable: a fresh reservation succeeds.
        self.assertIsNotNone(await engine._prepare_buy(USER, action))

    async def test_dead_book_on_close_still_routes_to_resolution(self):
        await self.open_row()

        async def place_order(*args, **kwargs):
            raise PreflightError("orderbook", http_404())

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place_order)
        engine._resolve_departed = AsyncMock()
        row = await self.row()
        action = Action(kind="close", token_id=TOKEN, side="SELL", amount=row["shares"], row=row)

        self.assertEqual(0.0, await engine._execute(USER, object(), action))
        engine._resolve_departed.assert_awaited_once()

    async def test_unsent_sell_reopens_the_row_and_does_not_raise(self):
        await self.open_row()

        async def place_order(*args, **kwargs):
            raise PreflightError("orderbook", httpx.ReadTimeout("slow"))

        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place_order)
        row = await self.row()
        action = Action(kind="close", token_id=TOKEN, side="SELL", amount=row["shares"], row=row)

        self.assertEqual(0.0, await engine._execute(USER, object(), action))
        self.assertEqual("open", (await self.row())["status"])


# ---------------------------------------------------------------------------
# Stale trader_shares anchor
# ---------------------------------------------------------------------------
class StaleAnchorTests(EngineDb):
    ROW = {"token_id": TOKEN, "id": "pos-1", "shares": 30.0, "trader_shares": 1000.0,
           "notional_usd": 15.0, "entry_price": 0.5}

    def test_capped_growth_reanchors_instead_of_doing_nothing(self):
        actions = plan_actions([leader_position(3000.0)], [dict(self.ROW)],
                               {"max_position_usd": 15.0}, 100.0)
        self.assertEqual(["reanchor"], [a.kind for a in actions])
        self.assertEqual(3000.0, actions[0].trader_shares)

    def test_blocked_opens_also_reanchor(self):
        row = dict(self.ROW, notional_usd=5.0)
        actions = plan_actions([leader_position(3000.0)], [row],
                               {"max_position_usd": 15.0}, 100.0, block_opens=True)
        self.assertEqual(["reanchor"], [a.kind for a in actions])

    def test_trim_after_reanchor_is_mirrored_proportionally(self):
        row = dict(self.ROW, trader_shares=3000.0)
        actions = plan_actions([leader_position(2000.0)], [row],
                               {"max_position_usd": 15.0}, 100.0)
        self.assertEqual(["resize"], [a.kind for a in actions])
        self.assertEqual("decrease", actions[0].subkind)
        self.assertAlmostEqual(10.0, actions[0].amount)

    def test_top_up_below_the_wallet_floor_is_not_planned(self):
        """Planned against 1.0 while _prepare_buy enforced ignore_below, a
        $1.50 top-up was emitted and refused on every tick."""
        row = dict(self.ROW, notional_usd=13.5, trader_shares=1000.0)
        actions = plan_actions([leader_position(1300.0)], [row],
                               {"max_position_usd": 15.0}, 100.0, ignore_below=2.0)
        self.assertEqual(["reanchor"], [a.kind for a in actions])

    async def test_engine_applies_reanchor_without_trading(self):
        row = await self.open_row()
        place_order = AsyncMock()
        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place_order)
        action = Action(kind="reanchor", token_id=TOKEN, trader_shares=3000.0, row=row)

        await engine._execute(USER, object(), action)

        self.assertEqual(3000.0, (await self.row())["trader_shares"])
        place_order.assert_not_awaited()

    async def test_reanchor_never_lowers_the_anchor(self):
        row = await self.open_row(trader_shares=5000.0)
        engine = CopyEngine(self.db, SimpleNamespace())
        await engine._execute(USER, object(), Action(
            kind="reanchor", token_id=TOKEN, trader_shares=3000.0, row=row))
        self.assertEqual(5000.0, (await self.row())["trader_shares"])

    async def test_a_killed_increase_reanchors(self):
        row = await self.open_row()
        place_order = AsyncMock()
        engine = CopyEngine(self.db, SimpleNamespace(), place_order=place_order)
        action = Action(kind="resize", subkind="increase", token_id=TOKEN, side="BUY",
                        amount=5.0, notional_usd=5.0, reference_price=0.5,
                        trader_shares=3000.0, row=row, position=leader_position(3000.0))
        engine._attempts[engine._attempt_key(USER, action)] = [
            ce.MAX_FILL_ATTEMPTS, engine._intent_fingerprint(action)]

        await engine._execute(USER, object(), action)

        self.assertEqual(3000.0, (await self.row())["trader_shares"])
        place_order.assert_not_awaited()

    def fast_engine(self, leader_positions):
        sold = []

        async def place_order(client, pm, token, side, amount, **kwargs):
            sold.append(amount)
            return OrderResult(ok=True, side=side, token_id=token,
                               filled_shares=amount, avg_price=0.5)

        async def get_positions(wallet_address, **kwargs):
            return list(leader_positions)

        engine = CopyEngine(self.db, SimpleNamespace(get_positions=get_positions),
                            place_order=place_order, client_factory=client_factory)
        return engine, sold

    async def test_fast_sell_defers_when_the_leader_holds_far_above_the_anchor(self):
        follow = await self.follow()
        await self.open_row(trader_shares=1000.0)
        engine, sold = self.fast_engine([leader_position(3000.0)])

        await engine._handle_leader_trade(follow, sell_trade(1000.0))

        self.assertEqual([], sold, "a one-third trim must not liquidate the copy")
        row = await self.row()
        self.assertEqual(("open", 30.0), (row["status"], row["shares"]))

    async def test_whole_position_exit_is_not_deferred_by_a_stale_anchor(self):
        """Leader grew 1,000 -> 3,000 and then sold all 3,000 in one trade
        before the indexer caught up: that is an exit, not a trim."""
        follow = await self.follow()
        await self.open_row(trader_shares=1000.0)
        engine, sold = self.fast_engine([leader_position(3000.0)])

        await engine._handle_leader_trade(follow, sell_trade(3000.0))

        self.assertEqual([30.0], sold)
        self.assertEqual("closed", (await self.row())["status"])

    async def test_fast_sell_still_full_exits_a_genuine_exit(self):
        follow = await self.follow()
        await self.open_row(trader_shares=1000.0)
        engine, sold = self.fast_engine([])          # leader is flat

        await engine._handle_leader_trade(follow, sell_trade(1000.0))

        self.assertEqual([30.0], sold)
        self.assertEqual("closed", (await self.row())["status"])

    async def test_fast_sell_is_proportional_with_an_accurate_anchor(self):
        follow = await self.follow()
        await self.open_row(trader_shares=3000.0)
        engine, sold = self.fast_engine([leader_position(3000.0)])   # pre-index

        await engine._handle_leader_trade(follow, sell_trade(1000.0))

        self.assertEqual(1, len(sold))
        self.assertAlmostEqual(10.0, sold[0])
        row = await self.row()
        self.assertEqual("open", row["status"])
        self.assertAlmostEqual(20.0, row["shares"])
        self.assertAlmostEqual(2000.0, row["trader_shares"])

    async def test_orders_are_placed_under_the_token_lock(self):
        follow = await self.follow()
        await self.open_row(trader_shares=1000.0)
        held = []

        async def place_order(client, pm, token, side, amount, **kwargs):
            held.append(len(engine._token_locks) == 1)
            return OrderResult(ok=True, side=side, token_id=token,
                               filled_shares=amount, avg_price=0.5)

        async def get_positions(wallet_address, **kwargs):
            return []

        engine = CopyEngine(self.db, SimpleNamespace(get_positions=get_positions),
                            place_order=place_order, client_factory=client_factory)
        await engine._handle_leader_trade(follow, sell_trade(1000.0))
        self.assertEqual([True], held)
        self.assertEqual(0, len(engine._token_locks), "idle token locks are dropped")


# ---------------------------------------------------------------------------
# A closed book with no known outcome is not a loss
# ---------------------------------------------------------------------------
class UnknownResolutionTests(EngineDb):
    async def resolve(self, *, prices=None, prices_error=None, redeems=None,
                      redeems_error=None) -> dict:
        row = await self.open_row(status="closing", shares=20.0, notional=10.0)

        async def get_resolved_prices(condition_id):
            if prices_error:
                raise prices_error
            return prices or {}

        async def get_redeems(user_id):
            if redeems_error:
                raise redeems_error
            return redeems or []

        engine = CopyEngine(self.db, SimpleNamespace(
            get_resolved_prices=get_resolved_prices, get_redeems=get_redeems))
        await engine._resolve_departed(USER, row)
        return await self.row()

    async def test_unknown_resolution_keeps_the_closing_fence(self):
        row = await self.resolve()
        self.assertEqual("closing", row["status"])
        self.assertIsNone(row["realized_pnl"])

    async def test_lookup_failures_are_not_booked_as_a_loss(self):
        row = await self.resolve(prices_error=RuntimeError("clob"),
                                 redeems_error=RuntimeError("data-api"))
        self.assertEqual("closing", row["status"])

    async def test_a_paid_redeem_still_books_a_win(self):
        row = await self.resolve(redeems=[{"conditionId": COND, "usdcSize": 20}])
        self.assertEqual(("resolved", 1.0), (row["status"], row["exit_price"]))

    async def test_a_flagged_loser_still_books_a_loss(self):
        row = await self.resolve(prices={TOKEN: 0.0})
        self.assertEqual(("resolved", 0.0), (row["status"], row["exit_price"]))
        self.assertAlmostEqual(-10.0, row["realized_pnl"])


# ---------------------------------------------------------------------------
# DEV_PREVIEW can never impersonate a real wallet
# ---------------------------------------------------------------------------
class DevPreviewGuardTests(EngineDb):
    async def test_never_returns_a_real_wallet(self):
        with patch("backend.core.dev_preview.DEV_PREVIEW", True):
            self.assertIsNone(await dev_preview.ensure_dev_preview_user(self.db))

    async def test_refuses_postgres(self):
        db = SimpleNamespace(is_pg=True, fetchone=AsyncMock(return_value=None))
        with patch("backend.core.dev_preview.DEV_PREVIEW", True):
            self.assertIsNone(await dev_preview.ensure_dev_preview_user(db))
        db.fetchone.assert_not_awaited()

    async def test_dev_login_refuses_when_real_wallets_exist(self):
        with patch("backend.api.routes_auth.DEV_PREVIEW", True), \
             patch("backend.core.dev_preview.DEV_PREVIEW", True):
            with self.assertRaises(HTTPException) as ctx:
                await routes_auth.dev_login(SimpleNamespace(set_cookie=lambda *a, **k: None),
                                            db=self.db)
        self.assertEqual(503, ctx.exception.status_code)
        user = await self.db.fetchone("SELECT api_token FROM users WHERE id=?", (USER,))
        self.assertIsNone(user["api_token"])


# ---------------------------------------------------------------------------
# Per-key locks
# ---------------------------------------------------------------------------
class KeyedLocksTests(unittest.IsolatedAsyncioTestCase):
    async def test_different_keys_do_not_block_each_other(self):
        locks = KeyedLocks()
        async with locks.hold("a"):
            async with asyncio.timeout(1):
                async with locks.hold("b"):
                    pass

    async def test_same_key_serializes(self):
        locks, inside, overlap = KeyedLocks(), [], []

        async def work(name):
            async with locks.hold("k"):
                overlap.append(bool(inside))
                inside.append(name)
                await asyncio.sleep(0.01)
                inside.remove(name)

        await asyncio.gather(work(1), work(2), work(3))
        self.assertEqual([False, False, False], overlap)

    async def test_idle_locks_are_dropped(self):
        locks = KeyedLocks()
        async with locks.hold("k"):
            self.assertEqual(1, len(locks))
        self.assertEqual(0, len(locks))


class PerUserRiskLockTests(EngineDb):
    async def test_one_users_buy_does_not_block_another_users_settings(self):
        locks = KeyedLocks()
        engine = CopyEngine(self.db, SimpleNamespace(), risk_lock=locks)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(copy_risk_lock=locks)))
        async with engine._risk_lock_for(USER):           # USER's BUY in flight
            async with asyncio.timeout(1):
                async with routes_traders._risk_write_guard(request, OTHER):
                    pass
                async with engine._risk_lock_for(OTHER):
                    pass

    async def test_a_plain_injected_lock_still_applies_globally(self):
        lock = asyncio.Lock()
        engine = CopyEngine(self.db, SimpleNamespace(), risk_lock=lock)
        self.assertIs(lock, engine._risk_lock_for(USER))


# ---------------------------------------------------------------------------
# One leader read per reconcile pass; users run concurrently
# ---------------------------------------------------------------------------
class LeaderDedupeTests(EngineDb):
    async def engine(self, get_all_positions):
        await self.add_user(OTHER)
        await self.follow(USER, follow_id="f1")
        await self.follow(OTHER, follow_id="f2")

        async def collateral(client):
            return 100.0

        return CopyEngine(self.db, SimpleNamespace(get_all_positions=get_all_positions),
                          client_factory=client_factory, collateral_fn=collateral)

    async def test_reconcile_pass_reads_each_leader_once(self):
        calls = []

        async def get_all_positions(wallet_address, **kwargs):
            calls.append(wallet_address)
            await asyncio.sleep(0)
            return [], True

        engine = await self.engine(get_all_positions)
        await engine._reconcile_tick()
        self.assertEqual(1, calls.count(TRADER))

    async def test_a_failed_leader_read_is_not_repeated_per_follower(self):
        calls = []

        async def get_all_positions(wallet_address, **kwargs):
            calls.append(wallet_address)
            raise RuntimeError("data-api 503")

        engine = await self.engine(get_all_positions)
        await engine._reconcile_tick()
        self.assertEqual(1, calls.count(TRADER))


class DetectConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_follower_does_not_hold_up_the_next(self):
        class Detector:
            async def new_trades(self, trader_address, since_ts):
                return [SimpleNamespace(timestamp=105, tx_hash="0xa", side="BUY", asset="t")]

        db = AsyncMock()
        db.fetchall.return_value = [
            {"user_id": USER, "trader_address": TRADER, "is_active": 1},
            {"user_id": OTHER, "trader_address": TRADER, "is_active": 1}]
        engine = CopyEngine(db, SimpleNamespace(), detector=Detector(), user_concurrency=2)
        engine._cursors[(USER, TRADER)] = engine._cursors[(OTHER, TRADER)] = 100
        other_done, seen = asyncio.Event(), {}

        async def handle(follow, trade):
            if follow["user_id"] == USER:
                try:
                    await asyncio.wait_for(other_done.wait(), 1)
                    seen[USER] = "ran-alongside"
                except asyncio.TimeoutError:
                    seen[USER] = "blocked"
            else:
                other_done.set()

        engine._handle_leader_trade = handle
        await asyncio.wait_for(engine._detect_tick(), 5)

        self.assertEqual("ran-alongside", seen[USER])
        self.assertEqual(105, engine._cursors[(USER, TRADER)])
        self.assertEqual(105, engine._cursors[(OTHER, TRADER)])


# ---------------------------------------------------------------------------
# Cached geoblock probe
# ---------------------------------------------------------------------------
class GeoblockCacheTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, status: int):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(status, json={"blocked": False, "country": "IE"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        return PolymarketClient(http), calls

    async def test_one_probe_serves_many_orders(self):
        pm, calls = await self.client(200)
        first = await pm.get_geoblock()
        second = await pm.get_geoblock()
        self.assertEqual(first, second)
        self.assertEqual(1, len(calls))

    async def test_a_failing_probe_is_not_retried_on_every_order(self):
        pm, calls = await self.client(500)
        for _ in range(3):
            with self.assertRaises(httpx.HTTPStatusError):
                await pm.get_geoblock()
        self.assertEqual(1, len(calls))


# ---------------------------------------------------------------------------
# Background Telegram delivery
# ---------------------------------------------------------------------------
class BackgroundNotifierTests(unittest.IsolatedAsyncioTestCase):
    def notifier(self, *responses):
        db = AsyncMock()
        db.fetchone.return_value = {"telegram_user_id": 42}
        http = AsyncMock()
        http.post.side_effect = list(responses)
        notifier = TelegramPositionNotifier(db, "bot-token", http=http, background=True)
        notifier.SEND_INTERVAL_SECONDS = 0
        notifier.RATE_LIMIT_FLOOR_SECONDS = 0
        return notifier, http

    async def test_enqueue_returns_before_delivery(self):
        notifier, http = self.notifier(SimpleNamespace(status_code=200))
        await notifier({"event": "opened", "user_id": "w"})
        self.assertEqual(0, http.post.await_count)
        await notifier.aclose()
        self.assertEqual(1, http.post.await_count)

    async def test_a_rate_limited_alert_is_retried(self):
        limited = SimpleNamespace(status_code=429,
                                  json=lambda: {"parameters": {"retry_after": 0}})
        notifier, http = self.notifier(limited, SimpleNamespace(status_code=200))
        await notifier({"event": "opened", "user_id": "w"})
        await notifier.aclose()
        self.assertEqual(2, http.post.await_count)


# ---------------------------------------------------------------------------
# Key export refuses a ciphertext that belongs to another signer
# ---------------------------------------------------------------------------
class ExportIntegrityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def key(account) -> str:
        raw = account.key.hex()
        return raw if raw.startswith("0x") else "0x" + raw

    async def export(self, *, signer, key):
        user = {"id": USER, "telegram_user_id": 123, "signer_address": signer,
                "private_key_enc": wallet.encrypt_private_key(key, "test-encryption-secret")}
        with patch("backend.api.routes_user.auth.validate_init_data", return_value={"id": 123}):
            return await export_key(ExportKeyBody(init_data="signed"), user=user)

    async def test_swapped_ciphertext_is_refused(self):
        mine, theirs = Account.create(), Account.create()
        with self.assertRaises(HTTPException) as ctx:
            await self.export(signer=mine.address, key=self.key(theirs))
        self.assertEqual(409, ctx.exception.status_code)

    async def test_matching_ciphertext_exports(self):
        mine = Account.create()
        result = await self.export(signer=mine.address, key=self.key(mine))
        self.assertEqual(self.key(mine), result["private_key"])


# ---------------------------------------------------------------------------
# Follow guards, dashboard resilience, small consistency fixes
# ---------------------------------------------------------------------------
class FollowGuardTests(EngineDb):
    def request(self):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            copy_risk_lock=KeyedLocks())))

    async def follow_route(self, address):
        return await routes_traders.follow(
            address, routes_traders.FollowBody(), self.request(),
            user={"id": USER, "signer_address": "0x" + "9" * 40}, db=self.db)

    async def test_own_wallet_cannot_be_copied(self):
        for address in (USER, "0x" + "9" * 40):
            with self.assertRaises(HTTPException) as ctx:
                await self.follow_route(address)
            self.assertEqual(400, ctx.exception.status_code)

    async def test_active_follows_are_capped(self):
        with patch.object(routes_traders, "MAX_FOLLOWS_PER_USER", 2):
            await self.follow_route("0x" + "a" * 40)
            await self.follow_route("0x" + "b" * 40)
            with self.assertRaises(HTTPException) as ctx:
                await self.follow_route("0x" + "c" * 40)
            self.assertEqual(409, ctx.exception.status_code)
            # Updating an existing active follow is not a new follow.
            await self.follow_route("0x" + "a" * 40)


class SizingSettingsTests(EngineDb):
    """Capital-share sizing settings, from the API to what the engine reads."""
    USER_ROW = {"id": USER, "signer_address": "0x" + "9" * 40}

    def request(self):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            copy_risk_lock=KeyedLocks())))

    async def test_capital_share_settings_persist_and_list(self):
        await self.follow()
        body = routes_traders.FollowSettings(sizing_mode="capital_share", capital_multiplier=12)

        result = await routes_traders.update_follow_settings(
            TRADER, body, self.request(), user=self.USER_ROW, db=self.db)

        self.assertEqual({"sizing_mode", "capital_multiplier"}, set(result["updated"]))
        [listed] = await routes_traders.following(user=self.USER_ROW, db=self.db)
        self.assertEqual(("capital_share", 12.0),
                         (listed["sizing_mode"], listed["capital_multiplier"]))
        stored = await self.db.fetchone("SELECT * FROM followed_traders WHERE id='f1'")
        risk = CopyEngine._follow_risk(stored)
        self.assertEqual((ce.CAPITAL_SHARE, 12.0), (risk["sizing_mode"], risk["multiplier"]))
        self.assertEqual(1, await self.db.fetchval(
            "SELECT risk_revision FROM users WHERE id=?", (USER,)),
            "a sizing change fences BUYs reserved under the old one")

    async def test_a_follow_sizes_by_position_share_until_switched(self):
        await self.follow()
        row = await self.db.fetchone("SELECT * FROM followed_traders WHERE id='f1'")
        self.assertEqual((None, None), (row["sizing_mode"], row["capital_multiplier"]))
        self.assertEqual(ce.POSITION_SHARE, CopyEngine._follow_risk(row)["sizing_mode"])

    async def test_the_schema_refuses_an_unknown_mode(self):
        await self.follow()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute(
                "UPDATE followed_traders SET sizing_mode='equity' WHERE id='f1'")

    def test_postgres_boot_schema_and_migration_add_the_same_columns(self):
        migration = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
                     / "0014_capital_share_sizing.sql").read_text()
        for column in ("sizing_mode TEXT", "capital_multiplier DOUBLE PRECISION"):
            with self.subTest(column=column):
                self.assertIn(f"followed_traders ADD COLUMN IF NOT EXISTS {column}",
                              PG_SCHEMA_SQL)
                self.assertIn(f"followed_traders ADD COLUMN IF NOT EXISTS {column}",
                              migration)
        self.assertIn("'position_share', 'capital_share'", migration)


class OpenPositionsResilienceTests(EngineDb):
    async def test_dashboard_survives_a_wallet_read_failure(self):
        await self.open_row()
        pmc = SimpleNamespace(get_positions=AsyncMock(side_effect=RuntimeError("data-api down")))
        rows = await routes_positions.open_positions(user={"id": USER}, db=self.db, pmc=pmc)
        self.assertEqual(["pos-1"], [r["id"] for r in rows])
        self.assertIsNone(rows[0]["current_price"])


class ConsistencyTests(EngineDb):
    def test_zero_max_open_means_unlimited_like_the_api(self):
        self.assertIsNone(CopyEngine._follow_risk({"max_open_positions": 0})["max_open"])
        self.assertEqual(3, CopyEngine._follow_risk({"max_open_positions": 3})["max_open"])

    async def test_trade_events_are_indexed_by_position(self):
        rows = await self.db.fetchall("PRAGMA index_list('trade_events')")
        self.assertIn("idx_trade_events_position", {r["name"] for r in rows})
        self.assertIn("idx_trade_events_position", PG_SCHEMA_SQL)

    def test_every_postgres_table_has_row_level_security(self):
        created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", PG_SCHEMA_SQL))
        secured = set(re.findall(r"ALTER TABLE (\w+)\s+ENABLE ROW LEVEL SECURITY", PG_SCHEMA_SQL))
        self.assertEqual(set(), created - secured)


if __name__ == "__main__":
    unittest.main()
