/* Data API v2: the wallet page's reads.
 *
 * Polymarket retired Data API v1 on 2026-11-02; every v1 route, and
 * lb-api.polymarket.com, answers 410 since. These tests pin how the screener
 * reads v2: cursor pages with the anchor and filters restated, the activity
 * types the metrics read, Combo rows left out of the tape, the 5,500-row
 * budget and what `complete` says about it, and the v2 field names mapped onto
 * the names lib/metrics.mjs and the wallet page read. Positions paging is
 * pinned in screenerModel.test.mjs, next to the rules that decide where it stops.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildProfile } from '../lib/metrics.mjs';

// The rate gate reads its rate when the module loads: these tests are about
// paging, so the gate is set wide open before the import.
process.env.SCREENER_POLYMARKET_RPS = '10000';
const pm = await import('../lib/polymarket.mjs');

const wallet = '0x' + '3'.repeat(40);

async function withFetch(fetchImpl, work) {
  const real = globalThis.fetch;
  globalThis.fetch = fetchImpl;
  try { return await work(); } finally { globalThis.fetch = real; }
}

const page = (data, nextCursor = null) => ({
  ok: true,
  status: 200,
  json: async () => ({
    data, pagination: { limit: data.length, offset: 0, has_more: nextCursor != null, next_cursor: nextCursor },
  }),
});

/** A fake /v2/activity feed of `rows`, newest first. The first request names
 *  the page size; each cursor carries the next row and that size. `status`
 *  answers the page that starts at a given row with an error instead. */
function activityFeed(rows, { status = {} } = {}) {
  const calls = [];
  const fetchImpl = async (url) => {
    const u = new URL(url);
    calls.push(u);
    const cursor = u.searchParams.get('cursor');
    const [from, size] = cursor ? cursor.split(':').map(Number) : [0, Number(u.searchParams.get('limit'))];
    if (status[from]) return { ok: false, status: status[from], json: async () => ({ error: 'refused' }) };
    const data = rows.slice(from, from + size);
    return page(data, from + data.length < rows.length ? `${from + data.length}:${size}` : null);
  };
  return { calls, fetchImpl };
}

const trade = (i, extra = {}) => ({
  proxy_wallet: wallet, timestamp: 1_790_000_000 - i, condition_id: `0xc${i}`, type: 'TRADE',
  size: 10, usdc_size: 5, transaction_hash: `0xt${i}`, price: 0.5, token_id: `${i}`, side: 'BUY',
  outcome_index: 0, title: `Market ${i}`, slug: `m-${i}`, icon: '', event_slug: `e-${i}`, outcome: 'Yes',
  name: 'n', pseudonym: 'p', bio: '', profile_image: '', profile_image_optimized: '', ...extra,
});

// --- the activity tape ----------------------------------------------------

test('the tape is read by cursor from /v2/activity, whole history, metric types only', async () => {
  const feed = activityFeed(Array.from({ length: 2500 }, (_, i) => trade(i)));
  const tape = await withFetch(feed.fetchImpl, () => pm.activity(wallet));
  assert.equal(tape.length, 2500);
  assert.equal(tape.complete, true);
  assert.equal(feed.calls.length, 3);
  const [first, ...rest] = feed.calls;
  assert.equal(`${first.origin}${first.pathname}`, 'https://data-api.polymarket.com/v2/activity');
  assert.equal(first.searchParams.get('limit'), '1000');
  assert.equal(first.searchParams.has('cursor'), false);
  for (const u of rest) {
    assert.ok(u.searchParams.get('cursor'));
    assert.equal(u.searchParams.has('limit'), false, 'the cursor carries the page size');
  }
  for (const u of feed.calls) {
    assert.equal(u.searchParams.get('user'), wallet);
    assert.equal(u.searchParams.get('start'), '1', 'left out, v2 starts three years back');
    assert.equal(u.searchParams.has('offset'), false, 'v2 answers any offset with a 400');
    assert.deepEqual(u.searchParams.get('type').split(',').sort(), [
      'CONVERSION', 'MAKER_REBATE', 'MERGE', 'REDEEM', 'REWARD', 'SPLIT', 'TAKER_REBATE', 'TRADE', 'YIELD',
    ], 'the types lib/metrics.mjs reads, restated on every page');
  }
});

