import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildLiveUniverse, createLastTradeLookup, fetchLiveUniverse, periodToLeaderboardWindow,
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

test('live universe pages both cohorts to the top 100 and asks for their last trades', async () => {
  // Polymarket cuts any larger limit to 50 rows, so the top 100 is two pages.
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(new URL(url));
    return {
      ok: true,
      json: async () => [{ proxyWallet: wallet, pnl: 100, vol: 200 }],
    };
  };
  let asked;
  const lastTradeDays = async (wallets) => {
    asked = wallets;
    return new Map([[wallet, '2026-08-20']]);
  };
  const universe = await fetchLiveUniverse({
    period: 'd30', snapshot, fetchImpl, fetchedAt: '2026-08-26T15:00:00.000Z', lastTradeDays,
  });
  assert.equal(calls.length, 4);
  for (const orderBy of ['PNL', 'VOL']) {
    const pages = calls.filter((u) => u.searchParams.get('orderBy') === orderBy);
    assert.deepEqual(pages.map((u) => u.searchParams.get('offset')).sort(), ['0', '50']);
    assert.ok(pages.every((u) => u.searchParams.get('timePeriod') === 'MONTH' && u.searchParams.get('limit') === '50'));
  }
  assert.deepEqual(asked, [wallet], 'each wallet is looked up once');
  assert.equal(universe.traders[0].d30, 100);
  assert.equal(universe.traders[0].lastTradeDay, '2026-08-20');
  assert.equal(universe.meta.generatedAt, '2026-08-26T15:00:00.000Z');
});

test('live universe fails closed when any leaderboard page fails', async () => {
  for (const broken of ['orderBy=VOL', 'offset=50']) {
    const fetchImpl = async (url) => ({
      ok: !String(url).includes(broken),
      status: 503,
      json: async () => [],
    });
    await assert.rejects(
      fetchLiveUniverse({ period: 'd7', snapshot, fetchImpl }),
      /Polymarket leaderboard 503/,
    );
  }
});

test('an unknown last trade stays null instead of becoming the fetch date', () => {
  const universe = buildLiveUniverse({
    period: 'all',
    pnlRows: [{ proxyWallet: wallet, pnl: 1, vol: 2 }, { proxyWallet: other, pnl: 3, vol: 4 }],
    snapshot,
    fetchedAt: '2026-10-21T09:00:00.000Z',
    lastTrades: new Map([[wallet, '2024-11-13']]),
  });
  const day = Object.fromEntries(universe.traders.map((t) => [t.w, t.lastTradeDay]));
  assert.equal(day[wallet], '2024-11-13');
  assert.equal(day[other], null);
  assert.equal(universe.meta.lastTradeUnknown, 1);
});

test('last trades come from TRADE rows only, once per wallet, and a failed lookup stays unknown', async () => {
  const broken = '0x' + 'c'.repeat(40);
  const rows = {
    [wallet]: [{ type: 'TRADE', timestamp: Date.UTC(2026, 7, 22, 15, 30) / 1000 }],
    [other]: [{ type: 'REDEEM', timestamp: Date.UTC(2026, 9, 20) / 1000 }],
  };
  const calls = [];
  const fetchImpl = async (url) => {
    const u = new URL(url);
    calls.push(u);
    const user = u.searchParams.get('user');
    if (user === broken) return { ok: false, status: 429, json: async () => ({}) };
    return { ok: true, json: async () => rows[user] || [] };
  };
  const lookup = createLastTradeLookup({ fetchImpl, perSecond: 1000 });
  const days = await lookup([wallet, other, broken, wallet]);
  assert.equal(days.get(wallet), '2026-08-22');
  assert.equal(days.get(other), null, 'a redemption is not a trade');
  assert.equal(days.has(broken), false, 'a failed lookup is left out, not guessed');
  assert.equal(calls.length, 3);
  assert.ok(calls.every((u) => u.searchParams.get('type') === 'TRADE' && u.searchParams.get('limit') === '1'));
  await lookup([wallet, other, broken]);
  assert.equal(calls.length, 3, 'known days and recent failures are not asked again');
});

