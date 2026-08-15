export const PERIODS = ['7d', '30d', '90d']
export const SORTS = [
  ['pnl', 'PNL'],
  ['winrate', 'WIN RATE'],
  ['volume', 'VOLUME'],
]

export const DEFAULT_PERIOD = '30d'
export const DEFAULT_SORT = 'pnl'
export const DEFAULT_FILTERS = Object.freeze({
  pnlMin: '',
  winrateMin: '',
  volumeMin: '',
  consistencyRatioMin: '',
  fillExitMin: '',
  fillExitMax: '',
})

const periodDays = (period) => Number.parseInt(period, 10)

/** Return a finite number for an active numeric filter, otherwise null. */
export function finiteFilterNumber(value) {
  if (value == null || (typeof value === 'string' && value.trim() === '')) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function addFinite(query, key, value, transform = (number) => number) {
  const number = finiteFilterNumber(value)
  if (number !== null) query[key] = transform(number)
}

/** Build the API query without React or browser state for deterministic tests. */
export function buildScreenerQuery({
  period = DEFAULT_PERIOD,
  sort = DEFAULT_SORT,
  search = '',
  includePartialHistory = true,
  filters = DEFAULT_FILTERS,
} = {}) {
  const query = { sort: `${sort}_${period}`, limit: 50 }
  const term = String(search ?? '').trim()
  if (term) query.search = term

  addFinite(query, `pnl_${period}_min`, filters.pnlMin)
  addFinite(query, `winrate_${period}_min`, filters.winrateMin, (number) => number / 100)
  addFinite(query, `volume_${period}_min`, filters.volumeMin)
  addFinite(query, `consistency_ratio_${period}_min`, filters.consistencyRatioMin, (number) => number / 100)
  addFinite(query, `fill_exit_ratio_${period}_min`, filters.fillExitMin)
  addFinite(query, `fill_exit_ratio_${period}_max`, filters.fillExitMax)
  if (!includePartialHistory) query.history_days_min = periodDays(period)
  return query
}

/** Return [state key, truthful display label] tuples for individually clearable chips. */
export function activeFilterChips({
  period = DEFAULT_PERIOD,
  includePartialHistory = true,
  filters = DEFAULT_FILTERS,
} = {}) {
  const labelPeriod = period.toUpperCase()
  const chips = []
  const addChip = (key, labelFor) => {
    const number = finiteFilterNumber(filters[key])
    if (number !== null) chips.push([key, labelFor(number)])
  }
  addChip('pnlMin', (number) => `PNL ${labelPeriod} ≥ $${number}`)
  addChip('winrateMin', (number) => `WIN RATE ${labelPeriod} ≥ ${number}%`)
  addChip('volumeMin', (number) => `VOLUME ${labelPeriod} ≥ $${number}`)
  addChip('consistencyRatioMin', (number) => `POSITIVE CLOSE-DAY RATIO ${labelPeriod} ≥ ${number}%`)
  addChip('fillExitMin', (number) => `SELL / BUY EVENT COUNT ${labelPeriod} ≥ ${number}%`)
  addChip('fillExitMax', (number) => `SELL / BUY EVENT COUNT ${labelPeriod} ≤ ${number}%`)
  if (!includePartialHistory) {
    chips.push(['includePartialHistory', `FETCHED TRADE HISTORY ≥ ${periodDays(period)}D`])
  }
  return chips
}
