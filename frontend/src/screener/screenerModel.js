/* Pure model for the standalone Wallet Screener.
 *
 * No React and no browser globals, so every rule below is directly testable:
 * how a query is built, how an absent metric is rendered, and how partial
 * history is described. The rules matter more than the rendering — this
 * surface publishes other people's money, and overstating what we know about
 * it is the failure mode to avoid.
 */

export const PERIODS = ['7d', '30d', '90d']
export const SORTS = [
  ['pnl', 'PnL'],
  ['winrate', 'Win rate'],
  ['volume', 'Volume'],
]
/** Which table column each server-side sort corresponds to, so a header click
 *  and the sidebar control drive the same state instead of two orderings. */
export const COLUMN_SORT = { pnl: 'pnl', winRate: 'winrate', volume: 'volume' }
export const DEFAULT_PERIOD = '30d'
export const DEFAULT_SORT = 'pnl'
const DEFAULT_LIMIT = 50

export const DEFAULT_FILTERS = Object.freeze({
  pnlMin: '',
  winrateMin: '',
  volumeMin: '',
  consistencyRatioMin: '',
  fillExitMin: '',
  fillExitMax: '',
})

const BOT_URL = 'https://t.me/cpolytrade_bot'
const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

export const isAddress = (value) => ADDRESS_RE.test(String(value ?? '').trim())

/** A filter is active only when it parses to a finite number. Blank stays
 *  blank; it never becomes a zero threshold that quietly hides wallets. */
