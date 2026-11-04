import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildLiveUniverse, createLastTradeLookup, fetchLiveUniverse, periodToLeaderboardWindow,
} from '../lib/liveLeaderboard.mjs';
import { polymarketFetch } from '../lib/polymarket.mjs';

const wallet = '0x' + 'a'.repeat(40);
const other = '0x' + 'b'.repeat(40);

const snapshot = {
  meta: { generatedAt: '2026-08-26T08:48:30.332Z' },
  traders: [{
    w: wallet, name: 'stale name', pnl: 1, vol: 2, openVal: 900,
    copyClass: 'strong', copyNet: 12, activeDays: 30, winRate: 0.6,
  }],
};

/** A Data API v2 page: rows in `data`, the cursor in `pagination`. */
const v2Page = (data, nextCursor = null) => ({
  data, pagination: { limit: data.length, offset: 0, has_more: nextCursor != null, next_cursor: nextCursor },
});
/** `count` distinct leaderboard rows, the first at address number `from`. */
const boardRows = (from, count) => Array.from({ length: count }, (_, i) => ({
  rank: from + i + 1, user_id: '0x' + (from + i + 1).toString(16).padStart(40, '0'), pnl: 100, volume: 200,
}));

test('periods map to the documented live Polymarket leaderboard windows', () => {
  assert.equal(periodToLeaderboardWindow('d7'), 'week');
  assert.equal(periodToLeaderboardWindow('d30'), 'month');
  assert.equal(periodToLeaderboardWindow('all'), 'all');
  assert.throws(() => periodToLeaderboardWindow('d90'), /unsupported period/);
});

test('live leaderboard money and identity replace snapshot values while Copy Score remains an explicit dated overlay', () => {
  const universe = buildLiveUniverse({
    period: 'd7',
    pnlRows: [{
      rank: 3, user_id: wallet, user_name: 'Live Name', pnl: 500, volume: 700,
      profile_image: 'live.png', x_username: 'livex', verified: true,
    }],
    volumeRows: [{ rank: 3, user_id: wallet, user_name: 'Live Name', pnl: 500, volume: 700 }],
    snapshot,
    fetchedAt: '2026-08-26T15:00:00.000Z',
  });
  assert.equal(universe.meta.source, 'live-polymarket-leaderboard');
  assert.equal(universe.meta.generatedAt, '2026-08-26T15:00:00.000Z');
  assert.equal(universe.meta.scoreGeneratedAt, snapshot.meta.generatedAt);
  assert.equal(universe.meta.leaderboardWindow, 'week');
  assert.equal(universe.meta.boardsFrozen, false);
  assert.deepEqual(universe.groups, []);
  assert.deepEqual(universe.angles, []);
  assert.deepEqual(universe.events, []);
  const [row] = universe.traders;
  assert.equal(row.name, 'Live Name');
  assert.equal(row.img, 'live.png');
  assert.equal(row.xUsername, 'livex');
  assert.equal(row.verified, true);
  assert.equal(row.liveRank, 3);
  assert.equal(row.d7, 500);
  assert.equal(row.d7Vol, 700);
  assert.equal(row.copyNet, 12);
  assert.equal(row.openVal, null, 'stale open value must not survive into a live row');
});

test('the live PnL and volume cohorts are unioned and wallets without a score overlay remain unscored', () => {
  const universe = buildLiveUniverse({
    period: 'd30',
    // v2 can name a wallet in checksum case; the board keys every wallet in lower case.
    pnlRows: [{ user_id: '0x' + 'A'.repeat(40), pnl: 100, volume: 200 }],
    volumeRows: [{ user_id: other, pnl: 10, volume: 900 }],
    snapshot,
    fetchedAt: '2026-08-26T15:00:00.000Z',
  });
  assert.equal(universe.traders.length, 2);
  assert.equal(universe.traders.find((x) => x.w === wallet).copyNet, 12, 'the overlay joins on the lower-cased wallet');
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
    pnlRows: [{ user_id: wallet, pnl: null, volume: '' }],
    snapshot,
    fetchedAt: '2026-08-26T15:00:00.000Z',
  });
  assert.equal(universe.traders[0].d7, null);
  assert.equal(universe.traders[0].d7Vol, null);
});

