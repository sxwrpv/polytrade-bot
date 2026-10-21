/* Rules the board must keep. Run: node --test tests/ */
import test from 'node:test';
import assert from 'node:assert/strict';
import * as M from '../public/lib/screenerModel.js';
import { buildEpisodes, buildProfile } from '../lib/metrics.mjs';
import { positions, POSITIONS_CEILING } from '../lib/polymarket.mjs';
import * as cs from '../lib/copyscore.mjs';
import { queryWallets, project, enforceRateLimit, resetRateLimits } from '../lib/publicScreener.mjs';

const trader = (over = {}) => ({
  w: '0x' + '1'.repeat(40), name: 'w', cats: ['Sports'], lastTradeDay: '2026-08-20',
  pnl: 1000, d7: 100, d30: 500, vol: 200000, d7Vol: 50000, d30Vol: 100000,
  winRate: 0.5, openVal: 0, copyClass: 'strong', copyNet: 12, activeDays: 40, ...over,
});

test('ROI is undefined without volume, not zero', () => {
  assert.equal(M.roiIn(trader({ d7Vol: 0, d7: 0 }), 'd7'), null);
  assert.equal(M.roiIn(trader({ d7Vol: 50000, d7: 5000 }), 'd7'), 10);
});

test('rate-based orders require the window volume floor', () => {
  const thin = trader({ w: '0x' + '2'.repeat(40), d7Vol: 24999 });
  const thick = trader({ w: '0x' + '3'.repeat(40), d7Vol: 25000 });
  const board = M.buildBoard([thin, thick], { metric: 'copy', period: 'd7' });
  assert.deepEqual(board.map((t) => t.w), [thick.w]);
  // PnL ordering has no such floor: it is not a rate.
  assert.equal(M.buildBoard([thin, thick], { metric: 'pnl', period: 'd7' }).length, 2);
});

test('Copy Score orders by band first and money only as tie-break', () => {
  const rich = trader({ w: '0x' + 'a'.repeat(40), copyClass: 'marginal', copyNet: 3, d7: 9_000_000 });
  const poorA = trader({ w: '0x' + 'b'.repeat(40), copyClass: 'strong', copyNet: 11, d7: 10 });
  const poorB = trader({ w: '0x' + 'c'.repeat(40), copyClass: 'strong', copyNet: 40, d7: 20 });
  const board = M.buildBoard([rich, poorA, poorB], {
    metric: 'copy', period: 'd7', bands: new Set(['strong', 'marginal']),
  });
  assert.deepEqual(board.map((t) => t.w), [poorB.w, poorA.w, rich.w]);
});

test('the default board shows only bands that clear copy costs', () => {
  const rows = ['strong', 'marginal', 'uneconomic', 'loss_making', 'not_measurable']
    .map((c, i) => trader({ w: '0x' + String(i).repeat(40), copyClass: c, copyNet: 1 }));
  const board = M.buildBoard(rows, { metric: 'copy', period: 'd7' });
  assert.deepEqual(board.map((t) => t.copyClass), ['strong', 'marginal']);
});

test('the lifetime board hides wallets stale for 30 days', () => {
  const fresh = trader({ w: '0x' + 'd'.repeat(40), lastTradeDay: '2026-08-01' });
  const stale = trader({ w: '0x' + 'e'.repeat(40), lastTradeDay: '2026-07-01' });
  const opts = { metric: 'pnl', period: 'all', asOf: '2026-08-21' };
  assert.deepEqual(M.buildBoard([fresh, stale], opts).map((t) => t.w), [fresh.w]);
  // The 7-day board does not apply that cut; the window already does it.
  assert.equal(M.buildBoard([fresh, stale], { ...opts, period: 'd7' }).length, 2);
  // An unknown last trade is never a reason to hide a wallet, and the board counts it.
  const unknown = trader({ w: '0x' + 'f'.repeat(40), lastTradeDay: null });
  assert.deepEqual(M.buildBoard([fresh, stale, unknown], opts).map((t) => t.w).sort(), [fresh.w, unknown.w].sort());
  assert.equal(M.lastTradeUnknown([fresh, stale, unknown], { period: 'all' }), 1);
  assert.equal(M.lastTradeUnknown([fresh, stale, unknown], { period: 'd7' }), 0);
});

test('an unsupported period or sort fails loudly', () => {
  assert.throws(() => M.buildBoard([], { period: '7d' }), /unsupported period/);
  assert.throws(() => M.buildBoard([], { metric: 'sharpe' }), /unsupported sort/);
});

