/* Pure model for the trader screener.
 *
 * No DOM and no browser globals, so every rule below is directly testable:
 * which wallets are eligible, how a board is ordered, how bands are cut, and
 * how an absent metric is rendered.
 *
 * The selection, ordering and thresholds are reproduced from the Polycopy
 * discover client so the board can be checked against the live one row for
 * row. Constants that came out of the shipped bundle are marked VERBATIM;
 * constants recovered from the published dataset are marked INFERRED.
 */

export const PERIODS = ['d7', 'd30', 'all'];
export const PERIOD_LABEL = { d7: '7D', d30: '30D', all: 'ALL' };
export const SORTS = [
  ['copy', 'Copy Score'],
  ['pnl', 'PnL'],
  ['roi', 'ROI'],
  ['vol', 'Volume'],
];
export const DEFAULT_PERIOD = 'd7';
export const DEFAULT_SORT = 'copy';
export const SCREENER_BASE = '/screener';

/** Keep wallet pages inside the canonical production screener namespace. */
export const traderPath = (wallet) => `${SCREENER_BASE}/trader/${String(wallet).toLowerCase()}`;

/** VERBATIM. Minimum window volume before a rate-based order is meaningful. */
export const MIN_VOLUME = { d7: 25_000, d30: 60_000, all: 100_000 };

/** VERBATIM. Structure ordering used to spread board picks across bet types. */
const STRUCTURE_ORDER = { spread: 0, totals: 1, moneyline: 2, btts: 3, binary: 4, prop: 5 };

/** VERBATIM. Lifetime totals never decay, so the all-time board hides wallets
 *  that have not traded in 30 days: it answers who you could copy today. */
export const STALE_DAYS = 30;

// --- Copy Score taxonomy ----------------------------------------------------
// VERBATIM: classes, chip labels and the one-line reading of each.

export const CLASS_DEF = {
  strong: { chip: 'Proven', line: 'Copying them has worked on their finished trades.', tone: 'clear' },
  marginal: { chip: 'Ahead', line: 'They finish ahead of what copying costs — but only just.', tone: 'ahead' },
  uneconomic: { chip: 'Caution', line: "They make money. It doesn't survive being copied.", tone: 'negative' },
  loss_making: { chip: 'Losing', line: "They've been losing on their own trades, before copying costs even start.", tone: 'negative' },
  not_measurable: { chip: 'Not comparable', line: 'They close out early, so we can warn about them but never rank them alongside hold-to-the-end traders.', tone: 'neutral' },
  unproven: { chip: 'Thin history', line: 'Too few finished trades to stand behind. A big return either way can still be one bet that came in.', tone: 'unknown' },
};
export const NONE_DEF = {
  chip: 'Not scored',
  line: 'The classifier has not graded this wallet. That is not a middling score; it is the absence of one.',
  tone: 'unknown',
};
export const CLASS_ORDER = ['strong', 'marginal', 'uneconomic', 'loss_making', 'not_measurable', 'unproven', 'none'];
/** VERBATIM. The board's default filter: only bands that clear copy costs. */
export const RECOMMENDED = new Set(['strong', 'marginal']);
const NUMERIC_CLASSES = new Set(['strong', 'marginal', 'uneconomic', 'loss_making']);

/** VERBATIM. Cohort baseline, shown as "typical trader −2.0%". */
export const TYPICAL_TRADER_NET = -2.05;

/** INFERRED from the published dataset: strong starts at +10, marginal at 0. */
export const CLASS_CUTS = { strong: 10, marginal: 0 };

export const knownClass = (k) => !!k && k in CLASS_DEF;
export const normClass = (k) => (knownClass(k) ? k : 'none');
export const classDef = (k) => (knownClass(k) ? CLASS_DEF[k] : NONE_DEF);
export const classRank = (k) => {
  const i = CLASS_ORDER.indexOf(normClass(k));
  return i < 0 ? CLASS_ORDER.length : i;
};
export const scoreIsNumeric = (k, v) => knownClass(k) && NUMERIC_CLASSES.has(k) && v != null && Number.isFinite(v);

