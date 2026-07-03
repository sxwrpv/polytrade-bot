import { useEffect, useState } from 'react'
import { api } from '../api'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')
const money = (v) => `${(v || 0) >= 0 ? '+' : '-'}$${Math.abs(v || 0).toFixed(2)}`

const VERB = {
  open: 'OPENED',
  close: 'CLOSED',
  partial: 'RESIZED',
  resolve: 'RESOLVED',
}

function ago(iso) {
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

// "The bot is alive" feed — every action the copy engine (or a manual close)
// took on this account, newest first. Auto-refreshes.
export default function ActivityFeed() {
  const [rows, setRows] = useState(null)

  useEffect(() => {
    const load = () => api.activity(30).then(setRows).catch(() => {})
    load()
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [])

  if (!rows) return <div className="muted small">loading activity…</div>
  if (rows.length === 0) {
    return (
      <div className="muted small">
        nothing yet — once you fund your wallet and copy a trader, every trade
        the engine makes for you shows up here
      </div>
    )
  }

  return (
    <div>
      {rows.map((r, i) => (
        <div className="feed-row" key={`${r.ts}-${i}`}>
          <span className={`feed-verb ${r.event_type === 'open' ? 'pos' : r.pnl != null ? (r.pnl >= 0 ? 'pos' : 'neg') : ''}`}>
            {VERB[r.event_type] || r.event_type.toUpperCase()}
          </span>
          <span className="tp-title">
            <a
              href={`https://polymarket.com/event/${r.market_slug}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              {(r.market_title || 'market').slice(0, 42)}
            </a>{' '}
            <span className={`badge ${r.outcome === 'YES' ? 'pos' : 'neg'}`}>{r.outcome}</span>
          </span>
          <span className="muted">copying {r.trader_name || short(r.trader_address)}</span>
          {r.pnl != null ? (
            <span className={r.pnl >= 0 ? 'pos' : 'neg'}>{money(r.pnl)}</span>
          ) : (
            <span className="muted">${(r.amount_usd || 0).toFixed(2)}</span>
          )}
          <span className="muted feed-time">{ago(r.ts)}</span>
        </div>
      ))}
    </div>
  )
}