test('band runs are emitted once per contiguous band', () => {
  const rows = [
    trader({ copyClass: 'strong' }), trader({ copyClass: 'strong' }), trader({ copyClass: 'marginal' }),
  ];
  assert.deepEqual(M.bandRuns(rows, 'copy'), [
    { index: 0, band: 'strong', total: 2 },
    { index: 2, band: 'marginal', total: 1 },
  ]);
  // No bands when the board is ordered by money.
  assert.deepEqual(M.bandRuns(rows, 'pnl'), []);
});

test('score formatting matches the two published forms', () => {
  assert.equal(M.scoreSigned(6.4), '+6');
  assert.equal(M.scoreSigned(-4.2), '−4');
  assert.equal(M.scoreSigned(0.2), '0');
  assert.equal(M.scoreMagnitude(11.4), '11%');
  assert.equal(M.scoreMagnitude(-4.2), '−4%');
  assert.equal(M.scoreSigned(null), null);
});

test('Smart Money narrative chooses human-readable text from the upstream object', () => {
  assert.equal(M.smartMoneyNarrative({
    narrative: { hero: 'Smart money is watching.', take: 'The cohort is steady.' },
  }), 'Smart money is watching.');
  assert.equal(M.smartMoneyNarrative({ narrative: 'Already readable.' }), 'Already readable.');
  assert.equal(M.smartMoneyNarrative({ narrative: {} }), '');
});

test('wallet links stay inside the canonical production screener route', () => {
  const wallet = '0x' + 'a'.repeat(40);
  assert.equal(M.traderPath(wallet), `/screener/trader/${wallet}`);
});

test('an absent metric renders as unavailable, never as zero', () => {
  assert.equal(M.money(null), '—');
  assert.equal(M.signedMoney(undefined), '—');
  assert.equal(M.percent(null), '—');
  assert.equal(M.signedPercent(NaN), '—');
  assert.equal(M.money(0), '$0');
});

test('money formatting matches the published shapes', () => {
  assert.equal(M.money(368000), '$368K');
  assert.equal(M.money(-38000), '−$38K');
  assert.equal(M.money(1_700_000), '$1.7M');
  assert.equal(M.signedMoney(108000), '+$108K');
});

test('an address is accepted bare or inside a URL', () => {
  const a = '0xE734E7BF7CFB9E464681F71822F6C2F6BE514F0C';
  assert.equal(M.resolveAddress(a), a.toLowerCase());
  assert.equal(M.resolveAddress(`https://polymarket.com/profile/${a}`), a.toLowerCase());
  assert.equal(M.resolveAddress('boyau'), null);
});

test('the bot hand-off does not pretend to carry the wallet', () => {
  // Flipping SUPPORTS_WALLET_DEEP_LINK is the only thing that should change this.
  assert.equal(M.SUPPORTS_WALLET_DEEP_LINK, false);
  assert.equal(M.botDeepLink('0x' + '1'.repeat(40)), 'https://t.me/cpolytrade_bot');
});

// --- episode reconstruction ------------------------------------------------

test('an episode is finished only when nothing is still held in it', () => {
  const tape = [
    { conditionId: 'A', type: 'TRADE', side: 'BUY', usdcSize: 100, size: 200, timestamp: 1, outcomeIndex: 0, title: 'A' },
    { conditionId: 'A', type: 'REDEEM', usdcSize: 200, timestamp: 9 },
    { conditionId: 'B', type: 'TRADE', side: 'BUY', usdcSize: 50, size: 100, timestamp: 2, outcomeIndex: 0, title: 'B' },
  ];
  const eps = buildEpisodes(tape, [{ conditionId: 'B' }]);
  const a = eps.find((e) => e.conditionId === 'A');
  const b = eps.find((e) => e.conditionId === 'B');
  assert.equal(a.finished, true);
  assert.equal(a.pnl, 100);
  assert.equal(a.avgEntry, 0.5);
  assert.equal(b.finished, false, 'still held, so not finished');
});

test('inventory arriving off-market marks an episode unpriceable', () => {
  const tape = [
    { conditionId: 'C', type: 'TRADE', side: 'BUY', usdcSize: 100, size: 200, timestamp: 1, outcomeIndex: 0 },
    { conditionId: 'C', type: 'SPLIT', usdcSize: 500, timestamp: 2 },
    { conditionId: 'C', type: 'REDEEM', usdcSize: 900, timestamp: 3 },
  ];
  const [ep] = buildEpisodes(tape, []);
  assert.equal(ep.finished, true);
  assert.equal(ep.priceable, false, 'a split has no entry price to copy at');
});

