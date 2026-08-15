const PERIOD_DAYS = Object.freeze({ '7d': 7, '30d': 30, '90d': 90 })

const available = (value) => (value == null ? null : value)

export function formatMoney(value, { signed = false } = {}) {
  if (value == null) return '—'

  const amount = Number(value)
  if (!Number.isFinite(amount)) return '—'

  const sign = amount < 0 ? '-' : signed ? '+' : ''
  const magnitude = Math.abs(amount)
  const readableAmount = magnitude === 0
    ? '0'
    : magnitude < 100
      ? magnitude.toFixed(2)
      : Math.round(magnitude).toLocaleString('en-US')

  return `${sign}$${readableAmount}`
}

export function formatActivePositions(value) {
  if (value == null) return '—'
  return String(value)
}

export function discoveryMetrics(trader, selectedPeriod) {
  const period = Object.hasOwn(PERIOD_DAYS, selectedPeriod) ? selectedPeriod : '30d'
  const days = PERIOD_DAYS[period]
  const historyDays = available(trader.history_days)
  const refreshedAt = available(trader.stats_refreshed_at)

  return {
    period,
    days,
    realizedPnl: available(trader[`pnl_${period}`]),
    winRate: available(trader[`winrate_${period}`]),
    grossVolume: available(trader[`volume_${period}`]),
    activePositions: refreshedAt == null ? null : available(trader.open_positions),
    historyDays,
    partial: historyDays != null && historyDays < days,
    refreshedAt,
  }
}