test('the tape stops at 5,500 rows read and says whether more remain', async () => {
  const over = activityFeed(Array.from({ length: 7000 }, (_, i) => trade(i)));
  const cut = await withFetch(over.fetchImpl, () => pm.activity(wallet));
  assert.equal(cut.length, pm.ACTIVITY_CEILING);
  assert.equal(cut.complete, false);
  assert.equal(over.calls.length, 6, 'no page past the budget is asked for');

  const exact = activityFeed(Array.from({ length: pm.ACTIVITY_CEILING }, (_, i) => trade(i)));
  const whole = await withFetch(exact.fetchImpl, () => pm.activity(wallet));
  assert.equal(whole.length, pm.ACTIVITY_CEILING);
  assert.equal(whole.complete, true, 'a feed that ends at the budget is the whole tape');

  const overshoot = activityFeed(Array.from({ length: 6000 }, (_, i) => trade(i)));
  const trimmed = await withFetch(overshoot.fetchImpl, () => pm.activity(wallet));
  assert.equal(trimmed.length, pm.ACTIVITY_CEILING);
  assert.equal(trimmed.complete, false, 'rows cut from the last page leave the tape partial');
});

test('a failed page leaves the tape partial and says so', async () => {
  // 400 is not retried; 410 is what every v1 route answers since the retirement.
  for (const status of [400, 410]) {
    const feed = activityFeed(Array.from({ length: 2500 }, (_, i) => trade(i)), { status: { 1000: status } });
    const tape = await withFetch(feed.fetchImpl, () => pm.activity(wallet));
    assert.equal(tape.length, 1000);
    assert.equal(tape.complete, false);
  }
  const empty = async () => page([], 'again');
  const stalled = await withFetch(empty, () => pm.activity(wallet));
  assert.equal(stalled.complete, false, 'an empty page that still names a cursor is no end of feed');
  const v1Shaped = async () => ({ ok: true, status: 200, json: async () => [trade(0)] });
  const refused = await withFetch(v1Shaped, () => pm.activity(wallet));
  assert.equal(refused.length, 0);
  assert.equal(refused.complete, false, 'a bare list is no v2 page');
});

test('Combo rows leave the tape and still count toward the rows read', async () => {
  // Every third row is a Combo trade, keyed by a combo condition id.
  const withCombos = (count) => Array.from({ length: count }, (_, i) => trade(i, i % 3 ? {} : {
    is_combo: true, condition_id: '0x03be2770b6f068622f76349f73504ae74c0000000000000000000000000000', slug: '', event_slug: '',
  }));
  const small = await withFetch(activityFeed(withCombos(10)).fetchImpl, () => pm.activity(wallet));
  assert.equal(small.length, 6);
  assert.equal(small.complete, true);
  assert.ok(small.every((row) => row.conditionId.startsWith('0xc')));

  const busy = activityFeed(withCombos(9000));
  const tape = await withFetch(busy.fetchImpl, () => pm.activity(wallet));
  assert.equal(busy.calls.length, 6, 'the budget counts rows read, combos included');
  assert.equal(tape.length, pm.ACTIVITY_CEILING - Math.ceil(pm.ACTIVITY_CEILING / 3));
  assert.equal(tape.complete, false);
});

test('v2 activity fields arrive under the names the metrics and the wallet page read', async () => {
  const row = trade(7, {
    side: 'SELL', usdc_size: 12.5, outcome_index: 1, icon: 'icon.png', bio: 'bio', profile_image: 'me.png',
    profile_image_optimized: 'me-small.png',
  });
  const [mapped] = await withFetch(activityFeed([row]).fetchImpl, () => pm.activity(wallet));
  assert.deepEqual(mapped, {
    proxyWallet: wallet,
    timestamp: row.timestamp,
    conditionId: '0xc7',
    type: 'TRADE',
    size: 10,
    usdcSize: 12.5,
    transactionHash: '0xt7',
    price: 0.5,
    asset: '7',
    side: 'SELL',
    outcomeIndex: 1,
    title: 'Market 7',
    slug: 'm-7',
    icon: 'icon.png',
    eventSlug: 'e-7',
    outcome: 'Yes',
    name: 'n',
    pseudonym: 'p',
    bio: 'bio',
    profileImage: 'me.png',
    profileImageOptimized: 'me-small.png',
  });
});