test('an incomplete current-position feed fails closed before resolved metrics are built', () => {
  const tape = [];
  tape.complete = true;
  const open = [];
  open.complete = false;
  assert.throws(() => buildProfile({
    wallet: '0x' + '1'.repeat(40), tape, open, rankRows: {}, value: null,
    cash: { usd: null, source: 'unavailable', asOf: null },
  }), /current positions are incomplete/);
});

test('a feed that stopped at worthless positions still builds a profile, and says so', () => {
  const tape = [{ conditionId: 'D', type: 'TRADE', side: 'BUY', usdcSize: 40, size: 100, timestamp: 1, outcomeIndex: 0 }];
  tape.complete = true;
  const open = [{ conditionId: 'C', currentValue: 12, cashPnl: 2 }];
  open.complete = true;
  open.worthlessSkipped = true;
  const out = buildProfile({
    wallet: '0x' + '1'.repeat(40), tape, open, rankRows: {}, value: null,
    cash: { usd: null, source: 'unavailable', asOf: null },
  });
  assert.equal(out.positions.openWorthlessSkipped, true);
  assert.equal(out.summary.resolved, 1, 'D is past the list read, so it counts as a finished, lost bet');
});

// --- Polymarket positions feed ---------------------------------------------

/** A fake /positions feed: `total` rows sorted by value, 500 a page. */
function positionsFeed(total, valueAt, { failFirst = null } = {}) {
  const calls = [];
  let failed = false;
  const fetchImpl = async (url) => {
    const u = new URL(url);
    const offset = Number(u.searchParams.get('offset'));
    calls.push(offset);
    if (failFirst && !failed) { failed = true; return { ok: false, status: failFirst, json: async () => ({}) }; }
    const count = Math.max(0, Math.min(Number(u.searchParams.get('limit')), total - offset));
    const rows = Array.from({ length: count }, (_, i) => ({ conditionId: `c${offset + i}`, currentValue: valueAt(offset + i) }));
    return { ok: true, status: 200, json: async () => rows };
  };
  return { calls, fetchImpl };
}

async function withFetch(fetchImpl, work) {
  const real = globalThis.fetch;
  globalThis.fetch = fetchImpl;
  try { return await work(); } finally { globalThis.fetch = real; }
}

const someWallet = '0x' + '2'.repeat(40);

test('under 3,000 positions the whole list is read, $0 positions included', async () => {
  const feed = positionsFeed(1200, (i) => (i < 400 ? 5 : 0));
  const rows = await withFetch(feed.fetchImpl, () => positions(someWallet));
  assert.equal(rows.length, 1200);
  assert.equal(rows.complete, true);
  assert.equal(rows.worthlessSkipped, false);
});

test('past 3,000 positions reading stops at the first page that ends on a $0 position', async () => {
  // 32 of the 181 wallets on the 7D board held over 3,000 on 2026-10-21; the old cap failed them all.
  const feed = positionsFeed(6600, (i) => (i < 2000 ? 100 - i * 0.04 : 0));
  const rows = await withFetch(feed.fetchImpl, () => positions(someWallet));
  assert.equal(rows.length, 3000);
  assert.equal(rows.complete, true);
  assert.equal(rows.worthlessSkipped, true);
  assert.ok(Math.max(...feed.calls) < 6600, 'the $0 tail is never paged through');
});

test('a list that still has value at the ceiling is refused as too big, with no retry advice', async () => {
  const feed = positionsFeed(POSITIONS_CEILING + 2000, () => 1);
  const rows = await withFetch(feed.fetchImpl, () => positions(someWallet));
  assert.equal(rows.length, POSITIONS_CEILING);
  assert.equal(rows.complete, false);
  assert.equal(rows.overCeiling, POSITIONS_CEILING);
  const tape = [];
  tape.complete = true;
  assert.throws(() => buildProfile({
    wallet: someWallet, tape, open: rows, rankRows: {}, value: null,
    cash: { usd: null, source: 'unavailable', asOf: null },
  }), (error) => error.status === 422 && /more than 10,000 open positions/.test(error.message));
});

test('a 429 is retried and a 400 ends the read as incomplete', async () => {
  const throttled = positionsFeed(300, () => 3, { failFirst: 429 });
  const rows = await withFetch(throttled.fetchImpl, () => positions(someWallet));
  assert.equal(rows.length, 300);
  assert.equal(rows.complete, true);
  assert.deepEqual(throttled.calls, [0, 0]);

  const refused = positionsFeed(300, () => 3, { failFirst: 400 });
  const none = await withFetch(refused.fetchImpl, () => positions(someWallet));
  assert.equal(none.complete, false);
  assert.deepEqual(refused.calls, [0]);
});

// --- Copy Score ------------------------------------------------------------

