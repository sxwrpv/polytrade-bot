"""Invalid incident rows stay auditable without affecting account metrics."""
import sqlite3
from datetime import datetime, timezone
import pytest
from backend.api.routes_user import activity
from backend.core.pnl import get_pnl_stats, get_pnl_by_wallet

class DB:
    def __init__(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript('''
        CREATE TABLE copy_positions(id,user_id,status,realized_pnl,closed_at,market_title,market_slug,outcome,trader_address,entry_price,exit_price);
        CREATE TABLE trade_events(id,user_id,position_id,event_type,amount_usd,pnl,ts);
        CREATE TABLE trader_cache(address,display_name);
        ''')
        now = datetime.now(timezone.utc).isoformat()
        for ident, status, value in [('legit','resolved',2),('invalid','reconciled_invalid',None)]:
            self.c.execute('INSERT INTO copy_positions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,'user',status,value,now,'title','slug','yes','trader',.5,1))
            self.c.execute('INSERT INTO trade_events VALUES(?,?,?,?,?,?,?)',(ident,'user',ident,'close' if value else 'reconciled_invalid',value,value,now))
    async def fetchall(self, sql, params=()):
        return [dict(r) for r in self.c.execute(sql,params)]
    async def fetchval(self, sql, params=()):
        return self.c.execute(sql,params).fetchone()[0]

@pytest.fixture
def anyio_backend(): return 'asyncio'

@pytest.mark.anyio
async def test_quarantined_rows_excluded_from_activity_and_metrics():
    db=DB()
    feed=await activity(100,{'id':'user'},db)
    assert len(feed)==1 and feed[0]['pnl']==2
    stats=await get_pnl_stats('user',db)
    assert stats['realized_pnl']==2 and stats['total_trades']==1
    breakdown=await get_pnl_by_wallet('user',db)
    assert len(breakdown)==1 and breakdown[0]['realized_pnl']==2