test('live universe reads each top 100 in one v2 request and asks for their last trades', async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(new URL(url));
    return { ok: true, json: async () => v2Page(boardRows(0, 100), 'more-rows') };
  };
  let asked;
  const lastTradeDays = async (wallets) => {
    asked = wallets;
    return new Map([[wallets[0], '2026-08-20']]);
  };
  const universe = await fetchLiveUniverse({
    period: 'd30', snapshot, fetchImpl, fetchedAt: '2026-08-26T15:00:00.000Z', lastTradeDays,
  });
  assert.equal(calls.length, 2, 'a full first page is the whole list: its cursor is not followed');
  for (const sortBy of ['PNL', 'VOLUME']) {
    const [u] = calls.filter((x) => x.searchParams.get('sort_by') === sortBy);
    assert.equal(`${u.origin}${u.pathname}`, 'https://data-api.polymarket.com/v2/leaderboard');
    assert.equal(u.searchParams.get('time_period'), 'month');
    assert.equal(u.searchParams.get('category'), 'overall');
    assert.equal(u.searchParams.get('limit'), '100');
    assert.equal(u.searchParams.has('offset'), false, 'v2 answers any offset with a 400');
    assert.equal(u.searchParams.has('cursor'), false);
  }
  assert.equal(asked.length, 100);
  assert.equal(new Set(asked).size, 100, 'each wallet is looked up once');
  assert.equal(universe.traders.length, 100);
  const first = universe.traders.find((t) => t.w === asked[0]);
  assert.equal(first.d30, 100);
  assert.equal(first.d30Vol, 200);
  assert.equal(first.lastTradeDay, '2026-08-20');
  assert.equal(universe.meta.generatedAt, '2026-08-26T15:00:00.000Z');
});

test('a list served in shorter pages is followed by its cursor to the top 100', async () => {
  // A server that cuts every page to 50 rows, the way v1 did, takes two requests a list.
  const calls = [];
  const fetchImpl = async (url) => {
    const u = new URL(url);
    calls.push(u);
    const from = Number(u.searchParams.get('cursor') || 0);
    return { ok: true, json: async () => v2Page(boardRows(from, 50), String(from + 50)) };
  };
  const universe = await fetchLiveUniverse({ period: 'd7', snapshot, fetchImpl });
  assert.equal(calls.length, 4);
  for (const sortBy of ['PNL', 'VOLUME']) {
    const [first, second] = calls.filter((x) => x.searchParams.get('sort_by') === sortBy);
    assert.equal(first.searchParams.get('limit'), '100');
    assert.equal(second.searchParams.get('cursor'), '50');
    assert.equal(second.searchParams.has('limit'), false, 'the cursor carries the page size');
    assert.equal(second.searchParams.get('time_period'), 'week');
    assert.equal(second.searchParams.get('category'), 'overall');
  }
  assert.equal(universe.traders.length, 100);
});

test('live universe fails closed when any leaderboard page fails', async () => {
  for (const broken of ['sort_by=VOLUME', 'cursor=']) {
    const fetchImpl = async (url) => {
      const from = Number(new URL(url).searchParams.get('cursor') || 0);
      return {
        ok: !String(url).includes(broken),
        status: 503,
        json: async () => v2Page(boardRows(from, 50), from ? null : '50'),
      };
    };
    await assert.rejects(
      fetchLiveUniverse({ period: 'd7', snapshot, fetchImpl }),
      /Polymarket leaderboard 503/,
    );
  }
  // v1's bare list, or any page without `data`, is refused.
  const v1Shaped = async () => ({ ok: true, json: async () => boardRows(0, 100) });
  await assert.rejects(fetchLiveUniverse({ period: 'd7', snapshot, fetchImpl: v1Shaped }), /invalid payload/);
  // So is an empty page that still names a cursor: the list would end short.
  const stalled = async (url) => ({
    ok: true,
    json: async () => (new URL(url).searchParams.has('cursor') ? v2Page([], '100') : v2Page(boardRows(0, 50), '50')),
  });
  await assert.rejects(fetchLiveUniverse({ period: 'd7', snapshot, fetchImpl: stalled }), /empty page mid-list/);
});

test('an unknown last trade stays null instead of becoming the fetch date', () => {
  const universe = buildLiveUniverse({
    period: 'all',
    pnlRows: [{ user_id: wallet, pnl: 1, volume: 2 }, { user_id: other, pnl: 3, volume: 4 }],
    snapshot,
    fetchedAt: '2026-10-21T09:00:00.000Z',
    lastTrades: new Map([[wallet, '2024-11-13']]),
  });
  const day = Object.fromEntries(universe.traders.map((t) => [t.w, t.lastTradeDay]));
  assert.equal(day[wallet], '2024-11-13');
  assert.equal(day[other], null);
  assert.equal(universe.meta.lastTradeUnknown, 1);
});

