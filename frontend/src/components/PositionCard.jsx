import { useState } from 'react'
import { api, haptic } from '../api'
import Modal from './Modal'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')
const cents = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}¢`)

export default function PositionCard({ p, closed, onClose }) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [closeSlippage, setCloseSlippage] = useState(2)
  const value = closed ? p.realized_pnl || 0 : p.unrealized_pnl
  const refPrice = closed ? p.exit_price : p.current_price
  const movePct = p.entry_price && refPrice != null
    ? ((refPrice - p.entry_price) / p.entry_price) * 100
    : null

  const manual = p.trader_address === 'manual'
  const needsReconciliation = p.reconciliation_required
    || p.status === 'closing'
    || p.status === 'reconciliation_required'

  async function doClose() {
    setBusy(true)
    setMsg('')
    try {
      const r = p.external
        ? await api.closeExternal(p.token_id, closeSlippage)
        : await api.closePosition(p.id, closeSlippage)
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
      {needsReconciliation ? (
        <div className="muted">
          <strong>claim {String(p.claim_state || p.status).toUpperCase()}</strong>
          {p.claim_action ? ` · ${p.claim_action} BUY` : ''}
          {p.reserved_usd != null ? ` · $${Number(p.reserved_usd).toFixed(2)} reserved` : ''}
          {p.claim_id ? ` · ${short(p.claim_id)}` : ''}
          <div>reconciliation required — close and retry are disabled</div>
          {p.claim_error && <div className="neg">{p.claim_error}</div>}
        </div>
      ) : manual ? (
        <div className="muted">
          closed by you
        </div>
      ) : p.external ? (
        <div className="muted">
          {p.origin === 'bot_history'
            ? `bot history links this token to ${short(p.trader_address)} — current shares need reconciliation`
            : 'untracked wallet position — origin not confirmed'}
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
      {!closed && (needsReconciliation ? (
        <div className="muted" style={{ marginTop: 8 }}>
          <span className="badge neg">RECONCILIATION REQUIRED</span>
        </div>
      ) : p.redeemable ? (
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
          <label className="fld">
            Acceptable slippage: <strong>{closeSlippage.toFixed(1)}%</strong>
            <div className="slider-row">
              <input
                type="range"
                min="0"
                max="10"
                step="0.5"
                value={closeSlippage}
                onChange={(e) => setCloseSlippage(Number(e.target.value))}
                disabled={busy}
                aria-label="Acceptable close slippage percentage"
              />
            </div>
          </label>
          <div className="muted" style={{ marginBottom: 10 }}>
            The order will not fill below the selected price tolerance.
          </div>
          {msg && <div className="muted">{msg}</div>}
          <button className="btn btn-danger" disabled={busy} onClick={doClose}>
            {busy ? 'CLOSING…' : 'CONFIRM CLOSE'}
          </button>
        </Modal>
      )}
    </div>
  )
}