test('too few finished trades withholds the verdict rather than scoring it', () => {
  const out = cs.compute({ grossReturnPct: 400, resolved: 2, avgEntryPrice: 0.5, soldBeforeResolution: 0 });
  assert.equal(out.class, 'unproven');
  assert.equal(out.netReturnPct, null, 'no number is published for a thin record');
});

test('a wallet that sells out most of its value is not comparable', () => {
  const out = cs.compute({ grossReturnPct: 60, resolved: 80, avgEntryPrice: 0.5, soldBeforeResolution: 0.9 });
  assert.equal(out.class, 'not_measurable');
});

test('losing before costs is graded as losing, not as uneconomic', () => {
  const out = cs.compute({ grossReturnPct: -12, resolved: 80, avgEntryPrice: 0.5, soldBeforeResolution: 0 });
  assert.equal(out.class, 'loss_making');
});

test('net return is the shrunk return less spread and fees', () => {
  const out = cs.compute({ grossReturnPct: 81.37, resolved: 6, avgEntryPrice: 0.472, soldBeforeResolution: 0 });
  assert.equal(out.costPct, out.costSpreadPct + out.costFeesPct);
  assert.ok(Math.abs(out.netReturnPct - (out.shrunkReturnPct - out.costPct)) < 0.02);
  // Reproduces the published example within a rounding step.
  assert.ok(Math.abs(out.shrunkReturnPct - 2.58) < 0.15, `shrunk was ${out.shrunkReturnPct}`);
  assert.equal(out.class, 'uneconomic');
});

test('class cut-points hold at the boundaries', () => {
  const at = (net) => cs.compute({
    grossReturnPct: (net + 4.62) * (200 + cs.COST_MODEL.shrinkPrior) / 200,
    resolved: 200, avgEntryPrice: 0.5, soldBeforeResolution: 0,
  }).class;
  assert.equal(at(11), 'strong');
  assert.equal(at(5), 'marginal');
  assert.equal(at(-1), 'uneconomic');
});

// --- the PolyTrade-shaped endpoint -----------------------------------------

test('the projection is an allowlist, and absent stays absent', () => {
  const row = project(trader({ winRate: null, d30: 500, d30Vol: 100000, activeDays: 5 }), '30d');
  assert.deepEqual(Object.keys(row).sort(), [
    'active_positions', 'address', 'consistency_ratio', 'daily_pnl', 'display_name', 'fill_exit_ratio',
    'history_coverage', 'history_days', 'history_partial', 'period', 'period_days', 'pnl',
    'stats_refreshed_at', 'verified', 'volume', 'win_rate', 'x_username',
  ]);
  assert.equal(row.win_rate, null, 'a missing win rate is not a zero');
  assert.equal(row.history_days, null, 'active days cannot stand in for history reach-back');
  assert.equal(row.history_partial, true);
  assert.equal(row.history_coverage, 'unknown');
});

test('the endpoint rejects unsupported periods rather than substituting lifetime totals', () => {
  assert.throws(() => queryWallets([], new URLSearchParams('period=90d')), /period must be one of 7d, 30d/);
  assert.throws(() => project(trader(), '90d'), /period must be one of 7d, 30d/);
});

test('complete-history filtering fails closed when snapshot reach-back is unknown', () => {
  const out = queryWallets([trader()], new URLSearchParams('period=30d&complete_history_only=true'));
  assert.equal(out.total, 0);
});

test('the endpoint rejects an unsupported period or sort', () => {
  assert.throws(() => queryWallets([], new URLSearchParams('period=1d')), /period must be one of/);
  assert.throws(() => queryWallets([], new URLSearchParams('sort=sharpe')), /sort must be one of/);
  assert.throws(() => queryWallets([], new URLSearchParams('limit=nan')), /limit must be an integer/);
  assert.throws(() => queryWallets([], new URLSearchParams('offset=-1')), /offset must be an integer/);
  assert.throws(() => queryWallets([], new URLSearchParams('pnl_min=nan')), /pnl_min must be a number/);
});

test('filters narrow the page and the total reports the filtered count', () => {
  const rows = [trader({ w: '0x' + '7'.repeat(40), d30: 10 }), trader({ w: '0x' + '8'.repeat(40), d30: 900 })];
  const out = queryWallets(rows, new URLSearchParams('period=30d&pnl_min=100'));
  assert.equal(out.total, 1);
  assert.equal(out.count, 1);
  assert.equal(out.has_more, false);
  assert.ok(out.provenance.source.includes('Polycopy'));
  assert.equal(out.wallets[0].pnl, 900);
});

