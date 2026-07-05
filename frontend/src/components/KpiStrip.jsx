// Persistent, non-collapsible glance strip — balance/PnL/open-count visible
// from HOME without a trip to USER. Sticks under the app header.
export default function KpiStrip({ me, pnl, followingCount }) {
  const bal = me?.balance
  const p7 = pnl?.pnl_7d
  const cls = (v) => (v == null ? '' : v >= 0 ? 'pos' : 'neg')
  const fmt = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '-'}$${Math.abs(Number(v)).toFixed(2)}`)

  return (
    <div className="kpi-strip stat-grid">
      <div className="stat-cell">
        <div className="label">BALANCE</div>
        <div className="value">{bal != null ? `$${bal.toFixed(2)}` : '—'}</div>
      </div>
      <div className="stat-cell">
        <div className="label">PNL 7D</div>
        <div className={`value ${cls(p7)}`}>{fmt(p7)}</div>
      </div>
      <div className="stat-cell">
        <div className="label">COPIED WALLETS</div>
        <div className="value">{followingCount ?? 0}</div>
      </div>
    </div>
  )
}
