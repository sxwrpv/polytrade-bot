import { deadlineSignal } from './polymarket.mjs';
import { createCoalescer, TtlLruCache } from './serviceGuards.mjs';

const WINDOWS = { d7: 'WEEK', d30: 'MONTH', all: 'ALL' };
const LEADERBOARD = 'https://data-api.polymarket.com/v1/leaderboard';
const ACTIVITY = 'https://data-api.polymarket.com/activity';
const HEADERS = { accept: 'application/json', 'user-agent': 'polytrade-screener/1.0' };
/* Polymarket serves the leaderboard 50 rows at a time: a larger `limit` is cut
 * to 50 without an error, which is how the board quietly held the top 50 of
 * each list instead of the top 100. The top 100 is two pages. */
const PAGE = 50;
const LIMIT = 100;
const OFFSETS = Array.from({ length: LIMIT / PAGE }, (_, i) => i * PAGE);

export function periodToLeaderboardWindow(period) {
  const window = WINDOWS[period];
  if (!window) throw new Error(`unsupported period: ${period}`);
  return window;
}

const addressOf = (row) => String(row?.proxyWallet || '').toLowerCase();
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
    const vol = finite(row.vol);
    return {
      w,
      name: row.userName || null,
      img: row.profileImage || null,
      xUsername: row.xUsername || null,
      verified: Boolean(row.verifiedBadge),
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
 * @param {(wallets: string[], o: { signal?: AbortSignal }) => Promise<Map>} [o.lastTradeDays]
 *   the server's cached createLastTradeLookup; without it every last trade
 *   stays unknown.
 */
export async function fetchLiveUniverse({
  period, snapshot, fetchImpl = fetch, fetchedAt = new Date().toISOString(), signal, lastTradeDays,
}) {
  const timePeriod = periodToLeaderboardWindow(period);
  const getPage = async (orderBy, offset) => {
    const query = new URLSearchParams({ timePeriod, orderBy, limit: String(PAGE), offset: String(offset) });
    const response = await fetchImpl(`${LEADERBOARD}?${query}`, { headers: HEADERS, signal });
    if (!response.ok) throw new Error(`Polymarket leaderboard ${response.status}`);
    const rows = await response.json();
    if (!Array.isArray(rows)) throw new Error('Polymarket leaderboard returned an invalid payload');
    return rows;
  };
  // Every page or none: a board missing ranks 51-100 of one list would look
  // complete while it is not.
  const getRows = async (orderBy) => (await Promise.all(OFFSETS.map((offset) => getPage(orderBy, offset)))).flat();
  const [pnlRows, volumeRows] = await Promise.all([getRows('PNL'), getRows('VOL')]);
  const wallets = [...new Set([...pnlRows, ...volumeRows].map(addressOf).filter(isWallet))];
  const lastTrades = lastTradeDays ? await lastTradeDays(wallets, { signal }) : undefined;
  return buildLiveUniverse({ period, pnlRows, volumeRows, snapshot, fetchedAt, lastTrades });
}

/**
 * The UTC day of each wallet's most recent trade, which the leaderboard does
 * not carry. Only TRADE rows count: redemptions, rebates and merges keep
 * arriving long after a wallet stops trading (on 2026-10-21 one all-time
 * leader had redeemed the day before but last traded eight weeks earlier),
 * and none of them is a trade anyone could copy.
 *
 * Built once per server. Days are cached per wallet for half an hour, which
 * is plenty for a 30-day rule; concurrent boards share one request per
 * wallet; at most `concurrency` requests run at once. A failed lookup is left
 * out of the result rather than guessed, and that wallet is not asked again
 * for two minutes. The board waits at most `budgetMs` for lookups; slower ones
 * finish in the background and land in the cache for the next refresh.
 */
export function createLastTradeLookup({
  fetchImpl = fetch,
  concurrency = 8,
  timeoutMs = 8_000,
  budgetMs = 6_000,
  cache = new TtlLruCache({ max: 2048, ttlMs: 30 * 60_000 }),
  failures = new TtlLruCache({ max: 2048, ttlMs: 2 * 60_000 }),
} = {}) {
  const inflight = createCoalescer();
  const lastTradeDay = (wallet, signal) => inflight.run(wallet, async () => {
    const deadline = deadlineSignal(timeoutMs, signal);
    try {
      const query = new URLSearchParams({ user: wallet, type: 'TRADE', limit: '1' });
      const response = await fetchImpl(`${ACTIVITY}?${query}`, { headers: HEADERS, signal: deadline.signal });
      if (!response.ok) throw new Error(`Polymarket activity ${response.status}`);
      const rows = await response.json();
      if (!Array.isArray(rows)) throw new Error('Polymarket activity returned an invalid payload');
      // Trust the row, not the filter: anything that is not a trade counts as no trade.
      const seconds = Number(rows.find((row) => row?.type === 'TRADE')?.timestamp);
      const day = Number.isFinite(seconds) && seconds > 0 ? new Date(seconds * 1000).toISOString().slice(0, 10) : null;
      cache.set(wallet, day);
      return day;
    } finally {
      deadline.cleanup();
    }
  });

  return async function lastTradeDays(wallets, { signal } = {}) {
    const days = new Map();
    const queue = [];
    for (const wallet of new Set(wallets)) {
      const hit = cache.get(wallet);
      if (hit !== undefined) days.set(wallet, hit);
      else if (failures.get(wallet) === undefined) queue.push(wallet);
    }
    const worker = async () => {
      while (queue.length && !signal?.aborted) {
        const wallet = queue.shift();
        try {
          days.set(wallet, await lastTradeDay(wallet, signal));
        } catch {
          if (!signal?.aborted) failures.set(wallet, true);
        }
      }
    };
    const work = Promise.all(Array.from({ length: Math.min(concurrency, queue.length) }, worker));
    let timer;
    const budget = new Promise((resolve) => { timer = setTimeout(resolve, budgetMs); });
    try {
      await Promise.race([work, budget]);
    } finally {
      clearTimeout(timer);
    }
    return new Map(days);
  };
}
