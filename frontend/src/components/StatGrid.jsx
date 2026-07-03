const fmt = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}$${Number(v).toFixed(2)}`)
const cls = (v) => (v == null ? '' : v >= 0 ? 'pos' : 'neg')

export default function StatGrid({ pnl }) {
  const p = pnl || {}
  const cells = [
    ['TOTAL PNL', fmt(p.total_pnl), cls(p.total_pnl)],
    ['WIN RATE', `${((p.win_rate || 0) * 100).toFixed(0)}%`, ''],
    ['TRADES', p.total_trades ?? 0, ''],
    ['PNL 7D', fmt(p.pnl_7d), cls(p.pnl_7d)],
    ['PNL 30D', fmt(p.pnl_30d), cls(p.pnl_30d)],
    ['BEST', fmt(p.best_trade), cls(p.best_trade)],
  ]
  return (
    <div className="stat-grid">
      {cells.map(([label, value, c]) => (
        <div className="stat-cell" key={label}>
          <div className="label">{label}</div>
          <div className={`value ${c}`}>{value}</div>
        </div>
      ))}
    </div>
  )
}
