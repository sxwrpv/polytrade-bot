/* Pure rules for the Copy Score overlay.
 *
 * No React and no browser globals, so every rule below is directly testable:
 * how a band is cut, how the cohort board is ordered, and how a score is
 * rendered when it is absent.
 *
 * WHOSE NUMBER THIS IS
 *
 * Copy Score is not PolyTrade's measurement. It comes from Polycopy's public
 * discover dataset, it needs a full-chain indexer PolyTrade does not run, and
 * it covers a cohort — roughly 3,700 wallets — that is a subset of PolyTrade's
 * cache. `screenerModel.js` and the public API keep their promise that
 * PolyTrade publishes no composite score of its own; this module is the
 * separately-provenanced overlay, and every surface that shows a score also
 * shows whose it is and when it was generated.
 *
 * Constants recovered from Polycopy's shipped client are marked VERBATIM;
 * constants fitted from its published dataset are marked INFERRED and are
 * stated as estimates wherever they reach the screen.
 */

/* The cohort's own windows. Deliberately NOT the live board's 7d/30d/90d:
 * `all` is a lifetime total, and labelling it "90D" would put one window's
 * figures under another window's heading. The board relabels instead of
 * pretending the two vocabularies match. */
export const COHORT_PERIODS = ['d7', 'd30', 'all']
export const COHORT_PERIOD_LABEL = { d7: '7D', d30: '30D', all: 'ALL' }

/** Live period → nearest cohort window. 90d has no cohort equivalent, so it
 *  resolves to the lifetime board and the UI says ALL, never 90D. */
export const toCohortPeriod = (period) =>
  (period === '7d' ? 'd7' : period === '30d' ? 'd30' : 'all')
/** The inverse, for carrying a window back to the live board. `all` has no
 *  live equivalent; 90d is the widest window the cache publishes. */
export const toLivePeriod = (cohortPeriod) =>
  (cohortPeriod === 'd7' ? '7d' : cohortPeriod === 'd30' ? '30d' : '90d')

/** VERBATIM. Minimum window volume before a rate-based order is meaningful. */
export const MIN_VOLUME = { d7: 25_000, d30: 60_000, all: 100_000 }

/** VERBATIM. Lifetime totals never decay, so the all-time board hides wallets
 *  that have not traded in 30 days: it answers who you could copy today. */
export const STALE_DAYS = 30

// --- the taxonomy -----------------------------------------------------------
// VERBATIM: classes, chip labels and the one-line reading of each.

export const CLASS_DEF = {
  strong: { chip: 'Proven', line: 'Copying them has worked on their finished trades.', tone: 'clear' },
  marginal: { chip: 'Ahead', line: 'They finish ahead of what copying costs — but only just.', tone: 'ahead' },
  uneconomic: { chip: 'Caution', line: "They make money. It doesn't survive being copied.", tone: 'negative' },
  loss_making: { chip: 'Losing', line: "They've been losing on their own trades, before copying costs even start.", tone: 'negative' },
  not_measurable: { chip: 'Not comparable', line: 'They close out early, so we can warn about them but never rank them alongside hold-to-the-end traders.', tone: 'neutral' },
  unproven: { chip: 'Thin history', line: 'Too few finished trades to stand behind. A big return either way can still be one bet that came in.', tone: 'unknown' },
}

export const NONE_DEF = {
  chip: 'Not scored',
  line: 'The cohort behind this score does not cover this wallet. That is not a middling score; it is the absence of one.',
  tone: 'unknown',
}

export const CLASS_ORDER = ['strong', 'marginal', 'uneconomic', 'loss_making', 'not_measurable', 'unproven', 'none']

/** VERBATIM. The board's default filter: only bands that clear copy costs. */
export const RECOMMENDED = new Set(['strong', 'marginal'])
const NUMERIC_CLASSES = new Set(['strong', 'marginal', 'uneconomic', 'loss_making'])

/** VERBATIM. Cohort baseline, shown as "typical trader −2.0%". */
export const TYPICAL_TRADER_NET = -2.05

/** INFERRED from the published dataset: strong starts at +10, marginal at 0. */
export const CLASS_CUTS = { strong: 10, marginal: 0 }

export const knownClass = (key) => Boolean(key) && key in CLASS_DEF
export const normClass = (key) => (knownClass(key) ? key : 'none')
export const classDef = (key) => (knownClass(key) ? CLASS_DEF[key] : NONE_DEF)
export const classRank = (key) => {
  const index = CLASS_ORDER.indexOf(normClass(key))
  return index < 0 ? CLASS_ORDER.length : index
}
export const scoreIsNumeric = (key, value) =>
  knownClass(key) && NUMERIC_CLASSES.has(key) && value != null && Number.isFinite(value)

