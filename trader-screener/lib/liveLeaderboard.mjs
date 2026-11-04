import { deadlineSignal, polymarketFetch } from './polymarket.mjs';
import { createCoalescer, TtlLruCache } from './serviceGuards.mjs';

const WINDOWS = { d7: 'week', d30: 'month', all: 'all' };
/** Days in each board's window; all time has no bound. */
const WINDOW_DAYS = { d7: 7, d30: 30, all: null };
const LEADERBOARD = 'https://data-api.polymarket.com/v2/leaderboard';
const ACTIVITY = 'https://data-api.polymarket.com/v2/activity';
const USER_VOLUME = 'https://data-api.polymarket.com/v2/user-volume';
const HEADERS = { accept: 'application/json', 'user-agent': 'polytrade-screener/1.0' };
/* Each board holds the top 100 of Polymarket's PnL list and the top 100 of
 * its volume list. v1 cut any `limit` to 50 rows without an error, which is
 * how the board once held the top 50 of each. v2 serves up to 1,000 rows a
 * page, so a list is one request; its cursor is followed if a page ever comes
 * back with fewer rows. */
const LIMIT = 100;
/* A TRADE row on /v2/activity can be a Combo trade (`is_combo`), which the
 * wallet page leaves out of its tape. The lookup reads this many of a
 * wallet's latest trades to find its latest single-market one. */
const LAST_TRADE_ROWS = 20;

export function periodToLeaderboardWindow(period) {
  const window = WINDOWS[period];
  if (!window) throw new Error(`unsupported period: ${period}`);
  return window;
}

const addressOf = (row) => String(row?.user_id || '').toLowerCase();
const isWallet = (w) => /^0x[a-f0-9]{40}$/.test(w);
const finite = (value) => {
  if (value == null || value === '') return null;
  return Number.isFinite(Number(value)) ? Number(value) : null;
};

/**
 * @param {Map<string, string|null>} [o.lastTrades]  wallet -> UTC day of its
 *   last trade, from createLastTradeLookup. A wallet missing from it is
 *   unknown and stays null: the leaderboard says nothing about when a wallet
 *   last traded, so the fetch date must never stand in for it.
 * @param {Map<string, number>} [o.volumes]  wallet -> USD traded in the
 *   board's window, from createUsdVolumeLookup. A wallet missing from it has
 *   an unknown volume, and its volume and ROI stay null.
 *
 * Rows are /v2/leaderboard rows. Their `pnl` is USDC. Their `volume` counts
 * shares bought and sold, the figure polymarket.com/leaderboard serves; the
 * board's volume is USD, so that field stays out of the rows built here.
 */
export function buildLiveUniverse({
  period, pnlRows = [], volumeRows = [], snapshot, fetchedAt, lastTrades, volumes,
}) {
  periodToLeaderboardWindow(period);
  const overlay = new Map((snapshot?.traders || []).map((row) => [String(row.w).toLowerCase(), row]));
  const live = new Map();
  for (const row of [...pnlRows, ...volumeRows]) {
    const w = addressOf(row);
    if (!isWallet(w)) continue;
    live.set(w, { ...(live.get(w) || {}), ...row });
  }

  let volumeUnknown = 0;
  const traders = [...live.entries()].map(([w, row]) => {
    const score = overlay.get(w) || {};
    const pnl = finite(row.pnl);
    const vol = finite(volumes?.get(w));
    if (vol == null) volumeUnknown += 1;
    return {
      w,
      name: row.user_name || null,
      img: row.profile_image || null,
      xUsername: row.x_username || null,
      verified: Boolean(row.verified),
      followers: null,
      pnl: period === 'all' ? pnl : null,
      d7: period === 'd7' ? pnl : null,
      d30: period === 'd30' ? pnl : null,
      vol: period === 'all' ? vol : null,
      d7Vol: period === 'd7' ? vol : null,
      d30Vol: period === 'd30' ? vol : null,
      openVal: null,
      openPositions: null,
      copyClass: score.copyClass ?? null,
      copyNet: score.copyNet ?? null,
      mm: null,
      arb: null,
      freq: null,
      cats: [],
      fills: null,
      activeDays: null,
      winRate: null,
      avgSize: null,
      lastTradeDay: lastTrades?.get(w) ?? null,
      liveRank: finite(row.rank),
      livePeriod: period,
    };
  });

  return {
    meta: {
      source: 'live-polymarket-leaderboard',
      generatedAt: fetchedAt,
      windowAnchor: fetchedAt.slice(0, 10),
      scoreGeneratedAt: snapshot?.meta?.generatedAt || null,
      leaderboardWindow: periodToLeaderboardWindow(period),
      boardsFrozen: false,
      lastTradeUnknown: traders.filter((t) => t.lastTradeDay == null).length,
      volumeUnknown,
    },
    traders,
    walletMeta: {},
    spark: {},
    wow: {},
    copyDelta: {},
    angles: [],
    events: [],
    groups: [],
    structures: [],
    wowAnchor: null,
  };
}