// --- from v2 rows to a profile ----------------------------------------------

test('a profile built from v2 rows keeps its v1 meaning', async () => {
  const day = 86400;
  const now = Math.floor(Date.now() / 1000);
  const activityRows = [
    trade(1, { condition_id: '0xheld', side: 'BUY', usdc_size: 100, size: 200, timestamp: now - day }),
    trade(2, { condition_id: '0xwon', side: 'BUY', usdc_size: 50, size: 100, timestamp: now - 3 * day }),
    trade(3, { condition_id: '0xwon', type: 'REDEEM', side: '', usdc_size: 120, size: 100, timestamp: now - 2 * day }),
    trade(4, { condition_id: '0x03be2770b6f068622f76349f73504ae74c0000000000000000000000000000',
      is_combo: true, side: 'BUY', usdc_size: 1000, size: 16000, timestamp: now - day }),
    trade(5, { condition_id: '', type: 'MAKER_REBATE', side: '', usdc_size: 4, size: 4, timestamp: now - day }),
  ].sort((a, b) => b.timestamp - a.timestamp);
  const positionRows = [{
    proxy_wallet: wallet, token_id: '1', condition_id: '0xheld', current_size: 200, avg_price: 0.5,
    entry_cost_usdc: 100, entry_fees_usdc: 0.4, total_cost_usdc: 100.4, current_price: 0.4, current_value: 80,
    total_size: 200, realized_pnl: 0, unrealized_pnl: -20, total_pnl: -20, percent_pnl: -20,
    percent_realized_pnl: -20, status: 'OPEN', redeemable: false, mergeable: false, negative_risk: false,
    archived: false, title: 'Held market', slug: 'held', icon: 'held.png', event_id: '1', event_slug: 'held-event',
    outcome: 'Yes', outcome_index: 0, opposite_outcome: 'No', opposite_token_id: '2', end_date: '2026-12-31',
    last_event_at: now - day, first_entry_at: now - day, name: 'n', profile_image: '', verified: false,
  }];
  const fetchImpl = async (url) => {
    const u = new URL(url);
    if (u.pathname === '/v2/activity') return page(activityRows);
    if (u.pathname === '/v2/positions') return page(positionRows);
    throw new Error(`unexpected ${u.pathname}`);
  };
  const [tape, open] = await withFetch(fetchImpl, () => Promise.all([pm.activity(wallet), pm.positions(wallet)]));
  const out = buildProfile({
    wallet, tape, open, rankRows: {}, value: 80, cash: { usd: null, source: 'unavailable', asOf: null },
  });
  assert.equal(out.tapeComplete, true);
  assert.equal(out.summary.trades, 2, 'the combo fill is left out');
  assert.equal(out.summary.volume, 150);
  assert.equal(out.summary.resolved, 1);
  assert.equal(out.summary.settledPnl, 70);
  assert.equal(out.summary.openPnl, -20);
  assert.equal(out.summary.pnl, 50);
  assert.equal(out.summary.openValue, 80);
  assert.equal(out.summary.openMarkets, 1);
  assert.equal(out.summary.markets, 2);
  assert.equal(out.summary.rebateUsd, 4);
  assert.equal(out.windows.d7.volume, 150);
  // The wallet page reads these rows as they are.
  const [held] = out.positions.open;
  assert.equal(held.title, 'Held market');
  assert.equal(held.icon, 'held.png');
  assert.equal(held.eventSlug, 'held-event');
  assert.equal(held.outcome, 'Yes');
  assert.equal(held.avgPrice, 0.5);
  assert.equal(held.curPrice, 0.4);
  assert.equal(held.size, 200);
  assert.equal(held.currentValue, 80);
  assert.equal(held.cashPnl, -20);
  assert.equal(held.initialValue, 100);
  assert.equal(held.totalBought, 200);
  assert.equal(held.oppositeAsset, '2');
  assert.equal(held.endDate, '2026-12-31');
  assert.equal(held.negativeRisk, false);
  assert.equal(held.grossInitialValue, 100.4);
  assert.equal(out.trades[0].usdcSize, 100);
  assert.equal(out.positions.topWins[0].realizedPnl, 70);
});