test('the public endpoint is rate limited per client', () => {
  resetRateLimits();
  for (let i = 0; i < 60; i++) enforceRateLimit('1.2.3.4');
  assert.throws(() => enforceRateLimit('1.2.3.4'), /rate limited/);
  assert.doesNotThrow(() => enforceRateLimit('5.6.7.8'), 'the budget is per client');
  resetRateLimits();
});

// --- numeric filters --------------------------------------------------------

test('a blank filter is inactive; zero is a real threshold', () => {
  assert.equal(M.finiteFilter(''), null);
  assert.equal(M.finiteFilter(null), null);
  assert.equal(M.finiteFilter('  '), null);
  assert.equal(M.finiteFilter('not a number'), null);
  assert.equal(M.finiteFilter('0'), 0, 'zero is a filter, not an absence of one');
});

test('an absent metric fails a filter that asks about it', () => {
  const noWinRate = trader({ winRate: null });
  assert.equal(M.passesFilters(noWinRate, { ...M.DEFAULT_FILTERS }, 'd7'), true, 'no filter, no opinion');
  assert.equal(
    M.passesFilters(noWinRate, { ...M.DEFAULT_FILTERS, winrateMin: '0' }, 'd7'), false,
    'an unknown win rate must not pass "at least 0%" by defaulting to zero',
  );
});

test('avgSizeMax is a ceiling, and an unknown fill size does not slip through', () => {
  const big = trader({ avgSize: 220_000 });
  const small = trader({ avgSize: 180 });
  const unknown = trader({ avgSize: null });
  const f = { ...M.DEFAULT_FILTERS, avgSizeMax: '1000' };
  assert.equal(M.passesFilters(big, f, 'd7'), false);
  assert.equal(M.passesFilters(small, f, 'd7'), true);
  assert.equal(M.passesFilters(unknown, f, 'd7'), false);
});

test('hard-to-mirror wallets are dropped only when asked for', () => {
  const mm = trader({ mm: 'market_maker' });
  const arb = trader({ arb: 'arb' });
  const fast = trader({ freq: 'vhft' });
  const plain = trader({ mm: 'directional', arb: 'none', freq: 'regular' });
  assert.deepEqual([mm, arb, fast, plain].map(M.isHardToMirror), [true, true, true, false]);
  const f = { ...M.DEFAULT_FILTERS, excludeHardToMirror: true };
  assert.deepEqual([mm, arb, fast, plain].map((t) => M.passesFilters(t, f, 'd7')),
    [false, false, false, true]);
});

test('money thresholds sit on a 1–2.5–5 ladder', () => {
  assert.deepEqual(M.ladder(1_000, 1_000_000),
    [1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000]);
  assert.deepEqual(M.ladder(100, 300), [100, 250, 500], 'stops at the first rung at or above the top');
  assert.deepEqual(M.ladder(0, 10), [], 'a ladder needs a positive floor');
});

test('the volume slider is sized from the selected window and reaches at least $1M', () => {
  // The live leaderboard fills only the window's own fields: a 7D row has
  // d7Vol and no lifetime vol. Sizing from the lifetime field left a slider
  // that stopped at $1,000.
  const live = [800, 4_500, 90_000].map((d7Vol) => trader({ vol: null, pnl: null, d7Vol }));
  const spec = M.filterSpec('volumeMin');
  const stops = spec.stops(live.map((t) => spec.value(t, 'd7')));
  assert.equal(stops[0], 1_000);
  assert.equal(stops.at(-1), 1_000_000, 'a quiet week still offers $1M');
  const busy = [trader({ d7Vol: 37_000_000 }), ...Array.from({ length: 99 }, () => trader({ d7Vol: 30_000_000 }))];
  assert.equal(spec.stops(busy.map((t) => spec.value(t, 'd7'))).at(-1), 25_000_000,
    'and reaches the busiest wallets, without a last step that keeps nobody');
});

test('a filter with no figure in the window is not offered', () => {
  const live = [trader({ winRate: null, activeDays: null, avgSize: null, mm: null, arb: null, freq: null })];
  assert.equal(M.filterAvailable(M.filterSpec('winrateMin'), live, 'd7'), false);
  assert.equal(M.filterAvailable(M.filterSpec('volumeMin'), live, 'd7'), true);
  assert.equal(M.mirrorFlagsKnown(live), false);
  assert.equal(M.mirrorFlagsKnown([trader({ mm: 'directional' })]), true);
});

