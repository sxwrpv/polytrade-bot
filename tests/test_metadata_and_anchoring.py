"""Regression tests for the 2026-07-12 'perfection pass'.

Covers: fast-path opens filling market metadata from the leader's indexed
position when the detector supplies none (the on-chain OrderFilled event
carries no condition/title/slug), the reconciler backfilling blind rows,
the no-condition guard that stops fictional $0 losses, and the managed
close anchoring the slippage tolerance to the live mark.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.api.routes_positions import CloseBody, close_position
from backend.core.copy_engine import CopyEngine
from backend.core.execution import OrderResult
from backend.db.database import Database, now_iso

USER = "0x" + "1" * 40
TRADER = "0x" + "2" * 40
TOKEN = "token-1"
CONDITION = "0x" + "c" * 64


def leader_position(*, size=2000.0, price=0.5):
    return SimpleNamespace(
        proxy_wallet=TRADER, asset=TOKEN, condition_id=CONDITION, size=size,
        avg_price=price, cur_price=price, current_value=size * price,
        redeemable=False, outcome="Yes", slug="real-market-slug",
        title="Real Market Title", event_slug="real-market-slug")


class MetadataDbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.db.execute(
            "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
            (USER, "encrypted", now_iso()))
        await self.db.execute(
            "INSERT INTO followed_traders(id, user_id, trader_address, copy_ratio_pct, "
            "max_position_usd, paused, is_active, created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("follow-1", USER, TRADER, 1.0, 50.0, 0, 1, now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)


class FastPathMetadataTests(MetadataDbTestCase):
    async def test_onchain_trade_without_metadata_records_leader_metadata(self):
        """OnChainDetector trades carry no condition/title/slug — the open row
        must take them from the leader's indexed position, or the position can
        never be matched to its market resolution."""
        async def get_positions(wallet, *, size_threshold=1.0, limit=500,
                                offset=0, sort_by="CURRENT", market=None):
            if wallet == TRADER:
                return [leader_position()]
            return []      # the user's wallet holds nothing yet

        async def get_all_positions(wallet, *, size_threshold=0.0,
                                    page_size=500, max_pages=6):
            return [], True

        async def place(client, pm, token, side, amount, **kwargs):
            return OrderResult(ok=True, side=side, filled_shares=amount / 0.5,
                               avg_price=0.5)

        async def client_factory(user):
            return object()

        async def collateral(client):
            return 100.0

        pm = SimpleNamespace(get_positions=get_positions,
                             get_all_positions=get_all_positions)
        engine = CopyEngine(self.db, pm, place_order=place,
                            client_factory=client_factory,
                            collateral_fn=collateral)
        follow = await self.db.fetchone("SELECT * FROM followed_traders")
        onchain_trade = SimpleNamespace(       # exactly what _decode emits
            asset=TOKEN, side="BUY", size=100.0, price=0.5,
            timestamp=int(time.time()), condition_id="", outcome="",
            slug="", title="", tx_hash="0xabc")

        await engine._handle_leader_trade(follow, onchain_trade)

        row = await self.db.fetchone(
            "SELECT * FROM copy_positions WHERE user_id=? AND token_id=?",
            (USER, TOKEN))
        self.assertIsNotNone(row)
        self.assertEqual(CONDITION, row["condition_id"])
        self.assertEqual("Real Market Title", row["market_title"])
        self.assertEqual("YES", row["outcome"])


class BackfillTests(MetadataDbTestCase):
    async def test_reconciler_backfills_blind_rows(self):
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?)",
            ("blind", USER, TRADER, "", TOKEN, "", "YES", 10.0, 2000.0, 0.5,
             5.0, now_iso()))

        async def get_all_positions(wallet, *, size_threshold=1.0,
                                    page_size=500, max_pages=6):
            return [leader_position()], True

        async def client_factory(user):
            return object()

        async def collateral(client):
            return 100.0

        pm = SimpleNamespace(get_all_positions=get_all_positions)
        engine = CopyEngine(self.db, pm, client_factory=client_factory,
                            collateral_fn=collateral)
        follow = await self.db.fetchone("SELECT * FROM followed_traders")

        await engine._sync_user(USER, [follow])

        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='blind'")
        self.assertEqual(CONDITION, row["condition_id"])
        self.assertEqual("Real Market Title", row["market_title"])
        self.assertEqual("real-market-slug", row["market_slug"])

    async def test_departed_row_without_condition_is_flagged_not_zeroed(self):
        """A dead-market position with no condition_id must be flagged for
        reconciliation — booking it as a $0 loss would fabricate a loss for
        what may be a winning position."""
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,entry_price,notional_usd,status,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'closing',?)",
            ("blind", USER, TRADER, "", TOKEN, "", "YES", 10.0, 0.5, 5.0, now_iso()))
        row = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='blind'")
        engine = CopyEngine(self.db, SimpleNamespace())

        await engine._resolve_departed(USER, row)

        fresh = await self.db.fetchone("SELECT * FROM copy_positions WHERE id='blind'")
        self.assertEqual("reconciliation_required", fresh["status"])
        self.assertIsNone(fresh["realized_pnl"])


class AnchoredCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_close_anchors_slippage_to_live_mark(self):
        row = {
            "id": "position", "token_id": TOKEN, "condition_id": CONDITION,
            "shares": 12.0, "entry_price": 0.35, "notional_usd": 4.2,
        }
        db = SimpleNamespace(
            fetchone=AsyncMock(return_value=row),
            claim_managed_sell=AsyncMock(return_value=True),
            try_transition=AsyncMock(return_value=True),
            execute=AsyncMock(),
        )

        class PM:
            async def get_positions(self, user_id, size_threshold=0, market=None):
                assert market == CONDITION       # targeted read
                return [SimpleNamespace(asset=TOKEN, cur_price=0.42)]

        result = OrderResult(ok=False, reason="test stop")
        with (
            patch("backend.api.routes_positions.get_user_client",
                  AsyncMock(return_value=object())),
            patch("backend.api.routes_positions.execution.place_market_order",
                  AsyncMock(return_value=result)) as place,
        ):
            await close_position(
                "position", SimpleNamespace(), CloseBody(acceptable_slippage_pct=4.5),
                user={"id": USER}, db=db, pmc=PM())

        self.assertEqual(0.42, place.await_args.kwargs["reference_price"])
        self.assertEqual(4.5, place.await_args.kwargs["max_slippage_pct"])


if __name__ == "__main__":
    unittest.main()
