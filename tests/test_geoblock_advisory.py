"""The frontend geoblock must not veto orders the CLOB API would accept.

polymarket.com/api/geoblock answers for the WEBSITE. The CLOB trading API is a
separate surface with a separate restriction list, so a frontend block is a
signal, not an order-level gate (see execution._check_frontend_geoblock).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.core import execution
from backend.core.execution import OrderResult


class BlockedPM:
    """Frontend says blocked; the book is perfectly tradeable."""
    def __init__(self, blocked=True, raises=False):
        self.blocked, self.raises = blocked, raises

    async def get_geoblock(self):
        if self.raises:
            raise RuntimeError("probe down")
        return {"blocked": self.blocked, "country": "IE", "region": "L"}


class FrontendGeoblockAdvisoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_frontend_block_is_recorded_but_does_not_abort(self):
        res = OrderResult(ok=False, side="BUY", token_id="t")
        with patch.object(execution, "ENFORCE_FRONTEND_GEOBLOCK", False):
            abort = await execution._check_frontend_geoblock(BlockedPM(), res)
        self.assertFalse(abort, "a frontend block must not veto the order")
        self.assertEqual("IE/L", res.raw["frontend_geoblock"])
        self.assertEqual("", res.reason)

    async def test_enforcement_flag_restores_hard_block(self):
        res = OrderResult(ok=False, side="BUY", token_id="t")
        with patch.object(execution, "ENFORCE_FRONTEND_GEOBLOCK", True):
            abort = await execution._check_frontend_geoblock(BlockedPM(), res)
        self.assertTrue(abort)
        self.assertIn("geoblocked (IE/L)", res.reason)

    async def test_unblocked_region_is_a_no_op(self):
        res = OrderResult(ok=False, side="BUY", token_id="t")
        abort = await execution._check_frontend_geoblock(BlockedPM(blocked=False), res)
        self.assertFalse(abort)
        self.assertNotIn("frontend_geoblock", res.raw)

    async def test_probe_failure_still_fails_open(self):
        res = OrderResult(ok=False, side="BUY", token_id="t")
        abort = await execution._check_frontend_geoblock(BlockedPM(raises=True), res)
        self.assertFalse(abort)
        self.assertIn("probe down", res.raw["geoblock_error"])

    async def test_order_reaches_the_book_despite_a_frontend_block(self):
        """End-to-end: the regression that refused 290 orders locally."""
        book = SimpleNamespace(
            asks=(SimpleNamespace(price=0.50, size=1000.0),),
            bids=(SimpleNamespace(price=0.49, size=1000.0),),
            best_ask=SimpleNamespace(price=0.50), best_bid=SimpleNamespace(price=0.49),
            min_order_size=1.0, tick_size=0.01)

        class PM(BlockedPM):
            async def get_orderbook(self, token_id):
                return book

        class Client:
            def __init__(self): self.called = False
            async def place_market_order(self, **kw):
                self.called = True
                return SimpleNamespace(ok=True, order_id="o1", status="matched",
                                       making_amount=10.0, taking_amount=20.0,
                                       trade_ids=["t1"], transactions_hashes=["0x1"],
                                       model_dump=lambda: {})

        client = Client()
        with patch.object(execution, "ENFORCE_FRONTEND_GEOBLOCK", False):
            res = await execution.place_market_order(
                client, PM(), "tok", "BUY", 10.0, reference_price=0.50)
        self.assertTrue(client.called, "order was never submitted to the exchange")
        self.assertTrue(res.ok)
        self.assertEqual("IE/L", res.raw["frontend_geoblock"])


if __name__ == "__main__":
    unittest.main()