/** Signed integer form: +6, −4, 0. Used wherever the score stands alone. */
export function scoreSigned(value) {
  if (value == null || !Number.isFinite(value)) return null;
  const n = Math.round(value);
  return n === 0 ? '0' : `${n > 0 ? '+' : '−'}${Math.abs(n)}`;
}
/** Magnitude-percent form: 11%, −4%. Used inside the board's score column. */
export function scoreMagnitude(value) {
  if (value == null || !Number.isFinite(value)) return null;
  const n = Math.round(Math.abs(value));
  return `${value < 0 && n !== 0 ? '−' : ''}${n}%`;
}

/**
 * The upstream SMI changed `narrative` from a string to a structured object.
 * Pick its human-readable headline deliberately instead of allowing DOM text
 * coercion to leak "[object Object]" into the board.
 */
export function smartMoneyNarrative(index) {
  const narrative = index?.narrative;
  if (typeof narrative === 'string') return narrative;
  if (narrative && typeof narrative === 'object') {
    for (const candidate of [narrative.hero, narrative.take, ...(narrative.bullets || [])]) {
      if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
    }
  }
  return typeof index?.summary === 'string' ? index.summary : '';
}

// --- window accessors -------------------------------------------------------

export const pnlIn = (t, period) => (period === 'd7' ? t.d7 : period === 'd30' ? t.d30 : t.pnl);
export const volumeIn = (t, period) => (period === 'd7' ? t.d7Vol : period === 'd30' ? t.d30Vol : t.vol);
/** ROI is undefined without volume; a zero-volume wallet is not a 0% wallet. */
export function roiIn(t, period) {
  const v = volumeIn(t, period);
  return v > 0 ? (pnlIn(t, period) / v) * 100 : null;
}
export const bandOf = (t) => normClass(t.copyClass);

export function tradedRecently(trader, asOf) {
  if (!asOf || !trader.lastTradeDay) return true;
  const cut = new Date(`${asOf}T00:00:00Z`);
  cut.setUTCDate(cut.getUTCDate() - STALE_DAYS);
  return trader.lastTradeDay >= cut.toISOString().slice(0, 10);
}

export const inCategory = (rows, cat) => (cat === 'all' ? rows : rows.filter((t) => t.cats?.includes(cat)));

export function comparator(metric, period) {
  if (metric === 'roi') return (a, b) => (roiIn(b, period) ?? -Infinity) - (roiIn(a, period) ?? -Infinity);
  if (metric === 'vol') return (a, b) => volumeIn(b, period) - volumeIn(a, period);
  return (a, b) => pnlIn(b, period) - pnlIn(a, period);
}

/**
 * The board: filter, then order.
 *
 * Under Copy Score the band is the primary key and money is only the
 * tie-break — the score is what puts a trader on the board and how high; the
 * money column beside it merely separates traders who scored the same.
 */
export function buildBoard(traders, {
  metric = DEFAULT_SORT, period = DEFAULT_PERIOD, cat = 'all', asOf = null,
  bands = null, minVolume = MIN_VOLUME, filters = DEFAULT_FILTERS, direction = 'desc',
} = {}) {
  if (!PERIODS.includes(period)) throw new Error(`unsupported period: ${period}`);
  if (!SORTS.some(([key]) => key === metric)) throw new Error(`unsupported sort: ${metric}`);

  let rows = inCategory(traders, cat);
  if (period === 'all') rows = rows.filter((t) => tradedRecently(t, asOf));
  if (metric === 'copy' || metric === 'roi') rows = rows.filter((t) => volumeIn(t, period) >= minVolume[period]);
  rows = rows.filter((t) => passesFilters(t, filters, period));
  const flip = direction === 'asc' ? (cmp) => (a, b) => cmp(b, a) : (cmp) => cmp;
  if (metric !== 'copy') return [...rows].sort(flip(comparator(metric, period)));

  const allow = bands ?? RECOMMENDED;
  const money = flip(comparator('pnl', period));
  const byBand = direction === 'asc'
    ? (a, b) => classRank(b.copyClass) - classRank(a.copyClass)
    : (a, b) => classRank(a.copyClass) - classRank(b.copyClass);
  return rows
    .filter((t) => allow.has(bandOf(t)))
    .sort((a, b) => byBand(a, b) || money(a, b));
}

// --- numeric filters --------------------------------------------------------
// Same semantics as polytrade's screener model: a filter is active only when it
// parses to a finite number. Blank stays blank; it never becomes a zero
// threshold that quietly hides wallets.