test('histogram bars line up with slider positions, floor and ceiling alike', () => {
  const t = [1_000, 2_500, 5_000];
  // floor: below 1K | [1K, 2.5K) | [2.5K, 5K) | 5K and up
  assert.deepEqual(M.histogram([0, 999, 1_000, 2_499, 2_500, 9_000, null, NaN], t, 'min'), [2, 2, 1, 1]);
  // ceiling: up to 1K | (1K, 2.5K] | (2.5K, 5K] | above 5K
  assert.deepEqual(M.histogram([1_000, 1_001, 2_500, 5_000, 5_001], t, 'max'), [1, 2, 1, 1]);
  // Position 0 is off for a floor; the last position is off for a ceiling.
  assert.equal(M.positionValue(t, 0, 'min'), '');
  assert.equal(M.positionValue(t, 2, 'min'), '2500');
  assert.equal(M.positionValue(t, 3, 'max'), '');
  assert.equal(M.stopPosition(t, '', 'min'), 0);
  assert.equal(M.stopPosition(t, '2500', 'min'), 2);
  assert.equal(M.stopPosition(t, '3000', 'min'), 2, 'an off-ladder value from an old link lands on the rung below');
  assert.equal(M.stopPosition(t, '', 'max'), 3);
  assert.equal(M.stopPosition(t, '2000', 'max'), 1);
});

test('filters narrow the board and survive the ordering path', () => {
  const rows = [
    trader({ w: '0x' + 'a'.repeat(40), d7: 50, mm: 'market_maker' }),
    trader({ w: '0x' + 'b'.repeat(40), d7: 40_000 }),
    trader({ w: '0x' + 'c'.repeat(40), d7: 100 }),
  ];
  const board = M.buildBoard(rows, {
    metric: 'pnl', period: 'd7',
    filters: { ...M.DEFAULT_FILTERS, pnlMin: '1000', excludeHardToMirror: true },
  });
  assert.deepEqual(board.map((t) => t.d7), [40_000]);
});

// --- sort direction ---------------------------------------------------------

test('ascending flips the money order and the band order together', () => {
  const rows = [
    trader({ w: '0x' + 'a'.repeat(40), copyClass: 'strong', copyNet: 11, d7: 10 }),
    trader({ w: '0x' + 'b'.repeat(40), copyClass: 'marginal', copyNet: 3, d7: 20 }),
  ];
  const desc = M.buildBoard(rows, { metric: 'copy', period: 'd7', direction: 'desc' });
  const asc = M.buildBoard(rows, { metric: 'copy', period: 'd7', direction: 'asc' });
  assert.deepEqual(desc.map((t) => t.copyClass), ['strong', 'marginal']);
  assert.deepEqual(asc.map((t) => t.copyClass), ['marginal', 'strong']);
  const descPnl = M.buildBoard(rows, { metric: 'pnl', period: 'd7', direction: 'desc' });
  const ascPnl = M.buildBoard(rows, { metric: 'pnl', period: 'd7', direction: 'asc' });
  assert.deepEqual(descPnl.map((t) => t.d7), [20, 10]);
  assert.deepEqual(ascPnl.map((t) => t.d7), [10, 20]);
});

// --- shareable state --------------------------------------------------------

test('only non-default state reaches the URL', () => {
  const defaults = { view: 'all', metric: 'copy', period: 'd7', cat: 'all', direction: 'desc' };
  const clean = M.encodeState(
    { ...defaults, bands: M.RECOMMENDED, filters: { ...M.DEFAULT_FILTERS } }, defaults);
  assert.equal(clean, '', 'the default board is a bare URL');

  const dirty = M.encodeState({
    ...defaults, metric: 'pnl', cat: 'Sports', bands: M.RECOMMENDED,
    filters: { ...M.DEFAULT_FILTERS, pnlMin: '5000', excludeHardToMirror: true },
  }, defaults);
  const q = new URLSearchParams(dirty);
  assert.equal(q.get('metric'), 'pnl');
  assert.equal(q.get('cat'), 'Sports');
  assert.equal(q.get('pnlMin'), '5000');
  assert.equal(q.get('excludeHardToMirror'), '1');
  assert.equal(q.get('period'), null, 'an unchanged period is not written');
});

test('a decoded URL round-trips, and junk in it is ignored', () => {
  const defaults = { view: 'all', metric: 'copy', period: 'd7', cat: 'all', direction: 'desc' };
  const state = M.decodeState(
    '?metric=pnl&period=d30&cat=Sports&pnlMin=5000&excludeHardToMirror=1&bands=strong', defaults);
  assert.equal(state.metric, 'pnl');
  assert.equal(state.period, 'd30');
  assert.equal(state.cat, 'Sports');
  assert.equal(state.filters.pnlMin, '5000');
  assert.equal(state.filters.excludeHardToMirror, true);
  assert.deepEqual([...state.bands], ['strong']);

  const junk = M.decodeState('?metric=sharpe&period=1y&bands=nonsense&nope=1', defaults);
  assert.equal(junk.metric, 'copy', 'an unsupported sort falls back to the default');
  assert.equal(junk.period, 'd7');
  assert.equal(junk.bands, undefined, 'an unrecognised band list is dropped entirely');
});

