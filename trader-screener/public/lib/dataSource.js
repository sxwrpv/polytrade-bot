/* Where the board's rows come from.
 *
 * Two sources, one row shape. Today the board reads a cached Polycopy cohort
 * snapshot, because that is where the Copy Score signal lives. Tomorrow it
 * reads PolyTrade's own `/api/public/screener/*`, which publishes truthful
 * per-window metrics but deliberately no composite score.
 *
 * The adapter below is the whole integration surface: point `SOURCE` at
 * 'polytrade', set `API_BASE`, and the same board renders PolyTrade's cache.
 * Anything the PolyTrade cache does not know stays null — a missing metric is
 * never rendered as a zero.
 */

/** Same contract as polytrade/frontend/src/screener/publicApi.js. */
const API_BASE = globalThis.__API_BASE__ || '/screener/api';

/** 'live' — current Polymarket leaderboard. 'snapshot' and 'polytrade' are explicit fallbacks. */
export const SOURCE = globalThis.__SCREENER_SOURCE__ || 'live';

async function get(path, params = {}) {
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === '' || v == null) continue;
    query.set(k, String(v));
  }
  const suffix = query.toString();
  const res = await fetch(`${API_BASE}${path}${suffix ? `?${suffix}` : ''}`, {
    credentials: 'omit',
    headers: { Accept: 'application/json' },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(body.detail || res.statusText);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export const api = {
  dataset: () => get('/dataset'),
  smi: () => get('/smi'),
  search: (q) => get('/search', { q }),
  trader: (address) => get(`/trader/${address}`),
  // PolyTrade-compatible surface, served locally by this app and by the
  // FastAPI router once this moves in-tree.
  wallets: (params) => get('/public/screener/wallets', params),
  wallet: (address, params) => get(`/public/screener/wallets/${address}`, params),
  provenance: () => get('/public/screener/provenance'),
  liveLeaderboard: (period) => get('/live/leaderboard', { period }),
};

const PERIOD_TO_POLYTRADE = { d7: '7d', d30: '30d' };

/**
 * Map one PolyTrade `wallets` row onto the board's row shape.
 *
 * PolyTrade publishes no composite score by design, so `copyClass` and
 * `copyNet` come back null and the board falls back to ordering by money. The
 * fields it does publish that the Polycopy cohort lacks — coverage, refresh
 * time, exit ratio — ride along so the table can show them honestly.
 */
export function fromPolytradeWallet(row, period = 'd30') {
  const days = row.period_days ?? null;
  return {
    w: String(row.address).toLowerCase(),
    name: row.display_name || null,
    img: null,
    followers: null,
    pnl: row.pnl ?? null,
    d7: period === 'd7' ? row.pnl ?? null : null,
    d30: period === 'd30' ? row.pnl ?? null : null,
    winRate: row.win_rate ?? null,
    vol: row.volume ?? null,
    d7Vol: period === 'd7' ? row.volume ?? null : null,
    d30Vol: period === 'd30' ? row.volume ?? null : null,
    openVal: null,
    openPositions: row.active_positions ?? null,
    // No composite score: PolyTrade's inputs are partial by construction and a
    // single number would conceal that.
    copyClass: null,
    copyNet: null,
    mm: null, arb: null, freq: null,
    cats: [],
    lastTradeDay: null,
    fills: null,
    activeDays: row.history_days ?? null,
    historyPartial: row.history_partial !== false,
    historyCoverage: row.history_coverage || 'unknown',
    periodDays: days,
    consistencyRatio: row.consistency_ratio ?? null,
    fillExitRatio: row.fill_exit_ratio ?? null,
    refreshedAt: row.stats_refreshed_at ?? null,
    verified: Boolean(row.verified),
    xUsername: row.x_username ?? null,
    maker: null, hold: null, weeksUp: null, vola: null, avgSize: null, maxDD: null, niche: null,
  };
}

/** Query params for PolyTrade's `/wallets`, from this board's control state. */
export function toPolytradeQuery({ period = 'd30', metric = 'pnl', search = '', limit = 50, offset = 0 } = {}) {
  if (!(period in PERIOD_TO_POLYTRADE)) throw new Error('PolyTrade API supports only d7 and d30');
  const q = {
    period: PERIOD_TO_POLYTRADE[period],
    sort: metric === 'roi' || metric === 'copy' ? 'pnl' : metric === 'vol' ? 'volume' : 'pnl',
    limit,
  };
  if (offset) q.offset = offset;
  if (search.trim()) q.search = search.trim();
  return q;
}

/** Load the board's universe from whichever source is configured. */
export async function loadUniverse({ period = 'd30' } = {}) {
  if (SOURCE === 'live') return api.liveLeaderboard(period);
  if (SOURCE === 'polytrade') {
    const payload = await api.wallets(toPolytradeQuery({ period, limit: 100 }));
    return {
      meta: {
        generatedAt: payload.wallets?.[0]?.stats_refreshed_at ?? new Date().toISOString(),
        windowAnchor: null,
        source: 'polytrade',
      },
      traders: (payload.wallets ?? []).map((r) => fromPolytradeWallet(r, period)),
      walletMeta: {}, spark: {}, wow: {}, copyDelta: {}, angles: [], events: [],
      groups: [], structures: [], wowAnchor: null,
    };
  }
  const ds = await api.dataset();
  ds.meta.source = 'snapshot';
  return ds;
}
