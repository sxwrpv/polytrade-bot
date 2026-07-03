import { useEffect, useState, useCallback, useMemo } from 'react'
import { api } from '../api'
import PositionCard from '../components/PositionCard'

function summarize(rows, closed) {
  if (closed) {
    const realized = rows.map((r) => Number(r.realized_pnl || 0))
    const total = realized.reduce((a, b) => a + b, 0)
    const wins = realized.filter((v) => v > 0).length
    return [
      ['REALIZED PNL', `${total >= 0 ? '+' : ''}$${total.toFixed(2)}`, total >= 0 ? 'pos' : 'neg'],
      ['WIN RATE', realized.length ? `${Math.round((wins / realized.length) * 100)}%` : '—', ''],
      ['BEST', realized.length ? `+$${Math.max(...realized).toFixed(2)}` : '—', 'pos'],
      ['WORST', realized.length ? `$${Math.min(...realized).toFixed(2)}` : '—', 'neg'],
    ]
  }
  const exposure = rows.reduce((a, r) => a + Number(r.notional_usd || 0), 0)
  const unrealized = rows.reduce((a, r) => a + Number(r.unrealized_pnl || 0), 0)
  return [
    ['OPEN EXPOSURE', `$${exposure.toFixed(2)}`, ''],
    ['UNREALIZED PNL', `${unrealized >= 0 ? '+' : ''}$${unrealized.toFixed(2)}`, unrealized >= 0 ? 'pos' : 'neg'],
    ['OPEN POSITIONS', rows.length, ''],
  ]
}

export default function Positions() {
  const [tab, setTab] = useState('open')
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    setLoading(true)
    const fn = tab === 'open' ? api.openPositions : api.closedPositions
    fn()
      .then((r) => {
        setRows(r)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [tab])

  useEffect(() => {
    load()
  }, [load])

  // open positions move with the market — keep them fresh without manual reloads
  useEffect(() => {
    if (tab !== 'open') return
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [tab, load])

  const cells = useMemo(() => summarize(rows, tab === 'closed'), [rows, tab])

  return (
    <div>
      <div className="toggle-row">
        <button className={`chip ${tab === 'open' ? 'active' : ''}`} onClick={() => setTab('open')}>
          OPEN
        </button>
        <button className={`chip ${tab === 'closed' ? 'active' : ''}`} onClick={() => setTab('closed')}>
          CLOSED
        </button>
      </div>

      {!loading && rows.length > 0 && (
        <div className="stat-grid">
          {cells.map(([label, value, c]) => (
            <div className="stat-cell" key={label}>
              <div className="label">{label}</div>
              <div className={`value ${c}`}>{value}</div>
            </div>
          ))}
        </div>
      )}

      {loading ? (
        <div className="muted">loading…</div>
      ) : rows.length === 0 ? (
        <div className="muted">no {tab} positions</div>
      ) : (
        rows.map((r) => (
          <PositionCard key={r.id || r.token_id} p={r} closed={tab === 'closed'} onClose={load} />
        ))
      )}
    </div>
  )
}