// --- portfolio value and ranks --------------------------------------------

test('portfolio value reads data.value from /v2/value; a miss or a refusal is null', async () => {
  const calls = [];
  const answer = (body, status = 200) => async (url) => {
    calls.push(new URL(url));
    return { ok: status === 200, status, json: async () => body };
  };
  assert.equal(await withFetch(answer({ data: { proxy_wallet: wallet, value: 123.4567 } }), () => pm.portfolioValue(wallet)), 123.4567);
  assert.equal(await withFetch(answer({ data: { proxy_wallet: wallet, value: 0 } }), () => pm.portfolioValue(wallet)), 0);
  assert.equal(await withFetch(answer({ data: null }), () => pm.portfolioValue(wallet)), null);
  assert.equal(await withFetch(answer({ error: 'bad' }, 400), () => pm.portfolioValue(wallet)), null);
  assert.equal(`${calls[0].origin}${calls[0].pathname}`, 'https://data-api.polymarket.com/v2/value');
  assert.equal(calls[0].searchParams.get('user'), wallet);
});

test('ranks read one /v2/leaderboard standing per window, both ranks at once', async () => {
  const calls = [];
  const standings = {
    day: null,
    week: { user_id: wallet, pnl: -5, volume: 300, rank_pnl: null, rank_volume: 0, user_name: 'Name', profile_image: '' },
    month: { user_id: wallet, pnl: 40, volume: 900, rank_pnl: 120, rank_volume: 77, user_name: 'Name', profile_image: '' },
    all: { user_id: wallet, pnl: 1000, volume: 5000, rank_pnl: 42, rank_volume: 7, user_name: 'Name', profile_image: 'me.png' },
  };
  const fetchImpl = async (url) => {
    const u = new URL(url);
    calls.push(u);
    return { ok: true, status: 200, json: async () => ({ data: standings[u.searchParams.get('time_period')] }) };
  };
  const out = await withFetch(fetchImpl, () => pm.ranks(wallet));
  assert.equal(calls.length, 4, 'one read per window');
  for (const u of calls) {
    assert.equal(`${u.origin}${u.pathname}`, 'https://data-api.polymarket.com/v2/leaderboard');
    assert.equal(u.searchParams.get('user'), wallet);
  }
  assert.deepEqual(calls.map((u) => u.searchParams.get('time_period')).sort(), ['all', 'day', 'month', 'week']);
  assert.deepEqual(out.all, { pnl: { amount: 1000, rank: 42 }, volume: { amount: 5000, rank: 7 } });
  assert.deepEqual(out.month, { pnl: { amount: 40, rank: 120 }, volume: { amount: 900, rank: 77 } });
  assert.deepEqual(out.week, { pnl: { amount: -5, rank: null }, volume: { amount: 300, rank: null } },
    'a null or 0 rank means unranked');
  assert.deepEqual(out.day, { pnl: null, volume: null }, 'no standing in the window');
  assert.equal(out.name, 'Name');
  assert.equal(out.image, 'me.png');
});

test('a window whose read fails is left out of the ranks', async () => {
  const fetchImpl = async (url) => {
    const w = new URL(url).searchParams.get('time_period');
    if (w === 'day') return { ok: false, status: 400, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => ({ data: { pnl: 1, volume: 2, rank_pnl: 3, rank_volume: 4 } }) };
  };
  const out = await withFetch(fetchImpl, () => pm.ranks(wallet));
  assert.equal(out.day, undefined);
  assert.deepEqual(out.all, { pnl: { amount: 1, rank: 3 }, volume: { amount: 2, rank: 4 } });
});