/** Signed integer form: +6, −4, 0. Used wherever the score stands alone. */
export function scoreSigned(value) {
  if (value == null || !Number.isFinite(value)) return null
  const n = Math.round(value)
  return n === 0 ? '0' : `${n > 0 ? '+' : '−'}${Math.abs(n)}`
}

/** Magnitude-percent form: 11%, −4%. Used inside the board's score column. */
export function scoreMagnitude(value) {
  if (value == null || !Number.isFinite(value)) return null
  const n = Math.round(Math.abs(value))
  return `${value < 0 && n !== 0 ? '−' : ''}${n}%`
}

// --- window accessors -------------------------------------------------------

export const pnlIn = (t, period) => (period === 'd7' ? t.d7 : period === 'd30' ? t.d30 : t.pnl)
export const volumeIn = (t, period) => (period === 'd7' ? t.d7Vol : period === 'd30' ? t.d30Vol : t.vol)

/** ROI is undefined without volume; a zero-volume wallet is not a 0% wallet. */
export function roiIn(t, period) {
  const volume = volumeIn(t, period)
  return volume > 0 ? (pnlIn(t, period) / volume) * 100 : null
}

export const bandOf = (t) => normClass(t.copyClass)

export const isHardToMirror = (t) =>
  t.mm === 'market_maker' || t.arb === 'arb' || t.freq === 'vhft'

export function tradedRecently(trader, asOf) {
  if (!asOf || !trader.lastTradeDay) return true
  const cut = new Date(`${asOf}T00:00:00Z`)
  cut.setUTCDate(cut.getUTCDate() - STALE_DAYS)
  return trader.lastTradeDay >= cut.toISOString().slice(0, 10)
}

export const inCategory = (rows, category) =>
  (category === 'all' ? rows : rows.filter((t) => t.cats?.includes(category)))

// --- cohort filters ---------------------------------------------------------
// Same vocabulary and the same blank-stays-blank contract as the live board's
// filters in screenerModel.js, so the two rails read as one set of controls.

export const COHORT_FILTERS = Object.freeze({
  roiMin: '',
  copyNetMin: '',
  activeDaysMin: '',
  avgSizeMax: '',
  excludeHardToMirror: false,
})