/**
 * @param {(wallets: string[]) => Promise<Map>} [o.lastTradeDays]  the
 *   server's createLastTradeLookup; without it every last trade stays unknown.
 * @param {(wallets: string[], period: string) => Promise<Map>} [o.usdVolumes]
 *   the server's createUsdVolumeLookup; without it every volume stays unknown.
 */
export async function fetchLiveUniverse({
  period, snapshot, fetchImpl = polymarketFetch, fetchedAt = new Date().toISOString(), signal,
  lastTradeDays, usdVolumes,
}) {
  const timePeriod = periodToLeaderboardWindow(period);
  // Every page or none: a board missing ranks 51-100 of one list would look
  // complete while it is not.
  const getRows = async (sortBy) => {
    const rows = [];
    let cursor = null;
    do {
      const query = new URLSearchParams({ time_period: timePeriod, category: 'overall', sort_by: sortBy });
      if (cursor) query.set('cursor', cursor);
      else query.set('limit', String(LIMIT));
      const response = await fetchImpl(`${LEADERBOARD}?${query}`, { headers: HEADERS, signal });
      if (!response.ok) throw new Error(`Polymarket leaderboard ${response.status}`);
      const page = await response.json();
      if (!Array.isArray(page?.data)) throw new Error('Polymarket leaderboard returned an invalid payload');
      rows.push(...page.data);
      cursor = page.pagination?.next_cursor || null;
      if (cursor && !page.data.length) throw new Error('Polymarket leaderboard returned an empty page mid-list');
    } while (cursor && rows.length < LIMIT);
    return rows.slice(0, LIMIT);
  };
  const [pnlRows, volumeRows] = await Promise.all([getRows('PNL'), getRows('VOLUME')]);
  const wallets = [...new Set([...pnlRows, ...volumeRows].map(addressOf).filter(isWallet))];
  const [lastTrades, volumes] = await Promise.all([
    lastTradeDays ? lastTradeDays(wallets) : undefined,
    usdVolumes ? usdVolumes(wallets, period) : undefined,
  ]);
  return buildLiveUniverse({ period, pnlRows, volumeRows, snapshot, fetchedAt, lastTrades, volumes });
}

/**
 * The three live boards, built by `build(period)`. A board is rebuilt at most
 * once every `freshMs`, and visitors who ask at the same time share one
 * build. A board with wallets whose USD volume is still unknown is rebuilt
 * behind the visitor once it is `fillingMs` old: the visitor gets the board
 * already built at once, and the volumes a cold start fills in reach the
 * board within seconds of arriving. A failed rebuild behind a visitor keeps
 * the board already built.
 */
export function createLiveBoards({ build, now = Date.now, freshMs = 60_000, fillingMs = 15_000 }) {
  const boards = new Map();          // period -> { board, at }
  const builds = createCoalescer();
  const rebuild = (period) => builds.run(period, async () => {
    const board = await build(period);
    boards.set(period, { board, at: now() });
    return board;
  });
  return async function liveBoard(period) {
    const held = boards.get(period);
    const age = held ? now() - held.at : Infinity;
    if (age >= freshMs) return rebuild(period);
    if (held.board.meta?.volumeUnknown > 0 && age >= fillingMs) rebuild(period).catch(() => {});
    return held.board;
  };
}