export const DEFAULT_FILTERS = Object.freeze({
  pnlMin: '',
  volumeMin: '',
  roiMin: '',
  winrateMin: '',
  copyNetMin: '',
  activeDaysMin: '',
  avgSizeMax: '',
  excludeHardToMirror: false,
});

export function finiteFilter(value) {
  if (value == null || String(value).trim() === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

/**
 * Predicate for one wallet against the active filters.
 *
 * Every comparison is written so that an ABSENT metric fails a filter that
 * asks about it, rather than passing by accident. A wallet with no win rate is
 * not a 0% wallet, so it must not survive "win rate >= 40%".
 */
export function passesFilters(t, filters = DEFAULT_FILTERS, period = DEFAULT_PERIOD) {
  const atLeast = (value, key) => {
    const min = finiteFilter(filters[key]);
    if (min === null) return true;
    return value != null && Number.isFinite(value) && value >= min;
  };
  const atMost = (value, key) => {
    const max = finiteFilter(filters[key]);
    if (max === null) return true;
    return value != null && Number.isFinite(value) && value <= max;
  };

  if (!atLeast(pnlIn(t, period), 'pnlMin')) return false;
  if (!atLeast(volumeIn(t, period), 'volumeMin')) return false;
  if (!atLeast(roiIn(t, period), 'roiMin')) return false;
  // Win rate is a percentage in the UI and a 0..1 fraction on the row, the same
  // split polytrade's query builder uses.
  if (!atLeast(t.winRate, 'winrateMin')) return false;
  if (!atLeast(t.copyNet, 'copyNetMin')) return false;
  if (!atLeast(t.activeDays, 'activeDaysMin')) return false;
  if (!atMost(t.avgSize, 'avgSizeMax')) return false;
  // A wallet whose edge is the spread, both sides at once, or raw speed is one
  // a copy cannot follow, whatever its score says.
  if (filters.excludeHardToMirror && isHardToMirror(t)) return false;
  return true;
}

export const isHardToMirror = (t) =>
  t.mm === 'market_maker' || t.arb === 'arb' || t.freq === 'vhft';

/** Rows for the Saved view keep the same ordering rules, no eligibility cut. */
export function buildSaved(traders, savedWallets, {
  metric = DEFAULT_SORT, period = DEFAULT_PERIOD,
  filters = DEFAULT_FILTERS, direction = 'desc',
} = {}) {
  // No eligibility cut here — you asked to watch these wallets, so a thin
  // window or a bad band does not remove one. The numeric filters still apply:
  // those are the reader narrowing their own list on purpose.
  const rows = traders
    .filter((t) => savedWallets.has(t.w.toLowerCase()))
    .filter((t) => passesFilters(t, filters, period));
  const flip = direction === 'asc' ? (cmp) => (a, b) => cmp(b, a) : (cmp) => cmp;
  const money = flip(comparator(metric === 'copy' ? 'pnl' : metric, period));
  if (metric !== 'copy') return rows.sort(money);
  const byBand = direction === 'asc'
    ? (a, b) => classRank(b.copyClass) - classRank(a.copyClass)
    : (a, b) => classRank(a.copyClass) - classRank(b.copyClass);
  return rows.sort((a, b) => byBand(a, b) || money(a, b));
}

/** Consecutive rows sharing a band, so the table can print one header each. */
export function bandRuns(rows, metric) {
  if (metric !== 'copy') return [];
  const runs = [];
  rows.forEach((t, i) => {
    const band = bandOf(t);
    if (i === 0 || bandOf(rows[i - 1]) !== band) {
      runs.push({ index: i, band, total: rows.filter((r) => bandOf(r) === band).length });
    }
  });
  return runs;
}

export function staleHeldBack(traders, { cat = 'all', period, asOf }) {
  if (period !== 'all') return 0;
  return inCategory(traders, cat).filter((t) => !tradedRecently(t, asOf)).length;
}

// --- angle boards -----------------------------------------------------------

export const realAngles = (angles) => angles.filter((a) => a.kind !== 'niche');
/** "General culture" is the board's own label for the catch-all slice; drop the
 *  qualifier but keep it reading as a proper name rather than "culture". */
export function stripGeneral(label) {
  const out = String(label ?? '').replace(/^General /, '');
  return out === label ? out : out.charAt(0).toUpperCase() + out.slice(1);
}

/** VERBATIM. Orders boards by bet type first, then by the best result in each. */
export const angleWeight = (a) =>
  1e12 * (a.kind === 'bracket' ? (a.dim === 'LOW' ? 1 : 2) : (STRUCTURE_ORDER[a.dim] ?? 5)) - a.topPnl;

/** VERBATIM. Round-robin across groups and bet types so one cannot dominate. */
export function spreadAngles(angles, limit = Infinity) {
  const sorted = [...angles].sort((a, b) => angleWeight(a) - angleWeight(b));
  const picked = [];
  const used = new Set(), byGroup = new Map(), byDim = new Map();
  while (picked.length < Math.min(limit, sorted.length)) {
    let best = -1, bestScore = Infinity;
    for (let i = 0; i < sorted.length; i++) {
      if (used.has(i)) continue;
      const a = sorted[i];
      const score = (byGroup.get(a.group) ?? 0) * 1e6 + (byDim.get(a.dim) ?? 0) * 1e3 + i / sorted.length;
      if (score < bestScore) { bestScore = score; best = i; }
    }
    if (best < 0) break;
    const a = sorted[best];
    used.add(best); picked.push(a);
    byGroup.set(a.group, (byGroup.get(a.group) ?? 0) + 1);
    byDim.set(a.dim, (byDim.get(a.dim) ?? 0) + 1);
  }
  return picked;
}

/** VERBATIM. One board per group, five in total, preferring unseen bet types. */
export function featuredAngles(angles, limit = 5) {
  const sorted = [...realAngles(angles)].sort((a, b) => angleWeight(a) - angleWeight(b));
  const picked = [];
  const groups = new Set(), dims = new Set();
  for (const pass of [0, 1]) {
    for (const a of sorted) {
      if (picked.length >= limit) break;
      if (!groups.has(a.group) && !(pass === 0 && dims.has(a.dim))) {
        picked.push(a); groups.add(a.group); dims.add(a.dim);
      }
    }
  }
  return picked;
}

/** VERBATIM. Where a P&L lands inside a board's cohort distribution. */
export function percentileIn(angle, pnl) {
  const dist = angle?.pnlDist;
  if (!dist?.length || pnl == null) return null;
  const marks = [0, 10, 25, 50, 75, 90, 95, 99, 100];
  if (pnl <= dist[0]) return { pctl: 0, n: angle.cohort };
  for (let i = 1; i < dist.length; i++) {
    if (pnl <= dist[i]) {
      const f = dist[i] === dist[i - 1] ? 0 : (pnl - dist[i - 1]) / (dist[i] - dist[i - 1]);
      return { pctl: Math.round(marks[i - 1] + f * (marks[i] - marks[i - 1])), n: angle.cohort };
    }
  }
  return { pctl: 100, n: angle.cohort };
}

/** Traders who were already profitable and added more this week. VERBATIM. */
export function trendingTraders(dataset, limit = 4) {
  const byWallet = new Map(dataset.traders.map((t) => [t.w, t]));
  return Object.entries(dataset.wow ?? {})
    .map(([w, [last7, prev7]]) => ({ w, trader: byWallet.get(w), last7, prev7, diff: last7 - prev7 }))
    .filter((r) => r.trader && r.prev7 > 0 && r.diff > 0
      && (dataset.spark[r.w]?.length ?? 0) >= 3 && r.trader.copyClass === 'strong')
    .sort((a, b) => b.diff - a.diff)
    .slice(0, limit);
}

// --- formatting -------------------------------------------------------------

const UNAVAILABLE = '—';

/** VERBATIM money formatting: $368K, −$38K, $1.7M. */
export function money(value) {
  if (value == null || !Number.isFinite(value)) return UNAVAILABLE;
  const a = Math.abs(value);
  const body = a >= 1e6 ? `${(a / 1e6).toFixed(1)}M`
    : a >= 1e3 ? `${Math.round(a / 1e3)}K`
    : Math.round(a).toLocaleString('en-US');
  return `${value < 0 ? '−$' : '$'}${body}`;
}
export function signedMoney(value) {
  if (value == null || !Number.isFinite(value)) return UNAVAILABLE;
  return `${value < 0 ? '−' : '+'}${money(Math.abs(value))}`;
}
export function percent(value, digits = 0) {
  return value != null && Number.isFinite(value) ? `${(100 * value).toFixed(digits)}%` : UNAVAILABLE;
}
export function signedPercent(value, digits = 1) {
  if (value == null || !Number.isFinite(value)) return UNAVAILABLE;
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`;
}
export const shortAddress = (w) => `${w.slice(0, 6)}…${w.slice(-4)}`;

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;
export const isAddress = (v) => ADDRESS_RE.test(String(v ?? '').trim());
/** Accept a bare address or any URL that ends in one. */
export function resolveAddress(input) {
  const raw = String(input ?? '').trim();
  if (isAddress(raw)) return raw.toLowerCase();
  const m = /^(?:https?:\/\/)?[^\s]*\/(0x[0-9a-fA-F]{40})(?:[/?#][^\s]*)?$/.exec(raw);
  return m ? m[1].toLowerCase() : null;
}

/** How much of the selected window a wallet's tape actually backs. */
export function coverageLabel(trader, period) {
  const days = period === 'd7' ? 7 : period === 'd30' ? 30 : null;
  if (trader.activeDays == null) return 'UNAVAILABLE';
  if (days == null) return `${Number(trader.activeDays).toLocaleString('en-US')}D ACTIVE`;
  return trader.activeDays < days ? `PARTIAL · ${trader.activeDays}D OF ${days}D` : `~${days}D OBSERVED`;
}

/** Chips describing every filter currently narrowing the board, so a reader can
 *  see what is being hidden. A threshold you cannot see the value behind
 *  asserts something the reader has no way to check. */
export function activeFilterChips({ metric, period, cat, bands, filters = DEFAULT_FILTERS }) {
  const chips = [];
  const label = PERIOD_LABEL[period];
  if (cat !== 'all') chips.push({ key: 'cat', text: `Category ${cat}`, clearable: true });
  if (metric === 'copy' || metric === 'roi') {
    chips.push({ key: 'minVolume', text: `Volume ${label} \u2265 ${money(MIN_VOLUME[period])}`, clearable: false });
  }
  if (metric === 'copy') {
    const shown = [...(bands ?? RECOMMENDED)].map((b) => classDef(b).chip).join(' or ');
    chips.push({ key: 'bands', text: `Copy Score ${shown}`, clearable: false });
  }
  if (period === 'all') {
    chips.push({ key: 'fresh', text: `Traded in the last ${STALE_DAYS}D`, clearable: false });
  }

  const add = (key, format) => {
    const num = finiteFilter(filters[key]);
    if (num !== null) chips.push({ key, text: format(num), clearable: true });
  };
  add('pnlMin', (v) => `PnL ${label} \u2265 ${money(v)}`);
  add('volumeMin', (v) => `Volume ${label} \u2265 ${money(v)}`);
  add('roiMin', (v) => `ROI ${label} \u2265 ${v}%`);
  add('winrateMin', (v) => `Win rate \u2265 ${Math.round(v * 100)}%`);
  add('copyNetMin', (v) => `Copy Score \u2265 ${scoreSigned(v)}`);
  add('activeDaysMin', (v) => `Active days \u2265 ${v}`);
  add('avgSizeMax', (v) => `Avg fill \u2264 ${money(v)}`);
  if (filters.excludeHardToMirror) {
    chips.push({ key: 'excludeHardToMirror', text: 'Excluding hard to mirror', clearable: true });
  }
  return chips;
}

// --- shareable state --------------------------------------------------------
// A research surface whose views cannot be linked is a research surface people
// screenshot instead. Only non-default values are written, so a plain /?  is
// always the default board and a shared link says exactly what it changed.

const STATE_KEYS = ['view', 'metric', 'period', 'cat', 'direction'];

export function encodeState(state, defaults) {
  const q = new URLSearchParams();
  for (const key of STATE_KEYS) {
    if (state[key] != null && state[key] !== defaults[key]) q.set(key, String(state[key]));
  }
  // Bands only travel when they differ from the default recommendation.
  const bands = [...(state.bands ?? [])].sort();
  const fallback = [...RECOMMENDED].sort();
  if (bands.length && bands.join(',') !== fallback.join(',')) q.set('bands', bands.join(','));
  for (const [key, value] of Object.entries(state.filters ?? {})) {
    if (value === true) q.set(key, '1');
    else if (value !== '' && value != null && value !== false) q.set(key, String(value));
  }
  return q.toString();
}

export function decodeState(search, defaults) {
  const q = new URLSearchParams(search);
  const out = { filters: { ...DEFAULT_FILTERS } };
  const period = q.get('period');
  if (PERIODS.includes(period)) out.period = period;
  const metric = q.get('metric');
  if (SORTS.some(([k]) => k === metric)) out.metric = metric;
  const view = q.get('view');
  // 'following' was this view's name before it became Saved. Old links still
  // work; they just resolve to the current name.
  if (view === 'all' || view === 'saved') out.view = view;
  else if (view === 'following') out.view = 'saved';
  const direction = q.get('direction');
  if (direction === 'asc' || direction === 'desc') out.direction = direction;
  if (q.has('cat')) out.cat = q.get('cat');
  if (q.has('bands')) {
    const bands = q.get('bands').split(',').filter((b) => CLASS_ORDER.includes(b));
    if (bands.length) out.bands = new Set(bands);
  }
  for (const key of Object.keys(DEFAULT_FILTERS)) {
    if (!q.has(key)) continue;
    const raw = q.get(key);
    out.filters[key] = typeof DEFAULT_FILTERS[key] === 'boolean' ? raw === '1' : raw;
  }
  return { ...defaults, ...out };
}

// --- export -----------------------------------------------------------------

/** The board as it is currently shown, as CSV. Absent stays empty, never 0. */
export function toCsv(rows, period) {
  const cell = (v) => {
    if (v == null || (typeof v === 'number' && !Number.isFinite(v))) return '';
    const s = String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const header = [
    'rank', 'wallet', 'name', 'copy_class', 'copy_score', 'roi_pct', 'pnl_usd',
    'open_value_usd', 'volume_usd', 'win_rate', 'active_days', 'avg_fill_usd',
    'max_drawdown_usd', 'maker_ratio', 'niche', 'categories', 'last_trade_day',
    'hard_to_mirror',
  ];
  const lines = [header.join(',')];
  rows.forEach((t, i) => {
    lines.push([
      i + 1, t.w, t.name, t.copyClass, t.copyNet,
      roiIn(t, period) == null ? null : Number(roiIn(t, period).toFixed(2)),
      pnlIn(t, period), t.openVal, volumeIn(t, period), t.winRate, t.activeDays,
      t.avgSize, t.maxDD, t.maker, t.niche, (t.cats ?? []).join(' '),
      t.lastTradeDay, isHardToMirror(t) ? 'yes' : 'no',
    ].map(cell).join(','));
  });
  return lines.join('\n');
}

/** How stale the snapshot behind the board is. */
export function snapshotAge(generatedAt, now = Date.now()) {
  if (!generatedAt) return { hours: null, label: 'age unknown', stale: true };
  const hours = (now - new Date(generatedAt)) / 3.6e6;
  if (!Number.isFinite(hours)) return { hours: null, label: 'age unknown', stale: true };
  const label = hours < 1 ? 'under an hour old'
    : hours < 48 ? `${Math.round(hours)}h old`
    : `${Math.round(hours / 24)}d old`;
  // The cohort is regenerated daily upstream, so past two days it is describing
  // a board that has already moved.
  return { hours, label, stale: hours > 48 };
}

// --- PolyTrade hand-off -----------------------------------------------------

const BOT_URL = 'https://t.me/cpolytrade_bot';

/* Telegram deep linking is NOT enabled — nothing in the polytrade repository
 * reads a `start` payload, so emitting `?start=wallet_<address>` would produce
 * a link that silently drops the wallet. Flip this when the bot learns to
 * resolve one; the link below is already the right shape. */
export const SUPPORTS_WALLET_DEEP_LINK = false;

export function botDeepLink(address) {
  if (!SUPPORTS_WALLET_DEEP_LINK || !isAddress(address)) return BOT_URL;
  return `${BOT_URL}?start=wallet_${String(address).trim().toLowerCase()}`;
}
export const polymarketProfile = (address) =>
  (isAddress(address) ? `https://polymarket.com/profile/${String(address).toLowerCase()}` : null);
