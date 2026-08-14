import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { api, haptic } from '../api'
import PositionCard from '../components/PositionCard'
import ActivityFeed from '../components/ActivityFeed'
import Modal from '../components/Modal'
import {
  CLOSE_POSITION_EVENT,
  CLOSE_POSITION_STATE,
  UNCERTAIN_EXECUTION_DETAIL,
  canDismissClosePosition,
  createCloseSubmissionGuard,
  dismissClosePosition,
  executeFreshCloseAttempt,
  isReconciliationRequestCurrent,
  refreshReconciliationStatus,
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
  const [reconciliationRefreshOk, setReconciliationRefreshOk] = useState(false)
  const [reconciliationRefreshPending, setReconciliationRefreshPending] = useState(false)
  const closeStateRef = useRef(CLOSE_POSITION_STATE.IDLE)
  const closeTargetRef = useRef(null)
  const closeSlippageRef = useRef(2)
  const closeOperationGenerationRef = useRef(0)
  const closeRefreshSequenceRef = useRef(0)
  const closeRefreshPendingRef = useRef(false)
  const loadSequenceRef = useRef(0)
  const tabRef = useRef('open')
  const mountedRef = useRef(false)
  const submissionGuard = useRef(createCloseSubmissionGuard())
  const pageContainerRef = useRef(null)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      loadSequenceRef.current += 1
      closeRefreshSequenceRef.current += 1
    }
  }, [])

  const load = useCallback(async () => {
    const requestedTab = tab
    if (requestedTab === 'activity') return   // ActivityFeed fetches its own data
    const sequence = ++loadSequenceRef.current
    setLoading(true)
    const fn = requestedTab === 'open' ? api.openPositions : api.closedPositions
    try {
      const result = await fn()
      if (mountedRef.current && sequence === loadSequenceRef.current && tabRef.current === requestedTab) {
        setRows(result)
      }
    } finally {
      if (mountedRef.current && sequence === loadSequenceRef.current && tabRef.current === requestedTab) {
        setLoading(false)
      }
    }
  }, [tab])

  useEffect(() => {
    load().catch(() => {})
  }, [load])

  // open positions move with the market — keep them fresh without manual reloads
  useEffect(() => {
    if (tab !== 'open') return
    const id = setInterval(() => { load().catch(() => {}) }, 30000)
    return () => clearInterval(id)
  }, [tab, load])

  const cells = useMemo(() => summarize(rows, tab === 'closed'), [rows, tab])

  const selectTab = useCallback((nextTab) => {
    tabRef.current = nextTab
    loadSequenceRef.current += 1
    setTab(nextTab)
  }, [])

  const transitionClose = useCallback((event) => {
    const next = reduceClosePosition(closeStateRef.current, event)
    closeStateRef.current = next
    setCloseState(next)
    return next
  }, [])

  const handleRequestClose = useCallback((position) => {
    const requested = requestClosePosition(closeStateRef.current, closeTargetRef.current, position)
    if (requested.state === closeStateRef.current) return
    closeOperationGenerationRef.current += 1
    closeRefreshSequenceRef.current += 1
    closeStateRef.current = requested.state
    closeTargetRef.current = requested.target
    closeSlippageRef.current = 2
    setCloseTarget(requested.target)
    setCloseDetail('')
    setReconciliationRefreshOk(false)
    setCloseSlippage(2)
    setCloseState(requested.state)
  }, [])

  const handleDismissClose = useCallback(() => {
    const next = dismissClosePosition(
      closeStateRef.current,
      'backdrop',
      closeTarget,
      rows,
      tab,
      reconciliationRefreshOk,
    )
    if (next === closeStateRef.current) return
    closeStateRef.current = next
    closeOperationGenerationRef.current += 1
    closeRefreshSequenceRef.current += 1
    closeRefreshPendingRef.current = false
    closeTargetRef.current = null
    setReconciliationRefreshPending(false)
    setCloseState(next)
    setCloseTarget(null)
    setCloseDetail('')
  }, [closeTarget, reconciliationRefreshOk, rows, tab])

  const refreshCloseStatus = useCallback(async () => {
    const refreshSequence = ++closeRefreshSequenceRef.current
    const request = {
      generation: closeOperationGenerationRef.current,
      refreshSequence,
      state: closeStateRef.current,
      target: closeTargetRef.current,
    }
    const isCurrent = () => mountedRef.current && isReconciliationRequestCurrent(request, {
      generation: closeOperationGenerationRef.current,
      refreshSequence: closeRefreshSequenceRef.current,
      state: closeStateRef.current,
      target: closeTargetRef.current,
    })
    if (!isCurrent()) return { ok: false, stale: true }
    closeRefreshPendingRef.current = true
    setReconciliationRefreshPending(true)
    tabRef.current = 'open'
    loadSequenceRef.current += 1
    setTab('open')
    try {
      const result = await refreshReconciliationStatus({
        target: request.target,
        openPositions: api.openPositions,
        closedPositions: api.closedPositions,
        updateRows: (freshRows) => {
          loadSequenceRef.current += 1
          setRows(freshRows)
          setLoading(false)
        },
        shouldApply: isCurrent,
      })
      if (!isCurrent()) return { ...result, stale: true }
      if (result.operationPositionId != null) {
        const target = Object.freeze({
          ...closeTargetRef.current,
          operation_position_id: result.operationPositionId,
        })
        closeTargetRef.current = target
        setCloseTarget(target)
      }
      setReconciliationRefreshOk(Boolean(result.allowDismiss))
      setCloseDetail(result.detail || UNCERTAIN_EXECUTION_DETAIL)
      if (result.event) transitionClose(result.event)
      return result
    } finally {
      if (refreshSequence === closeRefreshSequenceRef.current && mountedRef.current) {
        closeRefreshPendingRef.current = false
        setReconciliationRefreshPending(false)
      }
    }
  }, [transitionClose])

  const handleConfirmClose = useCallback(async () => {
    const current = closeStateRef.current
    let event
    if (current === CLOSE_POSITION_STATE.CONFIRMING) {
      event = CLOSE_POSITION_EVENT.CONFIRM
    } else if (current === CLOSE_POSITION_STATE.REJECTED) {
      event = CLOSE_POSITION_EVENT.RETRY
    } else {
      return
    }

    transitionClose(event)
    setCloseDetail('')
    const result = await executeFreshCloseAttempt({
      guard: submissionGuard.current,
      target: closeTargetRef.current,
      slippage: closeSlippageRef.current,
      openPositions: api.openPositions,
      updateRows: (freshRows) => {
        if (!mountedRef.current) return
        loadSequenceRef.current += 1
        setRows(freshRows)
      },
      updateTarget: (target) => {
        closeTargetRef.current = target
        if (mountedRef.current) setCloseTarget(target)
      },
      closeTracked: api.closePosition,
      closeExternal: api.closeExternal,
      haptic,
      refresh: load,
    })
    if (!result.accepted || !mountedRef.current) return
    if (result.value.target) {
      closeTargetRef.current = result.value.target
      setCloseTarget(result.value.target)
    }
    setCloseDetail(result.value.detail)
    transitionClose(result.value.event)
    if (result.value.event === CLOSE_POSITION_EVENT.UNCERTAIN_EXECUTION) {
      setReconciliationRefreshOk(false)
      await refreshCloseStatus()
    }
  }, [closeSlippage, closeTarget, load, refreshCloseStatus, transitionClose])

  const canSubmitClose = closeState === CLOSE_POSITION_STATE.CONFIRMING
    || closeState === CLOSE_POSITION_STATE.REJECTED

  const canDismissModal = canDismissClosePosition(
    closeState,
    closeTarget,
    rows,
    tab,
    reconciliationRefreshOk,
  )

  return (
    <div ref={pageContainerRef} tabIndex={-1} data-modal-focus-fallback>
      <div className="toggle-row">
        {TABS.map(([k, label]) => (
          <button key={k} className={`chip ${tab === k ? 'active' : ''}`} onClick={() => selectTab(k)}>
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
          title={closeState === CLOSE_POSITION_STATE.RECONCILIATION_REQUIRED
            ? 'EXECUTION STATUS'
            : closeState === CLOSE_POSITION_STATE.SUBMITTING
              ? 'CLOSING POSITION'
              : closeState === CLOSE_POSITION_STATE.CONFIRMED
                ? 'POSITION CLOSED'
                : closeState === CLOSE_POSITION_STATE.FAILED
                  ? 'CLOSE NOT COMPLETED'
                  : 'CONFIRM CLOSE'}
          accent="red"
          onClose={handleDismissClose}
          canClose={canDismissModal}
          returnFocusRef={pageContainerRef}
        >
          {canSubmitClose && (
            <>
              <p><strong>Close the entire current position</strong></p>
              <p className="muted">
                Estimated shares: {Number(closeTarget.shares || 0).toFixed(6)}
                {closeTarget.current_price != null
                  ? ` · Estimated value: $${((closeTarget.shares || 0) * closeTarget.current_price).toFixed(2)} at ${(closeTarget.current_price * 100).toFixed(1)}¢`
                  : ''}
              </p>
              {closeDetail && <div className="muted">{closeDetail}</div>}
              <label className="fld">
                Acceptable slippage: <strong>{closeSlippage.toFixed(1)}%</strong>
                <div className="slider-row">
                  <input
                    type="range"
                    min="0"
                    max="10"
                    step="0.5"
                    value={closeSlippage}
                    onChange={(e) => {
                      const value = Number(e.target.value)
                      closeSlippageRef.current = value
                      setCloseSlippage(value)
                    }}
                    aria-label="Acceptable close slippage percentage"
                  />
                </div>
              </label>
              <div className="muted" style={{ marginBottom: 10 }}>
                The order will not fill below the selected price tolerance.
              </div>
              <button className="btn btn-danger" onClick={handleConfirmClose}>
                {closeState === CLOSE_POSITION_STATE.CONFIRMING ? 'CONFIRM CLOSE' : 'TRY CLOSE AGAIN'}
              </button>
            </>
          )}
          {closeState === CLOSE_POSITION_STATE.SUBMITTING && (
            <div className="muted" role="status">CLOSING…</div>
          )}
          {closeState === CLOSE_POSITION_STATE.RECONCILIATION_REQUIRED && (
            <>
              <div className="muted">{closeDetail || UNCERTAIN_EXECUTION_DETAIL}</div>
              <button className="btn" onClick={refreshCloseStatus} disabled={reconciliationRefreshPending}>
                {reconciliationRefreshPending ? 'REFRESHING…' : 'REFRESH STATUS'}
              </button>
            </>
          )}
          {closeState === CLOSE_POSITION_STATE.CONFIRMED && (
            <div className="muted">CLOSED ✓</div>
          )}
          {closeState === CLOSE_POSITION_STATE.FAILED && (
            <div className="muted">
              {closeDetail || 'Close could not be safely completed.'} No new SELL will be submitted from this dialog.
            </div>
          )}
        </Modal>
      )}
    </div>
  )
}