// --- export -----------------------------------------------------------------

test('CSV leaves an absent metric empty rather than writing a zero', () => {
  const csv = M.toCsv([trader({ name: 'a,b "quoted"', winRate: null, copyNet: 12 })], 'd7');
  const [header, row] = csv.split('\n');
  assert.ok(header.startsWith('rank,wallet,name,'));
  assert.ok(row.includes('"a,b ""quoted"""'), 'commas and quotes are escaped');
  const cols = header.split(',');
  const cells = row.match(/("([^"]|"")*"|[^,]*)/g).filter((_, i) => i % 2 === 0);
  assert.equal(cells[cols.indexOf('win_rate')], '', 'no win rate writes an empty cell, not 0');
  assert.equal(cells[cols.indexOf('copy_score')], '12');
});

// --- snapshot age -----------------------------------------------------------

test('snapshot age reads in the units a person would use, and flags stale', () => {
  const at = (h) => M.snapshotAge('2026-08-21T00:00:00Z', Date.parse('2026-08-21T00:00:00Z') + h * 3.6e6);
  assert.equal(at(0.5).label, 'under an hour old');
  assert.equal(at(6).label, '6h old');
  assert.equal(at(72).label, '3d old');
  assert.equal(at(6).stale, false);
  assert.equal(at(72).stale, true, 'the cohort regenerates daily, so 3 days old is stale');
  assert.equal(M.snapshotAge(null).stale, true, 'an unknown age is treated as stale, not as fresh');
});

// --- the PolyTrade source adapter -------------------------------------------
// This is the whole integration surface, so it is pinned: a PolyTrade row must
// land in the board's shape with nothing invented along the way.

test('a PolyTrade wallet row maps into the board shape without inventing a score', async () => {
  const { fromPolytradeWallet, toPolytradeQuery } = await import('../public/lib/dataSource.js');
  const row = fromPolytradeWallet({
    address: '0xAbC' + '1'.repeat(37), display_name: 'w', pnl: 1200, win_rate: 0.55,
    volume: 90_000, period_days: 30, history_days: 12, history_partial: true,
    consistency_ratio: 0.6, fill_exit_ratio: 40, active_positions: 3, verified: true,
  }, 'd30');

  assert.equal(row.w, row.w.toLowerCase(), 'the address is normalised');
  assert.equal(row.d30, 1200);
  assert.equal(row.d30Vol, 90_000);
  assert.equal(row.copyClass, null, 'PolyTrade publishes no composite score');
  assert.equal(row.copyNet, null);
  assert.equal(row.historyPartial, true);
  assert.equal(row.fillExitRatio, 40);
  // The board must survive a row with no score: ordering falls back to money.
  const board = M.buildBoard([row], { metric: 'pnl', period: 'd30' });
  assert.equal(board.length, 1);
  assert.equal(M.classDef(row.copyClass).chip, 'Not scored');

  const q = toPolytradeQuery({ period: 'd30', metric: 'copy', search: '  boyau ' });
  assert.equal(q.period, '30d');
  assert.equal(q.sort, 'pnl', 'Copy Score has no server-side equivalent, so it degrades to PnL');
  assert.equal(q.search, 'boyau');
  assert.throws(() => toPolytradeQuery({ period: 'all' }), /supports only d7 and d30/);
});

// --- saved wallets ----------------------------------------------------------

function fakeStorage(seed = {}) {
  const d = { ...seed };
  return {
    d,
    getItem: (k) => (k in d ? d[k] : null),
    setItem: (k, v) => { d[k] = v; },
  };
}

test('the old Following list is carried over on first read, once', async () => {
  const { createStore } = await import('../public/lib/saved.js');
  const mem = fakeStorage({ 'polytrade.following': JSON.stringify(['0x' + '1'.repeat(40), 'not-an-address']) });
  const store = createStore(mem, () => 1000);
  assert.equal(store.count(), 1, 'junk in the legacy list is dropped');
  assert.equal(store.list()[0].migrated, true);
  assert.ok(mem.d['polytrade.saved'], 'the migration is written back');
  // The legacy key is left alone rather than deleted: nothing else reads it,
  // and removing it would make the migration unrepeatable if this one failed.
  assert.ok(mem.d['polytrade.following']);
});

