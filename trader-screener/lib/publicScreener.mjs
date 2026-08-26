/* /api/public/screener/* — the PolyTrade-shaped read surface.
 *
 * Provides the subset of backend/api/routes_public_screener.py that this
 * snapshot can support truthfully. The adapter can point at either server;
 * fields this frozen source cannot prove stay null. Three promises hold here:
 *
 *   * it reads ONLY the precomputed snapshot — a request can never trigger an
 *     upstream fan-out or a cache write;
 *   * it projects an explicit field allowlist, so a field added to the
 *     snapshot later cannot start leaking through by accident;
 *   * it is rate limited per client address.
 *
 * The projection deliberately publishes no composite copyability score: the
 * PolyTrade contract does not have one, and inventing a field the real API
 * will not return would make this endpoint a lie about the shape.
 */

export const PERIODS = { '7d': 7, '30d': 30 };
export const SORTS = new Set(['pnl', 'winrate', 'volume']);

const RATE_LIMIT = 60;
const RATE_WINDOW_MS = 60_000;
const MAX_TRACKED_CLIENTS = 4096;
const buckets = new Map();

export function enforceRateLimit(client = 'unknown') {
  const now = Date.now();
  let bucket = buckets.get(client);
  if (!bucket) {
    if (buckets.size >= MAX_TRACKED_CLIENTS) {
      // Bound memory: drop the least recently seen bucket.
      let oldestKey = null, oldest = Infinity;
      for (const [k, v] of buckets) {
        const last = v.length ? v[v.length - 1] : 0;
        if (last < oldest) { oldest = last; oldestKey = k; }
      }
      buckets.delete(oldestKey);
    }
    bucket = [];
    buckets.set(client, bucket);
  }
  while (bucket.length && now - bucket[0] > RATE_WINDOW_MS) bucket.shift();
  if (bucket.length >= RATE_LIMIT) {
    const err = new Error('too many requests — the public screener is rate limited');
    err.status = 429;
    err.retryAfter = Math.ceil(RATE_WINDOW_MS / 1000);
    throw err;
  }
  bucket.push(now);
}
export const resetRateLimits = () => buckets.clear();

/** Keep an absent metric absent. A missing value is not a zero. */
const number = (v) => (v == null || !Number.isFinite(v) ? null : Number(v));

/** Map the snapshot's window keys onto the PolyTrade period keys. */
const WINDOW = { '7d': ['d7', 'd7Vol'], '30d': ['d30', 'd30Vol'] };

/** Explicit allowlist projection of one snapshot row. */
export function project(t, period) {
  if (!(period in PERIODS)) {
    const error = new Error('period must be one of 7d, 30d');
    error.status = 422;
    throw error;
  }
  const days = PERIODS[period];
  const [pnlKey, volKey] = WINDOW[period];
  return {
    address: t.w,
    display_name: t.name && !t.name.startsWith('0x') ? t.name : null,
    x_username: null,
    verified: false,
    period,
    period_days: days,
    pnl: number(t[pnlKey]),
    win_rate: number(t.winRate),
    volume: number(t[volKey]),
    active_positions: null,
    consistency_ratio: number(t.weeksUp),
    fill_exit_ratio: null,
    history_days: null,
    // Active-day frequency cannot prove the snapshot reaches the period start.
    history_partial: true,
    history_coverage: 'unknown',
    stats_refreshed_at: null,
    daily_pnl: null,
  };
}

const sortValue = (t, sort, period) => {
  const [pnlKey, volKey] = WINDOW[period];
  if (sort === 'volume') return t[volKey] ?? -Infinity;
  if (sort === 'winrate') return t.winRate ?? -Infinity;
  return t[pnlKey] ?? -Infinity;
};