/**
 * Values fetched one key at a time behind the boards: a wallet's last trade,
 * a wallet's USD volume in a window.
 *
 * Polymarket limits requests per IP, and this service shares the server's IP
 * with the copy engine, which polls the same data API for the trades it
 * copies. So a lookup sends one request at a time and at most `perSecond` a
 * second, however many boards ask at once, and `fetchValue` draws on the
 * screener's shared budget (polymarketGate) as background work, after any
 * board or wallet page that is waiting. A cold cache therefore fills over
 * minutes.
 *
 * Built once per server. A value is kept for the cache's TTL and served at
 * once; one older than `refreshAfterMs` is renewed behind the board, after
 * every key nobody knows yet. A board waits at most `budgetMs` for unknown
 * keys and the rest land in the cache for the next refresh. A failed lookup
 * is left out rather than guessed, the value already cached is served until
 * its TTL runs out, and that key is not asked again for two minutes.
 */
function createPacedLookup({
  fetchValue, perSecond, timeoutMs, budgetMs, refreshAfterMs, now, sleep, cache, failures,
}) {
  const unknown = [];          // keys no board has a value for, oldest first
  const stale = [];            // known keys whose value is due for renewal
  const pending = new Map();   // key -> { promise, resolve }: one request per key
  let draining = false;

  // The only place a lookup leaves the process: one at a time, spaced to perSecond.
  async function drain() {
    if (draining) return;
    draining = true;
    try {
      while (unknown.length || stale.length) {
        const key = unknown.length ? unknown.shift() : stale.shift();
        const started = now();
        let answer = null;
        const deadline = deadlineSignal(timeoutMs);
        try {
          const value = await fetchValue(key, deadline.signal);
          cache.set(key, { value, at: now() });
          answer = { value };
        } catch {
          failures.set(key, true);
        } finally {
          deadline.cleanup();
        }
        pending.get(key)?.resolve(answer);
        pending.delete(key);
        const gap = 1000 / perSecond - (now() - started);
        if (gap > 0 && (unknown.length || stale.length)) await sleep(gap);
      }
    } finally {
      draining = false;
    }
  }

  function enqueue(key, queue) {
    if (!pending.has(key)) {
      let resolve;
      const promise = new Promise((done) => { resolve = done; });
      pending.set(key, { promise, resolve });
      queue.push(key);
      drain();
    }
    return pending.get(key).promise;
  }

  return async function lookup(keys) {
    const values = new Map();
    const waits = [];
    for (const key of new Set(keys)) {
      const known = cache.get(key);
      if (known) {
        values.set(key, known.value);
        if (now() - known.at >= refreshAfterMs && failures.get(key) === undefined) enqueue(key, stale);
      } else if (failures.get(key) === undefined) {
        waits.push(enqueue(key, unknown).then((answer) => { if (answer) values.set(key, answer.value); }));
      }
    }
    if (waits.length) {
      let timer;
      const budget = new Promise((resolve) => { timer = setTimeout(resolve, budgetMs); });
      try {
        await Promise.race([Promise.all(waits), budget]);
      } finally {
        clearTimeout(timer);
      }
    }
    return new Map(values);
  };
}

const pause = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

/**
 * The UTC day of each wallet's most recent trade, which the leaderboard does
 * not carry. Only single-market TRADE rows count: redemptions, rebates and
 * merges keep arriving long after a wallet stops trading (on 2026-10-21 one
 * all-time leader had redeemed the day before but last traded eight weeks
 * earlier), and none of them is a trade anyone could copy. Combo trades are
 * left out as the wallet page's tape leaves them out, so both pages name the
 * same last trade. A wallet whose latest LAST_TRADE_ROWS trades are all
 * combos stays unknown.
 *
 * Paced as createPacedLookup describes: two a second is a tenth of the 200
 * per 10 s Polymarket publishes for /v2/activity. A wallet's day is kept for
 * a day; one older than `refreshAfterMs` is renewed behind the board (a last
 * trade only moves forward, so the cost is that a wallet resuming after 30
 * days is held back that much longer).
 */