test('last trades come from single-market TRADE rows only, once per wallet, and a failed lookup stays unknown', async () => {
  const broken = '0x' + 'c'.repeat(40);
  const comboFirst = '0x' + 'd'.repeat(40);
  const combosOnly = '0x' + 'e'.repeat(40);
  const rows = {
    [wallet]: [{ type: 'TRADE', timestamp: Date.UTC(2026, 7, 22, 15, 30) / 1000 }],
    [other]: [{ type: 'REDEEM', timestamp: Date.UTC(2026, 9, 20) / 1000 }],
    [comboFirst]: [
      { type: 'TRADE', is_combo: true, timestamp: Date.UTC(2026, 9, 30) / 1000 },
      { type: 'TRADE', timestamp: Date.UTC(2026, 9, 12) / 1000 },
    ],
    [combosOnly]: [{ type: 'TRADE', is_combo: true, timestamp: Date.UTC(2026, 9, 30) / 1000 }],
  };
  const calls = [];
  const fetchImpl = async (url) => {
    const u = new URL(url);
    calls.push(u);
    const user = u.searchParams.get('user');
    if (user === broken) return { ok: false, status: 429, json: async () => ({}) };
    return { ok: true, json: async () => v2Page(rows[user] || []) };
  };
  const lookup = createLastTradeLookup({ fetchImpl, perSecond: 1000 });
  const days = await lookup([wallet, other, broken, comboFirst, combosOnly, wallet]);
  assert.equal(days.get(wallet), '2026-08-22');
  assert.equal(days.get(other), null, 'a redemption is not a trade');
  assert.equal(days.has(broken), false, 'a failed lookup is left out, not guessed');
  assert.equal(days.get(comboFirst), '2026-10-12', 'a combo trade is skipped for the single-market trade before it');
  assert.equal(days.get(combosOnly), null);
  assert.equal(calls.length, 5);
  for (const u of calls) {
    assert.equal(`${u.origin}${u.pathname}`, 'https://data-api.polymarket.com/v2/activity');
    assert.equal(u.searchParams.get('type'), 'TRADE');
    assert.equal(u.searchParams.get('limit'), '20');
    assert.equal(u.searchParams.get('start'), '1', 'the whole history, past v2\'s three-year default');
    assert.equal(u.searchParams.has('offset'), false);
  }
  await lookup([wallet, other, broken]);
  assert.equal(calls.length, 5, 'known days and recent failures are not asked again');
});

test('the board waits for unknown wallets only up to its budget; late answers land in the cache', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const fetchImpl = async () => {
    await gate;
    return { ok: true, json: async () => v2Page([{ type: 'TRADE', timestamp: Date.UTC(2026, 9, 20) / 1000 }]) };
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
    return { ok: true, json: async () => v2Page([{ type: 'TRADE', timestamp: Date.UTC(2026, 9, 1) / 1000 }]) };
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
    return { ok: true, json: async () => v2Page([{ type: 'TRADE', timestamp: traded }]) };
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
    return { ok: true, json: async () => v2Page([{ type: 'TRADE', timestamp: Date.UTC(2026, 9, 1) / 1000 }]) };
  };
  const lookup = createLastTradeLookup({ fetchImpl, perSecond: 1000, refreshAfterMs: 0 });
  await lookup([a, b]);
  await lookup([a, b, c]);
  await new Promise((resolve) => setTimeout(resolve, 20));
  // a's renewal was already under way when c arrived; c then goes before b.
  assert.deepEqual(order, [a, b, a, c, b]);
});

test('boards and every other Polymarket read share one budget: ten at once, then five a second', async () => {
  const real = globalThis.fetch;
  const sent = [];
  const t0 = Date.now();
  globalThis.fetch = async () => {
    sent.push(Date.now() - t0);
    return { ok: true, status: 200, json: async () => v2Page([]) };
  };
  try {
    await Promise.all([
      fetchLiveUniverse({ period: 'd7', snapshot }),
      ...Array.from({ length: 10 }, () => polymarketFetch('https://data-api.polymarket.com/v2/value?user=x')),
    ]);
  } finally {
    globalThis.fetch = real;
  }
  sent.sort((a, b) => a - b);
  assert.equal(sent.length, 12, 'two leaderboard pages and ten other reads');
  assert.ok(sent[9] < 50, 'a burst of ten goes at once');
  assert.ok(sent[10] >= 150 && sent[11] >= 350, `then one every 200 ms (${sent.slice(10).join(', ')} ms)`);
});
