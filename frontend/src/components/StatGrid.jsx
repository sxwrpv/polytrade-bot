import { dollars } from '../money'

const fmt = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}$${dollars(Math.abs(Number(v)))}`)
const cls = (v) => (v == null ? '' : v >= 0 ? 'pos' : 'neg')

export default function StatGrid({ pnl }) {
  const loading = pnl == null
  const p = pnl || {}
  const cells = [
    ['Total P&L', fmt(p.total_pnl), cls(p.total_pnl)],
    ['Win rate', `${((p.win_rate || 0) * 100).toFixed(0)}%`, ''],
    ['Trades', p.total_trades ?? 0, ''],
    ['P&L 7D', fmt(p.pnl_7d), cls(p.pnl_7d)],
    ['P&L 30D', fmt(p.pnl_30d), cls(p.pnl_30d)],
    ['Best trade', fmt(p.best_trade), cls(p.best_trade)],
  ]
  return (
    <div className="stat-grid" aria-busy={loading || undefined}>
      {cells.map(([label, value, c]) => (
        <div className="stat-cell" key={label}>
          <div className="label">{label}</div>
          <div className={`value ${c}`} style={{ '--len': String(value).length }}>
            {loading ? <span className="sk sk-line" /> : value}
          </div>
        </div>
      ))}
    </div>
  )
}