export function createLastTradeLookup({
  fetchImpl = polymarketFetch,
  perSecond = 2,
  timeoutMs = 8_000,
  budgetMs = 6_000,
  refreshAfterMs = 30 * 60_000,
  now = Date.now,
  sleep = pause,
  cache = new TtlLruCache({ max: 4096, ttlMs: 24 * 60 * 60_000, now }),
  failures = new TtlLruCache({ max: 4096, ttlMs: 2 * 60_000, now }),
} = {}) {
  async function fetchDay(wallet, signal) {
    // start=1 reaches past v2's default floor of three years back.
    const query = new URLSearchParams({ user: wallet, type: 'TRADE', limit: String(LAST_TRADE_ROWS), start: '1' });
    const response = await fetchImpl(`${ACTIVITY}?${query}`, { headers: HEADERS, signal, background: true });
    if (!response.ok) throw new Error(`Polymarket activity ${response.status}`);
    const page = await response.json();
    if (!Array.isArray(page?.data)) throw new Error('Polymarket activity returned an invalid payload');
    // The rows decide: only a single-market TRADE row counts as a trade.
    const seconds = Number(page.data.find((row) => row?.type === 'TRADE' && !row.is_combo)?.timestamp);
    return Number.isFinite(seconds) && seconds > 0 ? new Date(seconds * 1000).toISOString().slice(0, 10) : null;
  }
  return createPacedLookup({
    fetchValue: fetchDay, perSecond, timeoutMs, budgetMs, refreshAfterMs, now, sleep, cache, failures,
  });
}

/**
 * Each board wallet's USD volume in the board's window: `volume_usdc` from
 * /v2/user-volume. The leaderboard's own `volume` counts shares, which run
 * above dollars by one over the price paid, so the board's volume column,
 * volume sort, ROI and volume floors all read this figure.
 *
 * /v2/user-volume counts whole UTC days, flooring `start` and `end` to their
 * day. Asked for the seven or thirty days up to now, it covers the window the
 * week and month boards rank: on 2026-11-04 its share `volume` equalled the
 * leaderboard row's for both. The all-time board sends no bounds.
 *
 * One request per wallet and window, about 520 for the three boards on a
 * cold start, at two a second as createPacedLookup describes. A volume is
 * kept six hours and renewed behind the board after thirty minutes; a
 * renewal that fails keeps the volume already known. A wallet whose volume
 * was never read stays unknown, and the board leaves it out of every view
 * ranked or floored by volume until it arrives.
 */
export function createUsdVolumeLookup({
  fetchImpl = polymarketFetch,
  perSecond = 2,
  timeoutMs = 8_000,
  budgetMs = 6_000,
  refreshAfterMs = 30 * 60_000,
  now = Date.now,
  sleep = pause,
  cache = new TtlLruCache({ max: 4096, ttlMs: 6 * 60 * 60_000, now }),
  failures = new TtlLruCache({ max: 4096, ttlMs: 2 * 60_000, now }),
} = {}) {
  async function fetchUsd(key, signal) {
    const [period, wallet] = key.split(':');
    const query = new URLSearchParams({ user: wallet });
    const days = WINDOW_DAYS[period];
    if (days) {
      const end = Math.floor(now() / 1000);
      query.set('start', String(end - days * 86_400));
      query.set('end', String(end));
    }
    const response = await fetchImpl(`${USER_VOLUME}?${query}`, { headers: HEADERS, signal, background: true });
    if (!response.ok) throw new Error(`Polymarket user volume ${response.status}`);
    const usd = finite((await response.json())?.data?.volume_usdc);
    if (usd == null) throw new Error('Polymarket user volume returned an invalid payload');
    return usd;
  }
  const lookup = createPacedLookup({
    fetchValue: fetchUsd, perSecond, timeoutMs, budgetMs, refreshAfterMs, now, sleep, cache, failures,
  });
  return async function usdVolumes(wallets, period) {
    periodToLeaderboardWindow(period);
    const found = await lookup(wallets.map((wallet) => `${period}:${wallet}`));
    return new Map([...found].map(([key, usd]) => [key.slice(period.length + 1), usd]));
  };
}
