const PERIOD_DAYS = Object.freeze({ '7d': 7, '30d': 30, '90d': 90 })
const DAY_MS = 86_400_000
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/

const fieldValue = (record, key) => record?.[key] == null ? null : record[key]
const normalizeAddress = (value) => typeof value === 'string' ? value.trim().toLowerCase() : ''

export function selectCurrentEvidence(address, trader, fetchedResult) {
  const requestedAddress = normalizeAddress(address)
  const fetchedAddress = normalizeAddress(fetchedResult?.address)

  if (requestedAddress && fetchedAddress === requestedAddress && fetchedResult?.data) {
    return fetchedResult.data
  }
  return trader || {}
}

function periodDetails(selectedPeriod) {
  const period = Object.hasOwn(PERIOD_DAYS, selectedPeriod) ? selectedPeriod : '30d'
  return { period, days: PERIOD_DAYS[period] }
}

function isUtcDay(value) {
  if (!ISO_DAY.test(value)) return false
  const parsed = new Date(`${value}T00:00:00.000Z`)
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
}

export function analysisMetrics(record, selectedPeriod) {
  const { period, days } = periodDetails(selectedPeriod)
  const historyDays = fieldValue(record, 'history_days')

  return {
    period,
    days,
    realizedPnl: fieldValue(record, `pnl_${period}`),
    winRate: fieldValue(record, `winrate_${period}`),
    grossVolume: fieldValue(record, `volume_${period}`),
    historyDays,
    partial: historyDays != null && historyDays < days,
    refreshedAt: fieldValue(record, 'stats_refreshed_at'),
  }
}

// The backend retains daily close buckets on or after the UTC date N days ago.
// Mirror that inclusive date-label contract. It can contain N+1 labels because
// the first and last labels are partial rolling-window days.
export function sliceDailyPnl(raw, selectedPeriod, refreshedAt) {
  if (raw == null) return null

  let parsed = raw
  if (typeof raw === 'string') {
    try {
      parsed = JSON.parse(raw)
    } catch {
      return null
    }
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') return null

  if (typeof refreshedAt !== 'string' || !refreshedAt.trim()) return null
  const anchor = new Date(refreshedAt)
  if (Number.isNaN(anchor.getTime())) return null
  const { days } = periodDetails(selectedPeriod)
  const cutoff = new Date(anchor.getTime() - days * DAY_MS).toISOString().slice(0, 10)
  const through = anchor.toISOString().slice(0, 10)

  return Object.entries(parsed)
    .filter(([date, pnl]) => isUtcDay(date)
      && date >= cutoff
      && date <= through
      && pnl != null
      && Number.isFinite(Number(pnl)))
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([date, pnl]) => ({ date, pnl: Number(pnl) }))
}

export function positionPreview(rows, limit = 8) {
  if (!Array.isArray(rows)) {
    return {
      items: [],
      total: null,
      liveCount: null,
      redeemableCount: null,
      label: 'FETCHED HOLDINGS UNAVAILABLE',
    }
  }

  const holdings = rows
    .filter((position) => Number(position?.size) > 0)
    .slice()
    .sort((left, right) => Number(Boolean(left.redeemable)) - Number(Boolean(right.redeemable))
      || (Number.isFinite(Number(right.current_value)) ? Number(right.current_value) : -Infinity)
        - (Number.isFinite(Number(left.current_value)) ? Number(left.current_value) : -Infinity))
  const safeLimit = Math.max(0, Number.isFinite(Number(limit)) ? Math.floor(Number(limit)) : 8)
  const items = holdings.slice(0, safeLimit)
  const total = holdings.length
  const liveCount = holdings.filter((position) => !position.redeemable).length
  const redeemableCount = total - liveCount

  return {
    items,
    total,
    liveCount,
    redeemableCount,
    label: items.length < total
      ? `SHOWING ${items.length} OF ${total} FETCHED HOLDINGS`
      : `${total} FETCHED HOLDINGS`,
  }
}
