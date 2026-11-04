import { deadlineSignal, polymarketFetch } from './polymarket.mjs';
import { TtlLruCache } from './serviceGuards.mjs';

const WINDOWS = { d7: 'week', d30: 'month', all: 'all' };
const LEADERBOARD = 'https://data-api.polymarket.com/v2/leaderboard';
const ACTIVITY = 'https://data-api.polymarket.com/v2/activity';
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
 *
 * Rows are /v2/leaderboard rows. Their `pnl` is USDC; their `volume` counts
 * shares bought and sold, the figure polymarket.com/leaderboard serves.
 */
export function buildLiveUniverse({ period, pnlRows = [], volumeRows = [], snapshot, fetchedAt, lastTrades }) {
  periodToLeaderboardWindow(period);
  const overlay = new Map((snapshot?.traders || []).map((row) => [String(row.w).toLowerCase(), row]));
  const live = new Map();
  for (const row of [...pnlRows, ...volumeRows]) {
    const w = addressOf(row);
    if (!isWallet(w)) continue;
    live.set(w, { ...(live.get(w) || {}), ...row });
  }

  const traders = [...live.entries()].map(([w, row]) => {
    const score = overlay.get(w) || {};
    const pnl = finite(row.pnl);
    const vol = finite(row.volume);
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
 */
export async function fetchLiveUniverse({
  period, snapshot, fetchImpl = polymarketFetch, fetchedAt = new Date().toISOString(), signal, lastTradeDays,
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
  const lastTrades = lastTradeDays ? await lastTradeDays(wallets) : undefined;
  return buildLiveUniverse({ period, pnlRows, volumeRows, snapshot, fetchedAt, lastTrades });
}

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
 * Polymarket limits requests per IP, and this service shares the server's IP
 * with the copy engine, which polls the same data API for the trades it
 * copies. So lookups go out one at a time and at most `perSecond` a second,
 * however many boards ask at once: two a second is a tenth of the 200 per 10 s
 * Polymarket publishes for /v2/activity, and they draw on the screener's
 * shared budget (polymarketGate) as background work, after any board or
 * wallet page that is waiting. A cold cache therefore fills over minutes.
 *
 * Built once per server. A wallet's day is kept for a day and served at once;
 * one older than `refreshAfterMs` is renewed behind the board (a last trade
 * only moves forward, so the cost is that a wallet resuming after 30 days is
 * held back that much longer), after every wallet nobody knows yet. A board
 * waits at most `budgetMs` for unknown wallets and the rest land in the cache
 * for the next refresh. A failed lookup is left out rather than guessed, and
 * that wallet is not asked again for two minutes.
 */
export function createLastTradeLookup({
  fetchImpl = polymarketFetch,
  perSecond = 2,
  timeoutMs = 8_000,
  budgetMs = 6_000,
  refreshAfterMs = 30 * 60_000,
  now = Date.now,
  sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); }),
  cache = new TtlLruCache({ max: 4096, ttlMs: 24 * 60 * 60_000, now }),
  failures = new TtlLruCache({ max: 4096, ttlMs: 2 * 60_000, now }),
} = {}) {
  const unknown = [];          // wallets no board has a day for, oldest first
  const stale = [];            // known wallets whose day is due for renewal
  const pending = new Map();   // wallet -> { promise, resolve }: one request per wallet
  let draining = false;

  async function fetchDay(wallet) {
    const deadline = deadlineSignal(timeoutMs);
    try {
      // start=1 reaches past v2's default floor of three years back.
      const query = new URLSearchParams({ user: wallet, type: 'TRADE', limit: String(LAST_TRADE_ROWS), start: '1' });
      const response = await fetchImpl(`${ACTIVITY}?${query}`, { headers: HEADERS, signal: deadline.signal, background: true });
      if (!response.ok) throw new Error(`Polymarket activity ${response.status}`);
      const page = await response.json();
      if (!Array.isArray(page?.data)) throw new Error('Polymarket activity returned an invalid payload');
      // The rows decide: only a single-market TRADE row counts as a trade.
      const seconds = Number(page.data.find((row) => row?.type === 'TRADE' && !row.is_combo)?.timestamp);
      return Number.isFinite(seconds) && seconds > 0 ? new Date(seconds * 1000).toISOString().slice(0, 10) : null;
    } finally {
      deadline.cleanup();
    }
  }

  // The only place a lookup leaves the process: one at a time, spaced to perSecond.
  async function drain() {
    if (draining) return;
    draining = true;
    try {
      while (unknown.length || stale.length) {
        const wallet = unknown.length ? unknown.shift() : stale.shift();
        const started = now();
        let answer = null;
        try {
          const day = await fetchDay(wallet);
          cache.set(wallet, { day, at: now() });
          answer = { day };
        } catch {
          failures.set(wallet, true);
        }
        pending.get(wallet)?.resolve(answer);
        pending.delete(wallet);
        const gap = 1000 / perSecond - (now() - started);
        if (gap > 0 && (unknown.length || stale.length)) await sleep(gap);
      }
    } finally {
      draining = false;
    }
  }

  function enqueue(wallet, queue) {
    if (!pending.has(wallet)) {
      let resolve;
      const promise = new Promise((done) => { resolve = done; });
      pending.set(wallet, { promise, resolve });
      queue.push(wallet);
      drain();
    }
    return pending.get(wallet).promise;
  }

  return async function lastTradeDays(wallets) {
    const days = new Map();
    const waits = [];
    for (const wallet of new Set(wallets)) {
      const known = cache.get(wallet);
      if (known) {
        days.set(wallet, known.day);
        if (now() - known.at >= refreshAfterMs && failures.get(wallet) === undefined) enqueue(wallet, stale);
      } else if (failures.get(wallet) === undefined) {
        waits.push(enqueue(wallet, unknown).then((answer) => { if (answer) days.set(wallet, answer.day); }));
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
    return new Map(days);
  };
}