test('the board waits for unknown wallets only up to its budget; late answers land in the cache', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const fetchImpl = async () => {
    await gate;
    return { ok: true, json: async () => [{ type: 'TRADE', timestamp: Date.UTC(2026, 9, 20) / 1000 }] };
  };
  const lookup = createLastTradeLookup({ fetchImpl, budgetMs: 20 });
  assert.equal((await lookup([wallet])).has(wallet), false);
  release();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal((await lookup([wallet])).get(wallet), '2026-10-20');
});

test('lookups go out one at a time and at most perSecond a second, however many boards ask', async () => {
  // The copy engine shares this server's IP and Polymarket's per-IP limits,
  // so a burst from the screener could delay the trades it copies.
  let clock = 0;
  const started = [];
  let inFlight = 0;
  let mostInFlight = 0;
  const fetchImpl = async () => {
    started.push(clock);
    inFlight += 1;
    mostInFlight = Math.max(mostInFlight, inFlight);
    await Promise.resolve();
    clock += 100;
    inFlight -= 1;
    return { ok: true, json: async () => [{ type: 'TRADE', timestamp: Date.UTC(2026, 9, 1) / 1000 }] };
  };
  const lookup = createLastTradeLookup({
    fetchImpl, perSecond: 2, now: () => clock, sleep: async (ms) => { clock += ms; },
  });
  const wallets = Array.from({ length: 6 }, (_, i) => '0x' + String(i).repeat(40));
  const [first, second] = await Promise.all([lookup(wallets.slice(0, 4)), lookup(wallets.slice(2))]);
  assert.equal(started.length, 6, 'a wallet two boards share is asked once');
  assert.equal(mostInFlight, 1);
  for (let i = 1; i < started.length; i += 1) {
    assert.ok(started[i] - started[i - 1] >= 500, `request ${i} went out ${started[i] - started[i - 1]} ms after the last`);
  }
  assert.equal(first.get(wallets[2]), '2026-10-01');
  assert.equal(second.get(wallets[5]), '2026-10-01');
});

test('a known day is served at once and renewed behind the board once it is old', async () => {
  let clock = 0;
  let traded = Date.UTC(2026, 8, 1) / 1000;
  let calls = 0;
  const fetchImpl = async () => {
    calls += 1;
    return { ok: true, json: async () => [{ type: 'TRADE', timestamp: traded }] };
  };
  const lookup = createLastTradeLookup({ fetchImpl, perSecond: 1000, refreshAfterMs: 60_000, now: () => clock });
  assert.equal((await lookup([wallet])).get(wallet), '2026-09-01');
  traded = Date.UTC(2026, 9, 21) / 1000;
  clock += 30_000;
  assert.equal((await lookup([wallet])).get(wallet), '2026-09-01');
  assert.equal(calls, 1, 'a fresh day is not asked again');
  clock += 40_000;
  assert.equal((await lookup([wallet])).get(wallet), '2026-09-01', 'an old day is still answered at once');
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(calls, 2, 'and renewed behind the board');
  assert.equal((await lookup([wallet])).get(wallet), '2026-10-21');
});

test('wallets nobody knows yet are looked up before old days are renewed', async () => {
  const [a, b, c] = ['a', 'b', 'c'].map((ch) => '0x' + ch.repeat(40));
  const order = [];
  const fetchImpl = async (url) => {
    order.push(new URL(url).searchParams.get('user'));
    return { ok: true, json: async () => [{ type: 'TRADE', timestamp: Date.UTC(2026, 9, 1) / 1000 }] };
  };
  const lookup = createLastTradeLookup({ fetchImpl, perSecond: 1000, refreshAfterMs: 0 });
  await lookup([a, b]);
  await lookup([a, b, c]);
  await new Promise((resolve) => setTimeout(resolve, 20));
  // a's renewal was already under way when c arrived; c then goes before b.
  assert.deepEqual(order, [a, b, a, c, b]);
});