test('a save keeps a snapshot, and re-saving replaces rather than duplicates', async () => {
  const { createStore } = await import('../public/lib/saved.js');
  const store = createStore(fakeStorage(), () => 5000);
  const w = '0x' + 'a'.repeat(40);
  store.save(w, { name: 'boyau', copyClass: 'strong', copyNet: 11, pnl: 700_000 });
  store.save(w, { name: 'boyau renamed', copyClass: 'marginal', copyNet: 3 });
  assert.equal(store.count(), 1);
  const [rec] = store.list();
  assert.equal(rec.name, 'boyau renamed');
  assert.equal(rec.copyNet, 3);
  assert.equal(rec.pnl, null, 'a field absent from the new snapshot is not carried over stale');
});

test('toggle reports the state it left behind', async () => {
  const { createStore } = await import('../public/lib/saved.js');
  const store = createStore(fakeStorage(), () => 1);
  const w = '0x' + 'b'.repeat(40);
  assert.equal(store.toggle(w, {}), true);
  assert.equal(store.has(w), true);
  assert.equal(store.toggle(w), false);
  assert.equal(store.has(w), false);
});

test('a malformed address is never saved', async () => {
  const { createStore } = await import('../public/lib/saved.js');
  const store = createStore(fakeStorage(), () => 1);
  for (const bad of ['', null, 'boyau', '0x123', '0x' + 'z'.repeat(40)]) {
    assert.equal(store.save(bad, {}), false, `${bad} must be rejected`);
  }
  assert.equal(store.count(), 0);
});

test('the list is newest first, and subscribers hear every change', async () => {
  const { createStore } = await import('../public/lib/saved.js');
  let t = 0;
  const store = createStore(fakeStorage(), () => (t += 1000));
  const seen = [];
  const off = store.subscribe((rows) => seen.push(rows.length));
  store.save('0x' + 'a'.repeat(40), {});
  store.save('0x' + 'b'.repeat(40), {});
  assert.deepEqual(store.list().map((r) => r.w[2]), ['b', 'a'], 'newest first');
  store.remove('0x' + 'a'.repeat(40));
  assert.deepEqual(seen, [1, 2, 1]);
  off();
  store.save('0x' + 'c'.repeat(40), {});
  assert.deepEqual(seen, [1, 2, 1], 'unsubscribing stops the callbacks');
});

test('a storage that throws does not break saving for the session', async () => {
  const { createStore } = await import('../public/lib/saved.js');
  const full = { getItem: () => null, setItem: () => { throw new Error('QuotaExceededError'); } };
  const store = createStore(full, () => 1);
  assert.doesNotThrow(() => store.save('0x' + 'd'.repeat(40), {}));
  assert.equal(store.count(), 1, 'the in-memory list still works when persistence fails');
});

test('save timestamps read as elapsed time, and a migrated row says so', async () => {
  const { savedAgo } = await import('../public/lib/saved.js');
  const now = Date.parse('2026-08-22T12:00:00Z');
  assert.equal(savedAgo(null), 'carried over');
  assert.equal(savedAgo(now - 30_000, now), 'just now');
  assert.equal(savedAgo(now - 20 * 60_000, now), '20m ago');
  assert.equal(savedAgo(now - 5 * 3.6e6, now), '5h ago');
  assert.equal(savedAgo(now - 26 * 3.6e6, now), 'yesterday');
  assert.equal(savedAgo(now - 4 * 864e5, now), '4d ago');
});

test('a link written before the rename still opens the Saved view', () => {
  const defaults = { view: 'all', metric: 'copy', period: 'd7', cat: 'all', direction: 'desc' };
  assert.equal(M.decodeState('?view=following', defaults).view, 'saved');
  assert.equal(M.decodeState('?view=saved', defaults).view, 'saved');
  assert.equal(M.decodeState('?view=nonsense', defaults).view, 'all');
});

test('the Saved view applies numeric filters but no eligibility cut', () => {
  const thin = trader({ w: '0x' + 'a'.repeat(40), d7Vol: 10, d7: 5, copyClass: 'loss_making', copyNet: -30 });
  const rich = trader({ w: '0x' + 'b'.repeat(40), d7: 50_000 });
  const set = new Set([thin.w, rich.w]);
  // A saved wallet is one you asked to watch: a thin window or a bad band does
  // not remove it the way it would on the open board.
  assert.equal(M.buildSaved([thin, rich], set, { metric: 'copy', period: 'd7' }).length, 2);
  // The reader's own filters still apply.
  const filtered = M.buildSaved([thin, rich], set, {
    metric: 'pnl', period: 'd7', filters: { ...M.DEFAULT_FILTERS, pnlMin: '1000' },
  });
  assert.deepEqual(filtered.map((t) => t.w), [rich.w]);
});