function finiteFilter(value) {
  if (value == null || String(value).trim() === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function filterValidationError(query = {}) {
  const minimum = finiteFilter(query.fill_exit_ratio_min)
  const maximum = finiteFilter(query.fill_exit_ratio_max)
  if (minimum !== null && maximum !== null && minimum > maximum) {
    return 'Minimum sell / buy event count cannot exceed the maximum.'
  }
  return ''
}

export function buildPublicQuery({
  period = DEFAULT_PERIOD,
  sort = DEFAULT_SORT,
  search = '',
  filters = DEFAULT_FILTERS,
  completeHistoryOnly = false,
  limit = DEFAULT_LIMIT,
  offset = 0,
} = {}) {
  // Fail loudly rather than falling back: a silently-substituted period would
  // label one window's numbers with another window's heading.
  if (!PERIODS.includes(period)) throw new Error(`unsupported period: ${period}`)
  if (!SORTS.some(([key]) => key === sort)) throw new Error(`unsupported sort: ${sort}`)

  const query = { period, sort, limit }
  if (offset) query.offset = offset

  const term = String(search ?? '').trim()
  if (term) query.search = term

  const pnl = finiteFilter(filters.pnlMin)
  if (pnl !== null) query.pnl_min = pnl
  const winrate = finiteFilter(filters.winrateMin)
  if (winrate !== null) query.winrate_min = winrate / 100
  const volume = finiteFilter(filters.volumeMin)
  if (volume !== null) query.volume_min = volume
  // Positive close-day ratio is a percentage in the UI and a 0..1 fraction on
  // the wire, the same split win rate uses.
  const consistency = finiteFilter(filters.consistencyRatioMin)
  if (consistency !== null) query.consistency_ratio_min = consistency / 100
  // Sell/buy event count is already a percentage on both sides, so it travels
  // unscaled — 100 means one SELL row per BUY row.
  const fillExitMin = finiteFilter(filters.fillExitMin)
  if (fillExitMin !== null) query.fill_exit_ratio_min = fillExitMin
  const fillExitMax = finiteFilter(filters.fillExitMax)
  if (fillExitMax !== null) query.fill_exit_ratio_max = fillExitMax
  if (completeHistoryOnly) query.complete_history_only = true

  const validationError = filterValidationError(query)
  if (validationError) throw new Error(validationError)
  return query
}

export function paginationLabel({ offset = 0, count = 0, total = 0 } = {}) {
  if (!total) return 'No wallets'
  if (!count) return `No wallets on this page — ${total} wallets match`
  return `Showing ${offset + 1}–${offset + count} of ${total} wallets`
}

export function activeFilterChips({ filters = DEFAULT_FILTERS, period = DEFAULT_PERIOD,
  completeHistoryOnly = false } = {}) {
  const label = period.toUpperCase()
  const chips = []
  const add = (key, format) => {
    const number = finiteFilter(filters[key])
    if (number !== null) chips.push([key, format(number)])
  }
  add('pnlMin', (n) => `PnL ${label} ≥ $${n}`)
  add('winrateMin', (n) => `Win rate ${label} ≥ ${n}%`)
  add('volumeMin', (n) => `Volume ${label} ≥ $${n}`)
  add('consistencyRatioMin', (n) => `Positive close-day ratio ${label} ≥ ${n}%`)
  add('fillExitMin', (n) => `Sell / buy event count ${label} ≥ ${n}%`)
  add('fillExitMax', (n) => `Sell / buy event count ${label} ≤ ${n}%`)
  if (completeHistoryOnly) chips.push(['completeHistoryOnly', `Fetched history ≥ ${parseInt(period, 10)}D`])
  return chips
}

export const CURVES = [
  ['cumulative', 'Cumulative', 'area'],
  ['daily', 'Daily', 'bar'],
  ['drawdown', 'Drawdown', 'area'],
]

/**
 * Turn the daily realized-PnL series into the curve the reader picked.
 *
 * Cumulative and drawdown are both running figures, so they are derived here
 * rather than in the component: the rule is what matters and it should be
 * testable without a browser. Drawdown is distance below the running peak, so
 * it is always <= 0 and reads as "how far down from the best it had been".
 */
export function curveFrom(dailyPnl, kind = 'cumulative') {
  if (!Array.isArray(dailyPnl) || dailyPnl.length === 0) return []
  if (kind === 'daily') {
    return dailyPnl.map((d) => ({ date: d.date, value: Number(d.pnl) || 0 }))
  }
  let running = 0
  let peak = 0
  return dailyPnl.map((d) => {
    running += Number(d.pnl) || 0
    peak = Math.max(peak, running)
    return { date: d.date, value: kind === 'drawdown' ? running - peak : running }
  })
}

/** Day outcomes over the window: how often it went up, down, or nowhere. */
export function dayOutcomes(dailyPnl) {
  if (!Array.isArray(dailyPnl) || dailyPnl.length === 0) return null
  let positive = 0
  let negative = 0
  let flat = 0
  let total = 0
  for (const d of dailyPnl) {
    const v = Number(d.pnl) || 0
    total += v
    if (v > 0) positive += 1
    else if (v < 0) negative += 1
    else flat += 1
  }
  const moved = positive + negative
  return {
    positive,
    negative,
    flat,
    days: dailyPnl.length,
    // Averaged over days that actually closed something. Dividing by every
    // calendar day would quietly understate a wallet that trades in bursts.
    avgMovingDay: moved ? total / moved : null,
    best: Math.max(...dailyPnl.map((d) => Number(d.pnl) || 0)),
    worst: Math.min(...dailyPnl.map((d) => Number(d.pnl) || 0)),
  }
}

const UNAVAILABLE = '—'

/** Render a metric, or say it is unavailable. A null is not a zero: the
 *  wallet may simply not have been recomputed yet, and printing 0 there would
 *  assert something we do not know. */
export function formatMetric(value, kind = 'money') {
  if (value == null || !Number.isFinite(Number(value))) return UNAVAILABLE
  const number = Number(value)
  if (kind === 'percent') return `${Math.round(number * 100)}%`
  // Already a percentage in the cache (SELL rows / BUY rows * 100).
  if (kind === 'percentValue') return `${Math.round(number)}%`
  if (kind === 'count') return String(Math.round(number))
  const sign = number < 0 ? '-' : ''
  const magnitude = Math.abs(number)
  const body = magnitude === 0
    ? '0'
    : magnitude < 100
      ? magnitude.toFixed(2)
      : Math.round(magnitude).toLocaleString('en-US')
  return `${sign}$${body}`
}

/** Describe how much fetched TRADE history backs the selected period. This
 *  describes that source only — it makes no claim about any other coverage. */
export function coverageLabel(wallet) {
  const days = wallet?.history_days
  if (days == null || !Number.isFinite(Number(days))) return 'UNAVAILABLE'
  const period = Number(wallet.period_days)
  if (wallet.history_partial) {
    return `PARTIAL · ~${Math.round(Number(days))}D OF ${period}D`
  }
  return `~${period}D OBSERVED`
}

/** Flatten an API payload into rows for the table. Deliberately produces no
 *  aggregate: there is no "copyability score" here, because the inputs are
 *  partial by construction and one number would conceal that. */
export function walletRows(payload) {
  const periodDays = payload?.period_days ?? null
  return (payload?.wallets ?? []).map((wallet) => {
    const withPeriod = { ...wallet, period_days: wallet.period_days ?? periodDays }
    return {
      address: wallet.address,
      displayName: wallet.display_name || null,
      xUsername: wallet.x_username || null,
      verified: Boolean(wallet.verified),
      pnl: wallet.pnl ?? null,
      winRate: wallet.win_rate ?? null,
      volume: wallet.volume ?? null,
      activePositions: wallet.active_positions ?? null,
      consistencyRatio: wallet.consistency_ratio ?? null,
      fillExitRatio: wallet.fill_exit_ratio ?? null,
      historyDays: wallet.history_days ?? null,
      historyPartial: Boolean(wallet.history_partial),
      coverage: coverageLabel(withPeriod),
      refreshedAt: wallet.stats_refreshed_at ?? null,
      periodDays: withPeriod.period_days,
      // Ordered {date, pnl} for the selected window, or null when the wallet
      // has not been computed yet. Null and [] mean different things here.
      dailyPnl: Array.isArray(wallet.daily_pnl) ? wallet.daily_pnl : null,
    }
  })
}

/* The bot does not consume wallet deep-link payloads. Keep the handoff honest:
 * opening Telegram never implies that the selected address travels with it. */
export function botDeepLink() {
  return BOT_URL
}

export const POLYMARKET_PROFILE = (address) =>
  (isAddress(address) ? `https://polymarket.com/profile/${String(address).toLowerCase()}` : null)


// --- shareable state --------------------------------------------------------
// A research surface whose views cannot be linked is one people screenshot.
// Only non-defaults are written, so a bare /screener is always the default
// board and a shared link states exactly what it changed.

const STATE_DEFAULTS = { period: DEFAULT_PERIOD, sort: DEFAULT_SORT, search: '', completeHistoryOnly: false }

export function encodeScreenerState({ period, sort, search, filters, completeHistoryOnly }) {
  const q = new URLSearchParams()
  if (period !== STATE_DEFAULTS.period) q.set('period', period)
  if (sort !== STATE_DEFAULTS.sort) q.set('sort', sort)
  if (search?.trim()) q.set('q', search.trim())
  if (completeHistoryOnly) q.set('complete', '1')
  for (const [key, value] of Object.entries(filters || {})) {
    if (value !== '' && value != null) q.set(key, String(value))
  }
  return q.toString()
}

export function decodeScreenerState(search) {
  const q = new URLSearchParams(search || '')
  const out = { ...STATE_DEFAULTS, filters: { ...DEFAULT_FILTERS } }
  const period = q.get('period')
  if (PERIODS.includes(period)) out.period = period
  const sort = q.get('sort')
  if (SORTS.some(([k]) => k === sort)) out.sort = sort
  if (q.has('q')) out.search = q.get('q')
  if (q.get('complete') === '1') out.completeHistoryOnly = true
  for (const key of Object.keys(DEFAULT_FILTERS)) {
    if (q.has(key)) out.filters[key] = q.get(key)
  }
  return out
}

/** The table exactly as filtered and ordered. Absent exports empty, never 0. */
export function walletsToCsv(rows, period) {
  const cell = (v) => {
    if (v == null || (typeof v === 'number' && !Number.isFinite(v))) return ''
    const s = String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const header = ['address', 'display_name', 'period', 'pnl_usd', 'win_rate',
                  'volume_usd', 'active_positions', 'positive_close_day_ratio',
                  'sell_buy_event_pct', 'history_days', 'history_partial', 'refreshed_at']
  return [header.join(','), ...rows.map((r) => [
    r.address, r.displayName, period, r.pnl, r.winRate, r.volume, r.activePositions,
    r.consistencyRatio, r.fillExitRatio, r.historyDays, r.historyPartial ? 'yes' : 'no',
    r.refreshedAt,
  ].map(cell).join(','))].join('\n')
}
