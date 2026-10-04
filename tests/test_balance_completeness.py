"""Synthetic fixtures only; no credentials, network, or live database."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from backend.api import routes_user
from backend.core import equity
from backend.core.polymarket import PolymarketClient


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def pm_fixture(*, complete=True):
    p = SimpleNamespace(size=1, current_value=1, cash_pnl=0, redeemable=False)
    pm = PolymarketClient.__new__(PolymarketClient)
    pm.get_positions = AsyncMock(side_effect=([p] * 500, [p]) if complete else None,
                                 return_value=[p] * 500)
    return pm


@pytest.mark.anyio
@pytest.mark.parametrize('complete', [True, False])
async def test_profile_equity_requires_complete_position_read(complete):
    pm = pm_fixture(complete=complete)
    client = SimpleNamespace(get_balance_allowance=AsyncMock(
        return_value=SimpleNamespace(balance=10_000_000)))
    user = dict(id='fixture-user', signer_address='fixture-signer', display_name='Fixture')
    with patch.object(routes_user, 'get_user_client', AsyncMock(return_value=client)):
        result = await routes_user.me(None, balance=True, user=user, pmc=pm)
    assert result['balance'] == 10
    assert result['equity'] == (511 if complete else None)
    assert result['positions_value'] == (501 if complete else None)


@pytest.mark.anyio
@pytest.mark.parametrize('complete', [True, False])
async def test_snapshot_requires_complete_position_read(complete):
    pm = pm_fixture(complete=complete)
    client = SimpleNamespace(get_balance_allowance=AsyncMock(
        return_value=SimpleNamespace(balance=10_000_000)))
    db = SimpleNamespace(fetchval=AsyncMock(return_value=0), execute=AsyncMock())
    result = await equity.take_snapshot(db, 'fixture-user', client, pm)
    if complete:
        assert result['equity'] == 511
        db.execute.assert_awaited_once()
    else:
        assert result is None
        db.execute.assert_not_awaited()
