import { useId, useState } from 'react'
import { api, haptic } from '../api'
import Modal from './Modal'
import TraderProfile from './TraderProfile'
import { discoveryMetrics, formatActivePositions, formatMoney } from './traderCardModel'

const short = (address) => (address ? `${address.slice(0, 6)}…${address.slice(-4)}` : '—')
const unavailable = '—'

function percent(value) {
  return value == null ? unavailable : `${(Number(value) * 100).toFixed(0)}%`
}

function refreshedLabel(value) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'STATS UPDATED'
  const stamp = date.toISOString().slice(0, 16).replace('T', ' ')
  return `UPDATED ${stamp} UTC`
}

export default function TraderCard({ t, period = '30d', onFollowed, balance }) {
  const analysisRegionId = `trader-analysis-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [ratio, setRatio] = useState(1)
  const [maxPos, setMaxPos] = useState(15)
  const [msg, setMsg] = useState('')
  const metrics = discoveryMetrics(t, period)
  const traderName = t.display_name && !t.display_name.startsWith('0x')
    ? t.display_name
    : 'UNNAMED TRADER'
  const freshness = refreshedLabel(metrics.refreshedAt)

  async function follow() {
    setMsg('')
    try {
      await api.follow(t.address, {
        copy_ratio_pct: Number(ratio),
        max_position_usd: Number(maxPos),
      })
      setMsg('COPYING ✓')
      haptic('success')
      onFollowed?.()
      setTimeout(() => setOpen(false), 800)
    } catch (e) {
      setMsg(String(e.message || e))
    }
  }

  return (
    <div className="card trader-card">
      <div className="tc-top">
        <div className="tc-identity">
          <span className="tc-name">{traderName}</span>
          <span className="muted addr-inline" title={t.address}>{short(t.address)}</span>
        </div>
        <span className="tc-period-label">SELECTED PERIOD · {metrics.period.toUpperCase()}</span>
      </div>

      <div className="tc-discovery-stats">
        <div>
          <span>REALIZED PNL · {metrics.period.toUpperCase()}</span>
          <strong className={metrics.realizedPnl == null ? '' : metrics.realizedPnl >= 0 ? 'pos' : 'neg'}>
            {formatMoney(metrics.realizedPnl, { signed: true })}
          </strong>
        </div>
        <div>
          <span>WIN RATE · {metrics.period.toUpperCase()}</span>
          <strong>{percent(metrics.winRate)}</strong>
        </div>
        <div>
          <span>GROSS VOLUME · {metrics.period.toUpperCase()}</span>
          <strong>{formatMoney(metrics.grossVolume)}</strong>
        </div>
        <div>
          <span>ACTIVE POSITIONS*</span>
          <strong>{formatActivePositions(metrics.activePositions)}</strong>
        </div>
      </div>

      <div className="tc-evidence-state">
        {metrics.historyDays == null ? (
          <span>FETCHED TRADE HISTORY · UNAVAILABLE</span>
        ) : metrics.partial ? (
          <span title="fetched trade history covers only part of the selected period">
            FETCHED TRADE HISTORY · PARTIAL · ~{metrics.historyDays}D OF {metrics.days}D
          </span>
        ) : (
          <span>FETCHED TRADE HISTORY · ~{metrics.days}D OBSERVED</span>
        )}
        <span title="Open non-redeemable positions in the fetched snapshot; the source can truncate at 500 rows.">
          * fetched positions snapshot; count can be a lower bound if source reaches its 500-row cap.
        </span>
        {freshness && <time dateTime={metrics.refreshedAt}>{freshness}</time>}
      </div>

      <div className="tc-actions">
        <button
          className="btn"
          aria-expanded={expanded}
          aria-controls={analysisRegionId}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? 'HIDE ANALYSIS' : 'ANALYZE'}
        </button>
      </div>

      {expanded && (
        <div
          className="tc-analysis"
          id={analysisRegionId}
          role="region"
          aria-label="Trader analysis"
        >
          <div className="tc-source-note muted small">
            <strong>METRIC SOURCES</strong>
            <span>PNL AND WIN RATE ARE RECONSTRUCTED FROM FETCHED CLOSING EVENTS.</span>
            <span>TRADE, REDEEM, AND POSITIONS SOURCES CAN EACH BE PARTIAL.</span>
            <span>GROSS VOLUME USES FETCHED TRADE ROWS.</span>
          </div>
          <TraderProfile address={t.address} />
          <button className="btn btn-ghost tc-copy-settings" onClick={() => setOpen(true)}>COPY SETTINGS</button>
        </div>
      )}

      {open && (
        <Modal title="COPY SETTINGS" accent="green" onClose={() => setOpen(false)}>
          <label className="fld">
            RATIO % (of leader&apos;s position size)
            <input value={ratio} onChange={(e) => setRatio(e.target.value)} />
          </label>
          <label className="fld">
            MAX / TRADE (pUSD)
            <input value={maxPos} onChange={(e) => setMaxPos(e.target.value)} />
          </label>
          <div className="muted small">
            each copy = leader&apos;s position × {ratio || 0}%, capped at ${maxPos || 0}. Fine-tune
            price/exposure/max-open filters after copying, under COPIED WALLETS.
          </div>
          {balance != null && balance <= 0 && (
            <div className="warn-box">
              YOUR BALANCE IS $0 — COPYING WILL BE SET UP, BUT NO TRADES CAN
              EXECUTE UNTIL YOU FUND YOUR WALLET (USER &gt; FUND WALLET).
            </div>
          )}
          {msg && <div className="muted">{msg}</div>}
          <button className="btn" onClick={follow}>CONFIRM COPY</button>
        </Modal>
      )}
    </div>
  )
}
