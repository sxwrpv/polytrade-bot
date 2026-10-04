"""Offline incident regressions: no exchange, production DB or worker startup."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from backend.core.copy_engine import CopyEngine, Action


def engine():
    e = CopyEngine.__new__(CopyEngine)
    tx = SimpleNamespace(fetchone=AsyncMock(return_value=None), execute=AsyncMock(return_value=1))
    context = AsyncMock()
    context.__aenter__.return_value = tx
    e.db = SimpleNamespace(fetchone=AsyncMock(return_value=None), transaction=Mock(return_value=context), is_pg=False)
    e._event = AsyncMock()
    e._submitted = {}
    e._collateral_cache = {}
    e._risk_lock = asyncio.Lock()
    e.pm = None
    e._notify_position = AsyncMock()
    return e


@pytest.mark.parametrize('shares', [0, 16, 1687.99, 2521.45])
def test_legacy_intent_alone_never_authorizes_adoption(shares):
    async def run():
        e = engine()
        e._note_submitted('user', 'token', 15)
        p = SimpleNamespace(asset='token', size=shares, avg_price=.45,
                            condition_id='condition', event_slug='', slug='', title='', outcome='YES')
        assert await e._adopt_untracked_submissions('user', 'leader', [p], []) == 0
        e.db.transaction.assert_not_called()
        e._notify_position.assert_not_called()
        assert e._submitted_basis('user', 'token') == 15
    asyncio.run(run())


@pytest.mark.parametrize('uncertain', [False, True])
@pytest.mark.parametrize('prior', [0, 6])
def test_failed_buy_only_rolls_back_definitively_unsubmitted_intent(uncertain, prior):
    async def run():
        e = engine()
        if prior:
            e._note_submitted('user', 'token', prior)
        a = Action(kind='open', token_id='token', side='BUY', amount=15, claim_id='claim')
        e._prepare_buy = AsyncMock(return_value=(a, dict(slippage=1, min_price=0, max_price=1)))
        e._clamp_to_verified_position = AsyncMock(return_value=a)
        e._mark_claim_submitting = AsyncMock(return_value=True)
        e._place_order = AsyncMock(return_value=SimpleNamespace(ok=False, submission_uncertain=uncertain, reason='test'))
        e._release_buy_claim = AsyncMock()
        e._mark_claim_uncertain = AsyncMock()
        e._record_fill_outcome = Mock()
        assert await e._execute_buy('user', None, a) == 0
        assert e._submitted_basis('user', 'token') == (prior + 15 if uncertain else prior)
        assert e._release_buy_claim.await_count == (0 if uncertain else 1)
        assert e._mark_claim_uncertain.await_count == (1 if uncertain else 0)
    asyncio.run(run())


def test_unclassified_exception_retains_claim():
    async def run():
        e = engine()
        a = Action(kind='open', token_id='token', side='BUY', amount=15, claim_id='claim')
        e._prepare_buy = AsyncMock(return_value=(a, dict(slippage=1, min_price=0, max_price=1)))
        e._clamp_to_verified_position = AsyncMock(return_value=a)
        e._mark_claim_submitting = AsyncMock(return_value=True)
        e._place_order = AsyncMock(side_effect=ValueError('malformed post-submit response'))
        e._release_buy_claim = AsyncMock()
        e._mark_claim_uncertain = AsyncMock()
        with pytest.raises(ValueError):
            await e._execute_buy('user', None, a)
        e._release_buy_claim.assert_not_called()
        e._mark_claim_uncertain.assert_awaited_once()
        assert e._submitted_basis('user', 'token') == 15
    asyncio.run(run())


def test_old_absent_claim_is_not_proof_of_no_fill():
    async def run():
        e = engine()
        e._release_buy_claim = AsyncMock()
        await e._settle_uncertain_claim('user', dict(token_id='token', action='open',
            claim_id='claim', claimed_at='2000-01-01T00:00:00+00:00'), None)
        e._release_buy_claim.assert_not_called()
    asyncio.run(run())
