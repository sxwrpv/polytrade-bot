import { useState } from 'react'
import { api, haptic } from '../api'
import Modal from './Modal'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')
const cents = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}¢`)

export default function PositionCard({ p, closed, onClose }) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const value = closed ? p.realized_pnl || 0 : p.unrealized_pnl
  const refPrice = closed ? p.exit_price : p.current_price
  const movePct = p.entry_price && refPrice != null
    ? ((refPrice - p.entry_price) / p.entry_price) * 100
    : null

  // manual = held in the wallet but not opened by the bot: live rows carry
  // `external`; closed history rows carry the 'manual' sentinel trader_address
  const manual = p.external || p.trader_address === 'manual'

  async function doClose() {
    setBusy(true)
    setMsg('')
    try {
      const r = p.external ? await api.closeExternal(p.token_id) : await api.closePosition(p.id)
      setMsg(r.ok ? 'CLOSED ✓' : r.reason || 'failed')
      if (r.ok) {
        haptic('success')
        setTimeout(() => { setConfirm(false); onClose?.() }, 800)
      }
    } catch (e) {
      setMsg(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card pos-card">
      <div className="pc-title">
        {p.market_slug ? (
          <a
            href={`https://polymarket.com/event/${p.market_slug}`}
            target="_blank"
            rel="noopener noreferrer"
            title="view market on polymarket.com"
          >
            {p.market_title || `token ${(p.token_id || '').slice(0, 16)}…`}
          </a>
        ) : (
          p.market_title || `token ${(p.token_id || '').slice(0, 16)}…`
        )}
      </div>
      {manual ? (
        <div className="muted">
          {closed ? 'closed by you' : 'opened outside the bot — exits are yours to manage'}
        </div>
      ) : (
        <div className="muted">copying {short(p.trader_address)}</div>
      )}
      <div className="pc-row">
        <span className={`badge ${p.outcome === 'YES' ? 'pos' : 'neg'}`}>{p.outcome}</span>
        <span>entry {cents(p.entry_price)}</span>
        {!closed && <span>now {cents(p.current_price)}</span>}
        {closed && <span>exit {cents(p.exit_price)}</span>}
        {movePct != null && (
          <span className={movePct >= 0 ? 'pos' : 'neg'}>
            {movePct >= 0 ? '+' : ''}{movePct.toFixed(1)}%
          </span>
        )}
        <span className={value >= 0 ? 'pos' : 'neg'}>
          {value == null ? '—' : `${value >= 0 ? '+' : '-'}$${Math.abs(value).toFixed(2)}`}
        </span>
      </div>
      <div className="muted">{(p.shares || 0).toFixed(0)} shares</div>
      {!closed && (p.redeemable ? (
        <div className="muted" style={{ marginTop: 8 }}>
          <span className="badge pos">RESOLVED</span> winnings redeem automatically — nothing to sell
        </div>
      ) : (
        <button className="btn btn-danger" style={{ marginTop: 8 }} onClick={() => setConfirm(true)}>
          CLOSE
        </button>
      ))}

      {confirm && (
        <Modal title="CONFIRM CLOSE" accent="red" onClose={() => setConfirm(false)}>
          <p className="muted">
            Sell {(p.shares || 0).toFixed(0)} shares at market
            {p.current_price != null ? ` (~$${((p.shares || 0) * p.current_price).toFixed(2)} at ${cents(p.current_price)})` : ''}?
          </p>
          {msg && <div className="muted">{msg}</div>}
          <button className="btn btn-danger" disabled={busy} onClick={doClose}>
            {busy ? 'CLOSING…' : 'CONFIRM CLOSE'}
          </button>
        </Modal>
      )}
    </div>
  )
}
