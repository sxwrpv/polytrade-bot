import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { api, haptic } from '../api'
import PositionCard from '../components/PositionCard'
import ActivityFeed from '../components/ActivityFeed'
import Modal from '../components/Modal'
import {
  CLOSE_POSITION_EVENT,
  CLOSE_POSITION_STATE,
  canDismissClosePosition,
  createCloseSubmissionGuard,
  dismissClosePosition,
  executeCloseSubmission,
  reduceClosePosition,
  requestClosePosition,
} from '../closePositionState'

const signed = (v) => `${v >= 0 ? '+' : '-'}$${Math.abs(v).toFixed(2)}`

function summarize(rows, closed) {
  if (closed) {
    const realized = rows.map((r) => Number(r.realized_pnl || 0))
    const total = realized.reduce((a, b) => a + b, 0)
    const wins = realized.filter((v) => v > 0).length
    return [
      ['REALIZED PNL', signed(total), total >= 0 ? 'pos' : 'neg'],
      ['WIN RATE', realized.length ? `${Math.round((wins / realized.length) * 100)}%` : '—', ''],
      ['BEST', realized.length ? signed(Math.max(...realized)) : '—', 'pos'],
      ['WORST', realized.length ? signed(Math.min(...realized)) : '—', 'neg'],
    ]
  }
  const exposure = rows.reduce((a, r) => a + Number(r.notional_usd || 0), 0)
  const unrealized = rows.reduce((a, r) => a + Number(r.unrealized_pnl || 0), 0)
  return [
    ['OPEN EXPOSURE', `$${exposure.toFixed(2)}`, ''],
    ['UNREALIZED PNL', signed(unrealized), unrealized >= 0 ? 'pos' : 'neg'],
    ['OPEN POSITIONS', rows.length, ''],
  ]
}

const TABS = [['open', 'OPEN'], ['closed', 'CLOSED'], ['activity', 'ACTIVITY']]

export default function Positions() {
  const [tab, setTab] = useState('open')
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [closeTarget, setCloseTarget] = useState(null)
  const [closeState, setCloseState] = useState(CLOSE_POSITION_STATE.IDLE)
  const [closeSlippage, setCloseSlippage] = useState(2)
  const [closeDetail, setCloseDetail] = useState('')
  const closeStateRef = useRef(CLOSE_POSITION_STATE.IDLE)
  const submissionGuard = useRef(createCloseSubmissionGuard())
  const pageContainerRef = useRef(null)

  const load = useCallback(() => {
    if (tab === 'activity') return   // ActivityFeed fetches its own data
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

  const transitionClose = useCallback((event) => {
    const next = reduceClosePosition(closeStateRef.current, event)
    closeStateRef.current = next
    setCloseState(next)
    return next
  }, [])

  const handleRequestClose = useCallback((position) => {
    const requested = requestClosePosition(closeStateRef.current, closeTarget, position)
    if (requested.state === closeStateRef.current) return
    closeStateRef.current = requested.state
    setCloseTarget(requested.target)
    setCloseDetail('')
    setCloseSlippage(2)
    setCloseState(requested.state)
  }, [closeTarget])

  const handleDismissClose = useCallback(() => {
    const next = dismissClosePosition(closeStateRef.current, 'backdrop')
    if (next === closeStateRef.current) return
    closeStateRef.current = next
    setCloseState(next)
    setCloseTarget(null)
    setCloseDetail('')
  }, [])

  const handleConfirmClose = useCallback(async () => {
    const current = closeStateRef.current
    let event
    if (current === CLOSE_POSITION_STATE.CONFIRMING) {
      event = CLOSE_POSITION_EVENT.CONFIRM
    } else if (current === CLOSE_POSITION_STATE.REJECTED || current === CLOSE_POSITION_STATE.FAILED) {
      event = CLOSE_POSITION_EVENT.RETRY
    } else {
      return
    }

    transitionClose(event)
    setCloseDetail('')
    const result = await executeCloseSubmission({
      guard: submissionGuard.current,
      target: closeTarget,
      slippage: closeSlippage,
      closeTracked: api.closePosition,
      closeExternal: api.closeExternal,
      haptic,
      refresh: load,
    })
    if (!result.accepted) return
    setCloseDetail(result.value.detail)
    transitionClose(result.value.event)
  }, [closeSlippage, closeTarget, load, transitionClose])

  const canSubmitClose = closeState === CLOSE_POSITION_STATE.CONFIRMING
    || closeState === CLOSE_POSITION_STATE.REJECTED
    || closeState === CLOSE_POSITION_STATE.FAILED

  return (
    <div ref={pageContainerRef} tabIndex={-1} data-modal-focus-fallback>
      <div className="toggle-row">
        {TABS.map(([k, label]) => (
          <button key={k} className={`chip ${tab === k ? 'active' : ''}`} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'activity' ? (
        <ActivityFeed />
      ) : (
        <>
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
              <PositionCard
                key={r.id || r.token_id}
                p={r}
                closed={tab === 'closed'}
                onRequestClose={handleRequestClose}
              />
            ))
          )}
        </>
      )}

      {closeTarget && closeState !== CLOSE_POSITION_STATE.IDLE && (
        <Modal
          title="CONFIRM CLOSE"
          accent="red"
          onClose={handleDismissClose}
          canClose={canDismissClosePosition(closeState)}
          returnFocusRef={pageContainerRef}
        >
          <p className="muted">
            Sell {(closeTarget.shares || 0).toFixed(0)} shares at market
            {closeTarget.current_price != null
              ? ` (~$${((closeTarget.shares || 0) * closeTarget.current_price).toFixed(2)} at ${(closeTarget.current_price * 100).toFixed(1)}¢)`
              : ''}?
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
                disabled={closeState === CLOSE_POSITION_STATE.SUBMITTING}
                aria-label="Acceptable close slippage percentage"
              />
            </div>
          </label>
          <div className="muted" style={{ marginBottom: 10 }}>
            The order will not fill below the selected price tolerance.
          </div>
          {closeDetail && <div className="muted">{closeDetail}</div>}
          {canSubmitClose && (
            <button className="btn btn-danger" onClick={handleConfirmClose}>
              {closeState === CLOSE_POSITION_STATE.CONFIRMING ? 'CONFIRM CLOSE' : 'TRY CLOSE AGAIN'}
            </button>
          )}
          {closeState === CLOSE_POSITION_STATE.SUBMITTING && (
            <button className="btn btn-danger" disabled>CLOSING…</button>
          )}
        </Modal>
      )}
    </div>
  )
}
