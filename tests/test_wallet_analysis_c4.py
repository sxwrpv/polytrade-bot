"""C4 contracts for one-fetch wallet analysis orchestration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, call, patch

from backend.api import routes_traders
from backend.core import polymarket, trader_stats


ADDRESS = "0x" + "a" * 40


@dataclass(frozen=True)
class PreviewTrade:
    timestamp: int
    side: str = "BUY"
    usd_size: float = 1.0
    asset: str = "token-1"
    condition_id: str = "market-1"
    size: float = 1.0
    price: float = 1.0
    tx_hash: str = "tx-default"


@dataclass(frozen=True)
class PreviewPosition:
    asset: str
    size: float = 1.0
    redeemable: bool = False
    cash_pnl: float = 0.0
    condition_id: str = "market-1"
    cur_price: float = 1.0


class WalletAnalysisOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def db():
        async def fetchone(sql, _params):
            if "SELECT *" in sql:
                return {"address": ADDRESS, "total_pnl": 123.0}
            return None

        return SimpleNamespace(fetchone=AsyncMock(side_effect=fetchone))

    @staticmethod
    def pm(*, trades=None):
        return SimpleNamespace(
            get_trade_history=AsyncMock(return_value=list(trades or [])),
            get_redeems=AsyncMock(return_value=[]),
            get_positions=AsyncMock(return_value=[PreviewPosition("token-1")]),
            get_leaderboard_user=AsyncMock(return_value=SimpleNamespace(
                pnl=123.0, vol=456.0, user_name="alice", x_username=None,
            )),
        )

    async def test_profile_uses_one_bounded_fetch_path_and_reuses_rows_for_preview(self):
        # Deliberately oldest-first: the preview contract must not rely on an
        # accidental mock/upstream ordering guarantee.
        trades = [PreviewTrade(timestamp=i) for i in range(30)]
        pm = self.pm(trades=trades)
        db = self.db()

        with patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert:
            response = await routes_traders.trader_profile(
                ADDRESS.upper().replace("0X", "0x"), user={"id": "viewer"}, db=db, pmc=pm,
            )

        pm.get_trade_history.assert_awaited_once_with(ADDRESS, limit=1000, offset=0)
        pm.get_redeems.assert_awaited_once_with(ADDRESS, limit=1000, offset=0)
        pm.get_positions.assert_awaited_once_with(
            ADDRESS, size_threshold=0, limit=500,
        )
        pm.get_leaderboard_user.assert_awaited_once_with(ADDRESS)
        upsert.assert_awaited_once()
        self.assertEqual([29, 28, 27, 26, 25], [t["timestamp"] for t in response["recent_trades"][:5]])
        self.assertEqual(25, len(response["recent_trades"]))
        self.assertEqual("token-1", response["positions"][0]["asset"])
        self.assertEqual(123.0, response["total_pnl"])

    async def test_profile_serializes_production_trade_and_position_dataclasses(self):
        trade = polymarket.Trade.from_api({
            "proxyWallet": ADDRESS,
            "timestamp": 123,
            "conditionId": "market-production",
            "side": "BUY",
            "asset": "token-production",
            "price": "0.42",
            "size": "3",
            "usdcSize": "1.26",
            "transactionHash": "0xfeed",
        })
        position = polymarket.Position.from_api({
            "proxyWallet": ADDRESS,
            "asset": "token-production",
            "conditionId": "market-production",
            "size": "3",
            "curPrice": "0.42",
            "cashPnl": "0.12",
        })
        pm = self.pm(trades=[trade])
        pm.get_positions.return_value = [position]

        with patch.object(trader_stats, "_upsert", new=AsyncMock()):
            response = await routes_traders.trader_profile(
                ADDRESS, user={"id": "viewer"}, db=self.db(), pmc=pm,
            )

        self.assertEqual([asdict(trade)], response["recent_trades"])
        self.assertEqual([asdict(position)], response["positions"])

    async def test_structured_analysis_exposes_fetched_rows_without_refetch(self):
        trades = [PreviewTrade(timestamp=2), PreviewTrade(timestamp=1)]
        positions = [PreviewPosition("token-1")]
        pm = self.pm(trades=trades)
        pm.get_positions.return_value = positions

        with patch.object(trader_stats, "_upsert", new=AsyncMock()):
            analysis = await trader_stats.refresh_trader_analysis(ADDRESS, self.db(), pm)

        self.assertEqual(positions, analysis.positions)
        self.assertEqual(trades, analysis.trades)
        self.assertEqual(trades, analysis.recent_trades)
        self.assertEqual(1, pm.get_positions.await_count)
        self.assertEqual(1, pm.get_trade_history.await_count)

    async def test_trade_fetch_failure_propagates_and_writes_no_zero_cache(self):
        pm = self.pm()
        pm.get_trade_history.side_effect = RuntimeError("activity unavailable")

        with patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert:
            with self.assertRaisesRegex(RuntimeError, "activity unavailable"):
                await trader_stats.refresh_trader_analysis(ADDRESS, self.db(), pm)

        upsert.assert_not_awaited()
        pm.get_trade_history.assert_awaited_once_with(ADDRESS, limit=1000, offset=0)
        pm.get_redeems.assert_not_awaited()
        pm.get_positions.assert_not_awaited()
        pm.get_leaderboard_user.assert_not_awaited()

    async def test_overlapping_trade_pages_dedupe_exact_rows_but_keep_same_tx_fills(self):
        now = int(time.time())
        first = [
            PreviewTrade(timestamp=now - i, asset=f"token-{i}", tx_hash=f"tx-{i}")
            for i in range(1000)
        ]
        first[10] = PreviewTrade(
            timestamp=now - 10, asset="same-tx-a", size=1.0, tx_hash="shared-tx",
        )
        same_tx_distinct_fill = PreviewTrade(
            timestamp=now - 10, asset="same-tx-b", size=2.0, tx_hash="shared-tx",
        )
        second = [first[-1], same_tx_distinct_fill, first[-1]]
        pm = self.pm()
        pm.get_trade_history.side_effect = [first, second]

        with patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert:
            analysis = await trader_stats.refresh_trader_analysis(ADDRESS, self.db(), pm)

        self.assertEqual(1001, len(analysis.trades))
        self.assertEqual(1001, upsert.await_args_list[0].args[2]["total_trades"])
        self.assertIs(first[0], analysis.trades[0])
        self.assertEqual([first[-1], same_tx_distinct_fill], analysis.trades[-2:])
        self.assertEqual(
            [
                call(ADDRESS, limit=1000, offset=0),
                call(ADDRESS, limit=1000, offset=1000),
            ],
            pm.get_trade_history.await_args_list,
        )

    async def test_recent_preview_is_globally_newest_first_unique_and_capped(self):
        now = int(time.time())
        first = [
            PreviewTrade(timestamp=now - i, asset=f"first-{i}", tx_hash=f"first-{i}")
            for i in range(1000)
        ]
        page_overlap = first[0]
        second = [
            page_overlap,
            *[
                PreviewTrade(
                    timestamp=now + 100 - i,
                    asset=f"second-{i}", tx_hash=f"second-{i}",
                )
                for i in range(20)
            ],
        ]
        pm = self.pm()
        pm.get_trade_history.side_effect = [first, second]

        with patch.object(trader_stats, "_upsert", new=AsyncMock()):
            analysis = await trader_stats.refresh_trader_analysis(ADDRESS, self.db(), pm)

        preview = analysis.recent_trades
        self.assertEqual(25, len(preview))
        self.assertEqual(
            sorted((trade.timestamp for trade in preview), reverse=True),
            [trade.timestamp for trade in preview],
        )
        self.assertEqual(25, len({trade.asset for trade in preview}))
        self.assertEqual(1, sum(trade is page_overlap for trade in preview))

    async def test_activity_walk_stops_at_four_page_bound(self):
        now = int(time.time())
        fetch = AsyncMock(side_effect=[
            [
                PreviewTrade(
                    timestamp=now, asset=f"page-{page}-row-{i}",
                    tx_hash=f"page-{page}-tx-{i}",
                )
                for i in range(1000)
            ]
            for page in range(4)
        ])

        rows, covered = await trader_stats._fetch_activity_window(
            fetch, days=90, max_pages=4,
        )

        self.assertEqual(4000, len(rows))
        self.assertFalse(covered)
        self.assertEqual(
            [
                call(limit=1000, offset=0),
                call(limit=1000, offset=1000),
                call(limit=1000, offset=2000),
                call(limit=1000, offset=3000),
            ],
            fetch.await_args_list,
        )

    async def test_redeem_pages_use_recursive_exact_row_deduplication(self):
        now = int(time.time())
        overlap = {
            "timestamp": now,
            "conditionId": "market-1",
            "usdcSize": "5",
            "metadata": {"legs": ["yes", 1]},
        }
        first = [
            {
                "timestamp": now,
                "conditionId": f"market-{i}",
                "usdcSize": str(i),
                "metadata": {"legs": ["yes", i]},
            }
            for i in range(999)
        ] + [overlap]
        distinct = {
            **overlap,
            "metadata": {"legs": ["yes", 2]},
        }
        fetch = AsyncMock(side_effect=[[dict(row) for row in first], [overlap, distinct]])

        rows, covered = await trader_stats._fetch_activity_window(
            fetch, days=90, max_pages=2,
        )

        self.assertTrue(covered)
        self.assertEqual(1001, len(rows))
        self.assertEqual([overlap, distinct], rows[-2:])

    async def test_later_trade_page_failure_propagates_without_later_calls_or_upsert(self):
        now = int(time.time())
        first = [PreviewTrade(timestamp=now, asset=f"token-{i}") for i in range(1000)]
        pm = self.pm()
        pm.get_trade_history.side_effect = [first, RuntimeError("trade page two failed")]

        with patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert:
            with self.assertRaisesRegex(RuntimeError, "trade page two failed"):
                await trader_stats.refresh_trader_analysis(ADDRESS, self.db(), pm)

        self.assertEqual(2, pm.get_trade_history.await_count)
        pm.get_redeems.assert_not_awaited()
        pm.get_positions.assert_not_awaited()
        pm.get_leaderboard_user.assert_not_awaited()
        upsert.assert_not_awaited()

    async def test_redeem_failure_propagates_without_later_calls_or_upsert(self):
        pm = self.pm()
        pm.get_redeems.side_effect = RuntimeError("redeems unavailable")

        with patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert:
            with self.assertRaisesRegex(RuntimeError, "redeems unavailable"):
                await trader_stats.refresh_trader_analysis(ADDRESS, self.db(), pm)

        pm.get_trade_history.assert_awaited_once()
        pm.get_redeems.assert_awaited_once()
        pm.get_positions.assert_not_awaited()
        pm.get_leaderboard_user.assert_not_awaited()
        upsert.assert_not_awaited()

    async def test_positions_failure_propagates_without_later_calls_or_upsert(self):
        pm = self.pm()
        pm.get_positions.side_effect = RuntimeError("positions unavailable")

        with patch.object(trader_stats, "_upsert", new=AsyncMock()) as upsert:
            with self.assertRaisesRegex(RuntimeError, "positions unavailable"):
                await trader_stats.refresh_trader_analysis(ADDRESS, self.db(), pm)

        pm.get_trade_history.assert_awaited_once()
        pm.get_redeems.assert_awaited_once()
        pm.get_positions.assert_awaited_once()
        pm.get_leaderboard_user.assert_not_awaited()
        upsert.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
