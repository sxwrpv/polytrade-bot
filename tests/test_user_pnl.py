from __future__ import annotations

import os
import tempfile
import unittest

from backend.core.pnl import get_pnl_stats
from backend.db.database import Database, now_iso


class UserPnlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(path=self.path, dsn="")
        await self.db.connect()
        await self.db.init()
        await self.db.execute(
            "INSERT INTO users(id, private_key_enc, created_at) VALUES(?,?,?)",
            ("user", "encrypted", now_iso()))
        await self.db.execute(
            "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
            "market_title,outcome,shares,entry_price,notional_usd,status,realized_pnl,"
            "opened_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("position", "user", "leader", "condition", "token", "Market", "YES",
             5.0, .4, 2.0, "closed", 1.0, now_iso(), now_iso()))

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.path)

    async def test_total_realized_includes_partial_and_final_exit_events(self):
        await self.db.execute(
            "INSERT INTO trade_events(id,user_id,position_id,event_type,pnl,ts) "
            "VALUES(?,?,?,?,?,?)", ("partial", "user", "position", "partial", .5, now_iso()))
        await self.db.execute(
            "INSERT INTO trade_events(id,user_id,position_id,event_type,pnl,ts) "
            "VALUES(?,?,?,?,?,?)", ("close", "user", "position", "close", 1.0, now_iso()))

        stats = await get_pnl_stats("user", self.db)

        self.assertEqual(1.5, stats["realized_pnl"])
        self.assertEqual(1.5, stats["total_pnl"])


if __name__ == "__main__":
    unittest.main()
