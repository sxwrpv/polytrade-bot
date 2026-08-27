import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildLiveUniverse, fetchLiveUniverse, periodToLeaderboardWindow,
} from '../lib/liveLeaderboard.mjs';

const wallet = '0x' + 'a'.repeat(40);
const other = '0x' + 'b'.repeat(40);

const snapshot = {
  meta: { generatedAt: '2026-08-26T08:48:30.332Z' },
  traders: [{
    w: wallet, name: 'stale name', pnl: 1, vol: 2, openVal: 900,
    copyClass: 'strong', copyNet: 12, activeDays: 30, winRate: 0.6,
  }],
};

test('periods map to the documented live Polymarket leaderboard windows', () => {
  assert.equal(periodToLeaderboardWindow('d7'), 'WEEK');
  assert.equal(periodToLeaderboardWindow('d30'), 'MONTH');
  assert.equal(periodToLeaderboardWindow('all'), 'ALL');
  assert.throws(() => periodToLeaderboardWindow('d90'), /unsupported period/);
});

test('live leaderboard money and identity replace snapshot values while Copy Score remains an explicit dated overlay', () => {
  const universe = buildLiveUniverse({
    period: 'd7',
    pnlRows: [{ proxyWallet: wallet, userName: 'Live Name', pnl: 500, vol: 700, profileImage: 'live.png', verifiedBadge: true }],
    volumeRows: [{ proxyWallet: wallet, userName: 'Live Name', pnl: 500, vol: 700 }],
    snapshot,
    fetchedAt: '2026-08-26T15:00:00.000Z',
  });
  assert.equal(universe.meta.source, 'live-polymarket-leaderboard');
  assert.equal(universe.meta.generatedAt, '2026-08-26T15:00:00.000Z');
  assert.equal(universe.meta.scoreGeneratedAt, snapshot.meta.generatedAt);
  assert.equal(universe.meta.boardsFrozen, false);
  assert.deepEqual(universe.groups, []);
  assert.deepEqual(universe.angles, []);
  assert.deepEqual(universe.events, []);
  assert.equal(universe.traders[0].name, 'Live Name');
  assert.equal(universe.traders[0].d7, 500);
  assert.equal(universe.traders[0].d7Vol, 700);
  assert.equal(universe.traders[0].copyNet, 12);
  assert.equal(universe.traders[0].openVal, null, 'stale open value must not survive into a live row');
});

test('the live PnL and volume cohorts are unioned and wallets without a score overlay remain unscored', () => {
  const universe = buildLiveUniverse({
    period: 'd30',
    pnlRows: [{ proxyWallet: wallet, pnl: 100, vol: 200 }],
    volumeRows: [{ proxyWallet: other, pnl: 10, vol: 900 }],
    snapshot,
    fetchedAt: '2026-08-26T15:00:00.000Z',
  });
  assert.equal(universe.traders.length, 2);
  const row = universe.traders.find((x) => x.w === other);
  assert.equal(row.d30, 10);
  assert.equal(row.d30Vol, 900);
  assert.equal(row.copyClass, null);
  assert.equal(row.copyNet, null);
  assert.equal(row.activeDays, null, 'stale snapshot metrics must not leak into live rows');
  assert.equal(row.winRate, null, 'stale snapshot filters must not leak into live rows');
});

test('missing leaderboard numerics remain unavailable rather than becoming zero', () => {
  const universe = buildLiveUniverse({
    period: 'd7',
    pnlRows: [{ proxyWallet: wallet, pnl: null, vol: '' }],
    snapshot,
    fetchedAt: '2026-08-26T15:00:00.000Z',
  });
  assert.equal(universe.traders[0].d7, null);
  assert.equal(universe.traders[0].d7Vol, null);
});

test('live universe fetches both Polymarket leaderboard cohorts for the selected window', async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    return {
      ok: true,
      json: async () => [{ proxyWallet: wallet, pnl: 100, vol: 200 }],
    };
  };
  const universe = await fetchLiveUniverse({
    period: 'd30', snapshot, fetchImpl, fetchedAt: '2026-08-26T15:00:00.000Z',
  });
  assert.equal(calls.length, 2);
  assert.ok(calls.some((url) => url.includes('timePeriod=MONTH') && url.includes('orderBy=PNL')));
  assert.ok(calls.some((url) => url.includes('timePeriod=MONTH') && url.includes('orderBy=VOL')));
  assert.equal(universe.traders[0].d30, 100);
  assert.equal(universe.meta.generatedAt, '2026-08-26T15:00:00.000Z');
});

test('live universe fails closed when either leaderboard request fails', async () => {
  const fetchImpl = async (url) => ({
    ok: !String(url).includes('orderBy=VOL'),
    status: 503,
    json: async () => [],
  });
  await assert.rejects(
    fetchLiveUniverse({ period: 'd7', snapshot, fetchImpl }),
    /Polymarket leaderboard 503/,
  );
});