export function finiteFilter(value) {
  if (value == null || String(value).trim() === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

/**
 * Predicate for one cohort row against the active filters.
 *
 * Every comparison is written so that an ABSENT metric fails a filter that
 * asks about it, rather than passing by accident. A wallet with no win rate is
 * not a 0% wallet, so it must not survive "win rate ≥ 40%".
 */
export function passesCohortFilters(t, filters = {}, period = 'd30') {
  const atLeast = (value, key) => {
    const min = finiteFilter(filters[key])
    if (min === null) return true
    return value != null && Number.isFinite(value) && value >= min
  }
  const atMost = (value, key) => {
    const max = finiteFilter(filters[key])
    if (max === null) return true
    return value != null && Number.isFinite(value) && value <= max
  }

  if (!atLeast(pnlIn(t, period), 'pnlMin')) return false
  if (!atLeast(volumeIn(t, period), 'volumeMin')) return false
  if (!atLeast(roiIn(t, period), 'roiMin')) return false
  // Win rate is a percentage in the rail and a 0..1 fraction on the row, the
  // same split the live query builder uses.
  if (!atLeast(t.winRate == null ? null : t.winRate * 100, 'winrateMin')) return false
  if (!atLeast(t.copyNet, 'copyNetMin')) return false
  if (!atLeast(t.activeDays, 'activeDaysMin')) return false
  // A ceiling, not a floor: a wallet whose average fill is $220K is one you
  // cannot mirror at a retail budget however good its score.
  if (!atMost(t.avgSize, 'avgSizeMax')) return false
  // A wallet whose edge is the spread, both sides at once, or raw speed is one
  // a copy cannot follow, whatever its score says.
  if (filters.excludeHardToMirror && isHardToMirror(t)) return false
  return true
}

export function comparator(metric, period) {
  if (metric === 'roi') {
    return (a, b) => (roiIn(b, period) ?? -Infinity) - (roiIn(a, period) ?? -Infinity)
  }
  if (metric === 'volume') return (a, b) => volumeIn(b, period) - volumeIn(a, period)
  if (metric === 'winrate') return (a, b) => (b.winRate ?? -Infinity) - (a.winRate ?? -Infinity)
  return (a, b) => pnlIn(b, period) - pnlIn(a, period)
}

/**
 * The cohort board: filter, then order.
 *
 * Under Copy Score the band is the primary key and money is only the
 * tie-break — the score is what puts a wallet on the board and how high; the
 * money column beside it merely separates wallets that scored the same.
 */
export function buildCohortBoard(traders, {
  metric = 'copy', period = 'd30', category = 'all', asOf = null,
  bands = null, minVolume = MIN_VOLUME, filters = {}, direction = 'desc',
} = {}) {
  if (!COHORT_PERIODS.includes(period)) throw new Error(`unsupported period: ${period}`)

  let rows = inCategory(traders, category)
  if (period === 'all') rows = rows.filter((t) => tradedRecently(t, asOf))
  if (metric === 'copy' || metric === 'roi') {
    rows = rows.filter((t) => volumeIn(t, period) >= minVolume[period])
  }
  rows = rows.filter((t) => passesCohortFilters(t, filters, period))

  const flip = direction === 'asc' ? (cmp) => (a, b) => cmp(b, a) : (cmp) => cmp
  if (metric !== 'copy') return [...rows].sort(flip(comparator(metric, period)))

  const allow = bands ?? RECOMMENDED
  const money = flip(comparator('pnl', period))
  const byBand = direction === 'asc'
    ? (a, b) => classRank(b.copyClass) - classRank(a.copyClass)
    : (a, b) => classRank(a.copyClass) - classRank(b.copyClass)
  return rows
    .filter((t) => allow.has(bandOf(t)))
    .sort((a, b) => byBand(a, b) || money(a, b))
}

/** Consecutive rows sharing a band, so the table can print one header each. */
export function bandRuns(rows, metric) {
  if (metric !== 'copy') return []
  const runs = []
  rows.forEach((t, index) => {
    const band = bandOf(t)
    if (index === 0 || bandOf(rows[index - 1]) !== band) {
      runs.push({ index, band, total: rows.filter((row) => bandOf(row) === band).length })
    }
  })
  return runs
}

export function staleHeldBack(traders, { category = 'all', period, asOf }) {
  if (period !== 'all') return 0
  return inCategory(traders, category).filter((t) => !tradedRecently(t, asOf)).length
}

// --- formatting -------------------------------------------------------------

const UNAVAILABLE = '—'

/** VERBATIM money formatting: $368K, −$38K, $1.7M. */
export function money(value) {
  if (value == null || !Number.isFinite(value)) return UNAVAILABLE
  const magnitude = Math.abs(value)
  const body = magnitude >= 1e6 ? `${(magnitude / 1e6).toFixed(1)}M`
    : magnitude >= 1e3 ? `${Math.round(magnitude / 1e3)}K`
      : Math.round(magnitude).toLocaleString('en-US')
  return `${value < 0 ? '−$' : '$'}${body}`
}

export function signedMoney(value) {
  if (value == null || !Number.isFinite(value)) return UNAVAILABLE
  return `${value < 0 ? '−' : '+'}${money(Math.abs(value))}`
}

export function percent(value, digits = 0) {
  return value != null && Number.isFinite(value) ? `${(100 * value).toFixed(digits)}%` : UNAVAILABLE
}

export function signedPercent(value, digits = 1) {
  if (value == null || !Number.isFinite(value)) return UNAVAILABLE
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`
}

// --- provenance -------------------------------------------------------------

/** How stale the cohort behind the score is. Upstream regenerates daily, so
 *  past two days it is describing a board that has already moved. */
export function cohortAge(generatedAt, now = Date.now()) {
  if (!generatedAt) return { hours: null, label: 'age unknown', stale: true }
  const hours = (now - new Date(generatedAt)) / 3.6e6
  if (!Number.isFinite(hours)) return { hours: null, label: 'age unknown', stale: true }
  const label = hours < 1 ? 'under an hour old'
    : hours < 48 ? `${Math.round(hours)}h old`
      : `${Math.round(hours / 24)}d old`
  return { hours, label, stale: hours > 48 }
}

/** Chips describing what is narrowing the cohort board, including the cuts the
 *  ordering imposes. A threshold the reader cannot see the value behind
 *  asserts something they have no way to check. */
export function cohortFilterChips({ metric, period, category = 'all', bands, filters = {} }) {
  const chips = []
  const label = COHORT_PERIOD_LABEL[period]
  if (category !== 'all') chips.push({ key: 'category', text: `Category ${category}`, clearable: true })
  if (metric === 'copy' || metric === 'roi') {
    chips.push({ key: 'minVolume', text: `Volume ${label} ≥ ${money(MIN_VOLUME[period])}`, clearable: false })
  }
  if (metric === 'copy') {
    const shown = [...(bands ?? RECOMMENDED)].map((band) => classDef(band).chip).join(' or ')
    chips.push({ key: 'bands', text: `Copy Score ${shown}`, clearable: false })
  }
  if (period === 'all') {
    chips.push({ key: 'fresh', text: `Traded in the last ${STALE_DAYS}D`, clearable: false })
  }

  const add = (key, format) => {
    const number = finiteFilter(filters[key])
    if (number !== null) chips.push({ key, text: format(number), clearable: true })
  }
  add('roiMin', (v) => `ROI ${label} ≥ ${v}%`)
  add('copyNetMin', (v) => `Copy Score ≥ ${scoreSigned(v)}`)
  add('activeDaysMin', (v) => `Active days ≥ ${v}`)
  add('avgSizeMax', (v) => `Avg fill ≤ ${money(v)}`)
  if (filters.excludeHardToMirror) {
    chips.push({ key: 'excludeHardToMirror', text: 'Excluding hard to mirror', clearable: true })
  }
  return chips
}

// --- angle boards -----------------------------------------------------------

/** VERBATIM. Structure ordering used to spread board picks across bet types. */
const STRUCTURE_ORDER = { spread: 0, totals: 1, moneyline: 2, btts: 3, binary: 4, prop: 5 }

export const realAngles = (angles) => angles.filter((angle) => angle.kind !== 'niche')

/** "General culture" is the board's own label for the catch-all slice; drop the
 *  qualifier but keep it reading as a proper name rather than "culture". */
export function stripGeneral(label) {
  const out = String(label ?? '').replace(/^General /, '')
  return out === label ? out : out.charAt(0).toUpperCase() + out.slice(1)
}

/** VERBATIM. Orders boards by bet type first, then by the best result in each. */
export const angleWeight = (angle) =>
  1e12 * (angle.kind === 'bracket'
    ? (angle.dim === 'LOW' ? 1 : 2)
    : (STRUCTURE_ORDER[angle.dim] ?? 5)) - angle.topPnl

/** VERBATIM. One board per group, five in total, preferring unseen bet types. */
export function featuredAngles(angles, limit = 5) {
  const sorted = [...realAngles(angles)].sort((a, b) => angleWeight(a) - angleWeight(b))
  const picked = []
  const groups = new Set()
  const dims = new Set()
  for (const pass of [0, 1]) {
    for (const angle of sorted) {
      if (picked.length >= limit) break
      if (!groups.has(angle.group) && !(pass === 0 && dims.has(angle.dim))) {
        picked.push(angle)
        groups.add(angle.group)
        dims.add(angle.dim)
      }
    }
  }
  return picked
}

/** VERBATIM. Where a P&L lands inside a board's cohort distribution. */
export function percentileIn(angle, pnl) {
  const dist = angle?.pnlDist
  if (!dist?.length || pnl == null) return null
  const marks = [0, 10, 25, 50, 75, 90, 95, 99, 100]
  if (pnl <= dist[0]) return { pctl: 0, n: angle.cohort }
  for (let i = 1; i < dist.length; i += 1) {
    if (pnl <= dist[i]) {
      const fraction = dist[i] === dist[i - 1] ? 0 : (pnl - dist[i - 1]) / (dist[i] - dist[i - 1])
      return { pctl: Math.round(marks[i - 1] + fraction * (marks[i] - marks[i - 1])), n: angle.cohort }
    }
  }
  return { pctl: 100, n: angle.cohort }
}

// --- export -----------------------------------------------------------------

/** The cohort board exactly as shown. An absent metric exports as an empty
 *  cell, never a zero. */
export function cohortToCsv(rows, period) {
  const cell = (value) => {
    if (value == null || (typeof value === 'number' && !Number.isFinite(value))) return ''
    const text = String(value)
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
  }
  const header = [
    'rank', 'wallet', 'name', 'copy_class', 'copy_score', 'roi_pct', 'pnl_usd',
    'open_value_usd', 'volume_usd', 'win_rate', 'active_days', 'avg_fill_usd',
    'max_drawdown_usd', 'maker_ratio', 'niche', 'categories', 'last_trade_day',
    'hard_to_mirror',
  ]
  const lines = [header.join(',')]
  rows.forEach((t, index) => {
    const roi = roiIn(t, period)
    lines.push([
      index + 1, t.w, t.name, t.copyClass, t.copyNet,
      roi == null ? null : Number(roi.toFixed(2)),
      pnlIn(t, period), t.openVal, volumeIn(t, period), t.winRate, t.activeDays,
      t.avgSize, t.maxDD, t.maker, t.niche, (t.cats ?? []).join(' '),
      t.lastTradeDay, isHardToMirror(t) ? 'yes' : 'no',
    ].map(cell).join(','))
  })
  return lines.join('\n')
}
