import { useEffect, useState } from 'react'
import { api } from '../api'

const cents = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}¢`)
const money = (v) => `${(v || 0) >= 0 ? '+' : '-'}$${Math.abs(v || 0).toFixed(2)}`

function ago(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

// What is this wallet actually holding and doing right now? Fetched on first
// expand — live positions + recent trades, so the user sees the real book
// before committing money to copying it.
export default function TraderProfile({ address }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.trader(address).then(setData).catch((e) => setErr(String(e.message || e)))
  }, [address])

  if (err) return <div className="warn-box">{err}</div>
  if (!data) return <div className="muted small">loading live positions…</div>

  // live (still tradeable) first; resolved-but-unredeemed leftovers are shown
  // too — a "100% win rate" wallet with a pile of resolved losses is exactly
  // what this view exists to expose — but labeled, not passed off as open.
  const held = (data.positions || []).filter((p) => p.size > 0)
  const positions = held
    .sort((a, b) => (a.redeemable ? 1 : 0) - (b.redeemable ? 1 : 0)
      || b.current_value - a.current_value)
    .slice(0, 8)
  const trades = (data.recent_trades || []).slice(0, 10)

  return (
    <div className="tp">
      <div className="section-header">
        POSITIONS ({positions.length}{held.length > 8 ? ` OF ${held.length}` : ''})
      </div>
      {positions.length === 0 ? (
        <div className="muted small">no open positions</div>
      ) : (
        positions.map((p) => (
          <div className="tp-row" key={p.asset}>
            <span className="tp-title">{(p.title || '').slice(0, 44)}</span>
            <span className={`badge ${p.outcome === 'Yes' ? 'pos' : 'neg'}`}>{(p.outcome || '').toUpperCase()}</span>
            {p.redeemable && (
              <span className="badge" title="market already resolved — this is a leftover, not a live position">
                RESOLVED
              </span>
            )}
            <span className="muted">{Math.round(p.size)}sh</span>
            <span className="muted">{cents(p.avg_price)}→{cents(p.cur_price)}</span>
            <span className={p.cash_pnl >= 0 ? 'pos' : 'neg'}>{money(p.cash_pnl)}</span>
          </div>
        ))
      )}

      <div className="section-header">RECENT TRADES</div>
      {trades.length === 0 ? (
        <div className="muted small">no recent trades</div>
      ) : (
        trades.map((t, i) => (
          <div className="tp-row" key={`${t.tx_hash || i}`}>
            <span className={t.side === 'BUY' ? 'pos' : 'neg'}>{t.side}</span>
            <span className="tp-title">{(t.title || '').slice(0, 40)}</span>
            <span className="muted">{Math.round(t.size)}sh @ {cents(t.price)}</span>
            <span className="muted">{ago(t.timestamp)}</span>
          </div>
        ))
      )}
    </div>
  )
}
