import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { api, haptic } from '../api'
import { buildCloseOutcomeTelemetry, trackTelemetry } from '../telemetry'
import PositionCard from '../components/PositionCard'
import { useRedeemStatus } from '../redeemStatus'
import ActivityFeed from '../components/ActivityFeed'
import Modal from '../components/Modal'
import Icon from '../components/Icon'
import { ErrorState, Notice, SkeletonCards, StateBlock } from '../components/UiState'
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
import { dollars } from '../money'

const signed = (v) => `${v >= 0 ? '+' : '-'}$${dollars(Math.abs(v))}`

function summarize(rows, closed) {
  if (closed) {
    const realized = rows.map((r) => Number(r.realized_pnl || 0))
    const total = realized.reduce((a, b) => a + b, 0)
    const wins = realized.filter((v) => v > 0).length
    return [
      ['Realized P&L', signed(total), total >= 0 ? 'pos' : 'neg'],
      ['Win rate', realized.length ? `${Math.round((wins / realized.length) * 100)}%` : '—', ''],
      ['Best', realized.length ? signed(Math.max(...realized)) : '—', 'pos'],
      ['Worst', realized.length ? signed(Math.min(...realized)) : '—', 'neg'],
    ]
  }
  const exposure = rows.reduce((a, r) => a + Number(r.notional_usd || 0), 0)
  const unrealized = rows.reduce((a, r) => a + Number(r.unrealized_pnl || 0), 0)
  return [
    ['Open exposure', `$${dollars(exposure)}`, ''],
    ['Unrealized P&L', signed(unrealized), unrealized >= 0 ? 'pos' : 'neg'],
    ['Open positions', rows.length, ''],
  ]
}

const TABS = [['open', 'Open'], ['closed', 'Closed'], ['activity', 'Activity']]