export function queryWallets(traders, params) {
  const period = params.get('period') || '30d';
  const sort = params.get('sort') || 'pnl';
  if (!(period in PERIODS)) { const e = new Error('period must be one of 7d, 30d'); e.status = 422; throw e; }
  if (!SORTS.has(sort)) { const e = new Error('sort must be one of pnl, winrate, volume'); e.status = 422; throw e; }

  const integer = (name, fallback, floor, ceiling = Infinity) => {
    const raw = params.get(name);
    if (raw == null || raw === '') return fallback;
    const value = Number(raw);
    if (!Number.isInteger(value) || value < floor || value > ceiling) {
      const error = new Error(`${name} must be an integer from ${floor}${Number.isFinite(ceiling) ? ` to ${ceiling}` : ' upward'}`);
      error.status = 422;
      throw error;
    }
    return value;
  };
  const limit = integer('limit', 50, 1, 100);
  const offset = integer('offset', 0, 0);
  const rawSearch = params.get('search') || '';
  if (rawSearch.length > 64) { const e = new Error('search must be at most 64 characters'); e.status = 422; throw e; }
  const search = rawSearch.trim().toLowerCase();
  const [pnlKey, volKey] = WINDOW[period];

  const min = (name) => {
    const raw = params.get(name);
    if (raw == null || raw === '') return null;
    const n = Number(raw);
    if (!Number.isFinite(n)) { const e = new Error(`${name} must be a number`); e.status = 422; throw e; }
    return n;
  };
  const pnlMin = min('pnl_min');
  const winrateMin = min('winrate_min');
  const volumeMin = min('volume_min');
  const consistencyMin = min('consistency_ratio_min');
  const fillExitMin = min('fill_exit_ratio_min');
  const fillExitMax = min('fill_exit_ratio_max');
  if (consistencyMin != null && (consistencyMin < 0 || consistencyMin > 1)) {
    const e = new Error('consistency_ratio_min must be between 0 and 1'); e.status = 422; throw e;
  }
  if ((fillExitMin != null && fillExitMin < 0) || (fillExitMax != null && fillExitMax < 0)) {
    const e = new Error('fill exit ratios must be non-negative'); e.status = 422; throw e;
  }
  if (fillExitMin != null && fillExitMax != null && fillExitMin > fillExitMax) {
    const e = new Error('minimum sell / buy event count cannot exceed the maximum'); e.status = 422; throw e;
  }
  const completeOnly = params.get('complete_history_only') === 'true';

  let rows = traders;
  if (search) rows = rows.filter((t) => t.w.includes(search) || (t.name || '').toLowerCase().includes(search));
  if (pnlMin != null) rows = rows.filter((t) => (t[pnlKey] ?? -Infinity) >= pnlMin);
  if (winrateMin != null) rows = rows.filter((t) => (t.winRate ?? -Infinity) >= winrateMin);
  if (volumeMin != null) rows = rows.filter((t) => (t[volKey] ?? -Infinity) >= volumeMin);
  if (consistencyMin != null) rows = rows.filter((t) => (t.weeksUp ?? -Infinity) >= consistencyMin);
  // The source has no fill-exit values; a requested threshold must not let an
  // unknown value pass as though it had satisfied the filter.
  if (fillExitMin != null || fillExitMax != null) rows = [];
  // No row has proof of period reach-back in this snapshot.
  if (completeOnly) rows = [];

  const total = rows.length;
  const page = [...rows]
    .sort((a, b) => sortValue(b, sort, period) - sortValue(a, sort, period))
    .slice(offset, offset + limit)
    .map((t) => project(t, period));

  return {
    period, period_days: PERIODS[period], sort,
    count: page.length, total, limit, offset,
    has_more: offset + page.length < total,
    wallets: page,
    provenance: PROVENANCE,
  };
}

export const PROVENANCE = {
  source: 'Cached Polycopy public discover dataset; Polycopy attributes its derived wallet metrics to Polymarket data. Genuine 7-day and 30-day snapshot fields are used here.',
  limitations: [
    'The snapshot does not prove period reach-back, so coverage is unknown and every wallet is conservatively marked partial.',
    '90-day metrics are unavailable until genuine 90-day fields exist; lifetime values are never substituted.',
    'A missing metric is shown as unavailable, never as zero.',
    'Figures are a snapshot, refreshed periodically — they are not live.',
    'No composite copyability score is published on this endpoint: the inputs are partial by construction and a single number would hide that.',
    'Past wallet activity does not predict future results.',
  ],
};
