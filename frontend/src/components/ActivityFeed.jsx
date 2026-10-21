import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { ErrorState, Notice, SkeletonCards, StateBlock } from './UiState'
import { dollars } from '../money'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')
const money = (v) => `${(v || 0) >= 0 ? '+' : '−'}$${dollars(Math.abs(v || 0))}`

const VERB = {
  open: 'Opened',
  close: 'Closed',
  partial: 'Resized',
  resolve: 'Resolved',
}

function ago(iso) {
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

// "The bot is alive" feed — every action the copy engine (or a manual close)
// took on this account, newest first. Auto-refreshes; a failed refresh keeps
// the rows already shown and says so, a failed first read offers a retry.
export default function ActivityFeed() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(() => api.activity(30)
    .then((r) => { setRows(r); setError(null) })
    .catch((e) => setError(e)), [])

  useEffect(() => {
    load()
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [load])

  if (!rows && error) return <ErrorState title="Couldn’t load activity" error={error} onRetry={load} />
  if (!rows) return <SkeletonCards count={3} label="Loading activity" />
  if (rows.length === 0) {
    return (
      <StateBlock icon="activity" title="No activity yet">
        Once your wallet is funded and you copy a trader, every trade the engine makes for you shows up here.
      </StateBlock>
    )
  }

  return (
    <div>
      {error && (
        <div className="list-notice">
          <Notice tone="error">Couldn’t refresh. Showing the activity loaded earlier.</Notice>
        </div>
      )}
      <ol className="feed">
        {rows.map((r, i) => {
          const tone = r.event_type === 'open' ? 'pos' : r.pnl != null ? (r.pnl >= 0 ? 'pos' : 'neg') : 'quiet'
          return (
            <li className="feed-row" key={`${r.ts}-${i}`}>
              <span className={`badge feed-verb ${tone}`}>
                {VERB[r.event_type] || r.event_type}
              </span>
              <span className="feed-title">
                <a
                  href={`https://polymarket.com/event/${r.market_slug}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {r.market_title || 'Market'}
                </a>
              </span>
              <span className={`feed-amount ${r.pnl != null ? (r.pnl >= 0 ? 'pos' : 'neg') : ''}`}>
                {r.pnl != null ? money(r.pnl) : `$${dollars(r.amount_usd || 0)}`}
                <small>{r.outcome}</small>
              </span>
              <span className="feed-meta">
                Copying {r.trader_name || short(r.trader_address)} · <time dateTime={r.ts}>{ago(r.ts)}</time>
              </span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