export default function Positions({ onNavigate }) {
  const redeem = useRedeemStatus()
  const [tab, setTab] = useState('open')
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  // A failed read is kept apart from an empty one: "no open positions" after
  // a timeout would be a false statement about the account's money.
  const [error, setError] = useState(null)
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
  const closeSubmittedAtRef = useRef(null)

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
        setError(null)
      }
    } catch (failure) {
      // Recorded for the view, then rethrown: callers such as the close flow's
      // post-sell refresh rely on a failed reload still rejecting.
      if (mountedRef.current && sequence === loadSequenceRef.current && tabRef.current === requestedTab) {
        setError(failure)
      }
      throw failure
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
    if (nextTab === tabRef.current) return
    tabRef.current = nextTab
    loadSequenceRef.current += 1
    // The previous tab's rows must not show under the new tab's heading while
    // its own list loads.
    setRows([])
    setError(null)
    setLoading(nextTab !== 'activity')
    setTab(nextTab)
  }, [])

  const retryLoad = useCallback(() => { load().catch(() => {}) }, [load])

  const transitionClose = useCallback((event) => {
    const next = reduceClosePosition(closeStateRef.current, event)
    closeStateRef.current = next
    setCloseState(next)
    return next
  }, [])

  const trackCloseOutcome = useCallback((event) => {
    const outcome = buildCloseOutcomeTelemetry(
      event,
      closeSubmittedAtRef.current,
      Date.now(),
    )
    if (!outcome) {
      if (event === CLOSE_POSITION_EVENT.TARGET_CHANGED) closeSubmittedAtRef.current = null
      return
    }
    trackTelemetry(outcome.eventName, outcome.properties)
    if (outcome.terminal) closeSubmittedAtRef.current = null
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
    trackTelemetry('close_modal_opened', { source: 'positions' })
  }, [])

  const handleDismissClose = useCallback(() => {
    const dismissedState = closeStateRef.current
    const next = dismissClosePosition(
      closeStateRef.current,
      'backdrop',
      closeTarget,
      rows,
      tab,
      reconciliationRefreshOk,
    )
    if (next === closeStateRef.current) return
    trackTelemetry('modal_dismissed', { state: dismissedState, source: 'positions' })
    closeSubmittedAtRef.current = null
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
      if (result.event) {
        transitionClose(result.event)
        trackCloseOutcome(result.event)
      }
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
    closeSubmittedAtRef.current = Date.now()
    trackTelemetry('close_submitted', { source: 'positions' })
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
    if (!result.accepted || !mountedRef.current) {
      closeSubmittedAtRef.current = null
      return
    }
    if (result.value.target) {
      closeTargetRef.current = result.value.target
      setCloseTarget(result.value.target)
    }
    setCloseDetail(result.value.detail)
    transitionClose(result.value.event)
    trackCloseOutcome(result.value.event)
    if (result.value.event === CLOSE_POSITION_EVENT.UNCERTAIN_EXECUTION) {
      setReconciliationRefreshOk(false)
      await refreshCloseStatus()
    }
  }, [closeSlippage, closeTarget, load, refreshCloseStatus, trackCloseOutcome, transitionClose])

  const canSubmitClose = closeState === CLOSE_POSITION_STATE.CONFIRMING
    || closeState === CLOSE_POSITION_STATE.REJECTED

  const canDismissModal = canDismissClosePosition(
    closeState,
    closeTarget,
    rows,
    tab,
    reconciliationRefreshOk,
  )

  const tabIndex = TABS.findIndex(([k]) => k === tab)
  const titleByState = {
    [CLOSE_POSITION_STATE.RECONCILIATION_REQUIRED]: 'Execution status',
    [CLOSE_POSITION_STATE.SUBMITTING]: 'Closing position',
    [CLOSE_POSITION_STATE.CONFIRMED]: 'Position closed',
    [CLOSE_POSITION_STATE.FAILED]: 'Close not completed',
  }

  return (
    <div ref={pageContainerRef} tabIndex={-1} data-modal-focus-fallback className="page-focus">
      <div
        className="seg seg-row"
        role="group"
        aria-label="Positions view"
        style={{ '--n': TABS.length, '--i': tabIndex }}
      >
        {TABS.map(([k, label]) => (
          <button key={k} type="button" aria-pressed={tab === k} onClick={() => selectTab(k)}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'activity' ? (
        <ActivityFeed />
      ) : (
        <>
          {rows.length > 0 && (
            <div className="stat-grid">
              {cells.map(([label, value, c]) => (
                <div className="stat-cell" key={label}>
                  <div className="label">{label}</div>
                  <div className={`value ${c}`} style={{ '--len': String(value).length }}>{value}</div>
                </div>
              ))}
            </div>
          )}

          {error && rows.length > 0 && (
            <div className="list-notice">
              <Notice
                tone="error"
                action={(
                  <button type="button" className="btn btn-ghost btn-sm" onClick={retryLoad} disabled={loading}>
                    <Icon name="refresh" />Retry
                  </button>
                )}
              >
                Couldn’t refresh. These are the positions loaded earlier.
              </Notice>
            </div>
          )}

          {loading && rows.length === 0 ? (
            <SkeletonCards count={3} label={`Loading ${tab} positions`} />
          ) : error && rows.length === 0 ? (
            <ErrorState
              title={`Couldn’t load ${tab} positions`}
              error={error}
              onRetry={retryLoad}
              retrying={loading}
            />
          ) : rows.length === 0 ? (
            tab === 'open' ? (
              <StateBlock
                icon="layers"
                title="No open positions"
                action={onNavigate && (
                  <button type="button" className="btn btn-ghost" onClick={() => onNavigate('home')}>
                    Copy a wallet
                  </button>
                )}
              >
                Positions appear here when a wallet you copy opens a trade.
              </StateBlock>
            ) : (
              <StateBlock icon="clock" title="No closed positions">
                Nothing was closed in the last 12 hours.
              </StateBlock>
            )
          ) : (
            rows.map((r) => (
              <PositionCard
                key={r.id || r.token_id}
                p={r}
                closed={tab === 'closed'}
                onRequestClose={handleRequestClose}
                redeem={redeem}
              />
            ))
          )}
          {tab === 'closed' && !loading && (
            <div className="history-note">
              <div>Showing position history from the last 12 hours.</div>
              <a
                href="https://polymarket.com/portfolio"
                target="_blank"
                rel="noreferrer"
              >
                More position history on Polymarket
              </a>
            </div>
          )}
        </>
      )}

      {closeTarget && closeState !== CLOSE_POSITION_STATE.IDLE && (
        <Modal
          title={titleByState[closeState] || 'Close this position?'}
          accent="red"
          onClose={handleDismissClose}
          canClose={canDismissModal}
          returnFocusRef={pageContainerRef}
        >
          <p className="modal-market">{closeTarget.market_title || 'This position'}</p>
          {canSubmitClose && (
            <>
              <p className="modal-lede">Close the entire current position at market, within the price tolerance below.</p>
              <dl className="modal-facts">
                <div>
                  <dt>Estimated shares</dt>
                  <dd>{Number(closeTarget.shares || 0).toFixed(6)}</dd>
                </div>
                {closeTarget.current_price != null && (
                  <div>
                    <dt>Estimated value</dt>
                    <dd>${dollars((closeTarget.shares || 0) * closeTarget.current_price)} at {(closeTarget.current_price * 100).toFixed(1)}¢</dd>
                  </div>
                )}
              </dl>
              {closeDetail && <Notice>{closeDetail}</Notice>}
              <div className="fld fld-slider">
                <div className="setting-head">
                  <span>Acceptable slippage</span>
                  <span className="setting-val" aria-hidden="true">{closeSlippage.toFixed(1)}%</span>
                </div>
                <div className="slider-row">
                  <input
                    type="range"
                    min="0"
                    max="10"
                    step="0.5"
                    value={closeSlippage}
                    style={{ '--fill': `${closeSlippage * 10}%` }}
                    onChange={(e) => {
                      const value = Number(e.target.value)
                      closeSlippageRef.current = value
                      setCloseSlippage(value)
                    }}
                    aria-label="Acceptable close slippage percentage"
                  />
                </div>
                <span className="setting-hint">The order will not fill below the selected price tolerance.</span>
              </div>
              <div className="modal-actions">
                <button type="button" className="btn btn-danger is-strong" onClick={handleConfirmClose}>
                  {closeState === CLOSE_POSITION_STATE.CONFIRMING ? 'Close position' : 'Try closing again'}
                </button>
                <button type="button" className="btn btn-ghost" onClick={handleDismissClose} disabled={!canDismissModal}>
                  Keep position
                </button>
              </div>
            </>
          )}
          {closeState === CLOSE_POSITION_STATE.SUBMITTING && (
            <div className="modal-progress" role="status">
              <span className="launch-progress" aria-hidden="true" />
              <span>Selling at market. Waiting for the exchange to confirm…</span>
            </div>
          )}
          {closeState === CLOSE_POSITION_STATE.RECONCILIATION_REQUIRED && (
            <>
              <Notice>{closeDetail || UNCERTAIN_EXECUTION_DETAIL}</Notice>
              <div className="modal-actions">
                <button type="button" className="btn" onClick={refreshCloseStatus} disabled={reconciliationRefreshPending}>
                  <Icon name="refresh" />
                  {reconciliationRefreshPending ? 'Refreshing…' : 'Refresh status'}
                </button>
              </div>
            </>
          )}
          {closeState === CLOSE_POSITION_STATE.CONFIRMED && (
            <Notice tone="ok">{closeDetail || 'Position closed.'}</Notice>
          )}
          {closeState === CLOSE_POSITION_STATE.FAILED && (
            <Notice tone="error">
              {closeDetail || 'Close could not be safely completed.'} No new SELL will be submitted from this dialog.
            </Notice>
          )}
        </Modal>
      )}
    </div>
  )
}
