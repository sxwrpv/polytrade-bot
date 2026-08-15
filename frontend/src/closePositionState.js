export const CLOSE_POSITION_STATE = Object.freeze({
  IDLE: 'idle',
  CONFIRMING: 'confirming',
  SUBMITTING: 'submitting',
  CONFIRMED: 'confirmed',
  RECONCILIATION_REQUIRED: 'reconciliation_required',
  REJECTED: 'rejected',
  FAILED: 'failed',
})

export const CLOSE_POSITION_EVENT = Object.freeze({
  REQUEST_CLOSE: 'request_close',
  CONFIRM: 'confirm',
  VERIFIED_SUCCESS: 'verified_success',
  UNCERTAIN_EXECUTION: 'uncertain_execution',
  EXCHANGE_REJECTED: 'exchange_rejected',
  OPERATION_FAILED: 'operation_failed',
  RETRY: 'retry',
  CLOSE_VALIDATION_FAILED: 'close_validation_failed',
  TARGET_CHANGED: 'target_changed',
  RECONCILIATION_CONFIRMED: 'reconciliation_confirmed',
  RECONCILIATION_REJECTED: 'reconciliation_rejected',
  RECONCILIATION_FAILED: 'reconciliation_failed',
  DISMISS: 'dismiss',
})

const S = CLOSE_POSITION_STATE
const E = CLOSE_POSITION_EVENT
export const UNCERTAIN_EXECUTION_DETAIL = 'Execution status is being reconciled. No new SELL will be submitted while the result is uncertain.'
const knownStates = new Set(Object.values(S))

const transitions = Object.freeze({
  [S.IDLE]: Object.freeze({ [E.REQUEST_CLOSE]: S.CONFIRMING }),
  [S.CONFIRMING]: Object.freeze({ [E.CONFIRM]: S.SUBMITTING, [E.DISMISS]: S.IDLE }),
  [S.SUBMITTING]: Object.freeze({
    [E.VERIFIED_SUCCESS]: S.CONFIRMED,
    [E.UNCERTAIN_EXECUTION]: S.RECONCILIATION_REQUIRED,
    [E.EXCHANGE_REJECTED]: S.REJECTED,
    [E.OPERATION_FAILED]: S.FAILED,
    [E.CLOSE_VALIDATION_FAILED]: S.FAILED,
    [E.TARGET_CHANGED]: S.CONFIRMING,
  }),
  [S.CONFIRMED]: Object.freeze({ [E.DISMISS]: S.IDLE }),
  [S.RECONCILIATION_REQUIRED]: Object.freeze({
    [E.RECONCILIATION_CONFIRMED]: S.CONFIRMED,
    [E.RECONCILIATION_REJECTED]: S.REJECTED,
    [E.RECONCILIATION_FAILED]: S.FAILED,
    [E.DISMISS]: S.IDLE,
  }),
  [S.REJECTED]: Object.freeze({ [E.RETRY]: S.SUBMITTING, [E.DISMISS]: S.IDLE }),
  [S.FAILED]: Object.freeze({ [E.DISMISS]: S.IDLE }),
})

export function reduceClosePosition(state, event) {
  if (!knownStates.has(state)) throw new TypeError(`Unknown close-position state: ${String(state)}`)
  const stateTransitions = transitions[state]
  return Object.hasOwn(stateTransitions, event) ? stateTransitions[event] : state
}

export function isSameCloseTarget(target, row) {
  if (!target || !row) return false
  return target.external
    ? target.token_id != null && String(row.token_id) === String(target.token_id)
    : target.id != null && String(row.id) === String(target.id)
}

const isReconciliationRow = (row) => Boolean(row?.reconciliation_required)
  || row?.status === 'closing'
  || row?.status === 'reconciliation_required'

export function isReconciliationVisible(target, rows, currentTab) {
  return currentTab === 'open'
    && Array.isArray(rows)
    && rows.some((row) => isSameCloseTarget(target, row) && isReconciliationRow(row))
}

export function canDismissClosePosition(state, target, rows, currentTab, statusRefreshSucceeded = false) {
  if (!knownStates.has(state)) throw new TypeError(`Unknown close-position state: ${String(state)}`)
  return (state === S.RECONCILIATION_REQUIRED
      && statusRefreshSucceeded
      && isReconciliationVisible(target, rows, currentTab))
    || state === S.CONFIRMING
    || state === S.CONFIRMED
    || state === S.REJECTED
    || state === S.FAILED
}

export function dismissClosePosition(
  state,
  source,
  target,
  rows,
  currentTab,
  statusRefreshSucceeded = false,
) {
  if (source !== 'backdrop' && source !== 'escape') {
    throw new TypeError(`Unknown dismissal source: ${String(source)}`)
  }
  return canDismissClosePosition(state, target, rows, currentTab, statusRefreshSucceeded)
    ? reduceClosePosition(state, E.DISMISS)
    : state
}

function snapshotValue(value) {
  if (Array.isArray(value)) return Object.freeze(value.map(snapshotValue))
  if (value && typeof value === 'object') {
    return Object.freeze(Object.fromEntries(
      Object.entries(value).map(([key, child]) => [key, snapshotValue(child)]),
    ))
  }
  return value
}

export function createCloseTarget(position) {
  if (!position || typeof position !== 'object') throw new TypeError('A position is required to request close')
  return snapshotValue(position)
}

export function requestClosePosition(state, currentTarget, position) {
  const nextState = reduceClosePosition(state, E.REQUEST_CLOSE)
  if (nextState === state) return { state, target: currentTarget }
  return { state: nextState, target: createCloseTarget(position) }
}

export function findFreshCloseTarget(target, rows) {
  const row = Array.isArray(rows) ? rows.find((candidate) => isSameCloseTarget(target, candidate)) : null
  if (!row) return { ok: false, detail: 'Position is no longer present in Open positions. No SELL was submitted.' }
  const closeable = row.status === 'open'
    && !row.redeemable
    && !isReconciliationRow(row)
  if (!closeable) return { ok: false, detail: 'Position is no longer safely closeable. No SELL was submitted.' }
  return { ok: true, target: createCloseTarget(row) }
}

// Shares are exchange quantities, so only changes above one millionth of a
// share are actionable. Quote/value noise requires a value move exceeding the
// larger of $1 or 2% of the previously confirmed estimate.
export const CLOSE_SHARES_EPSILON = 1e-6
export const CLOSE_VALUE_RELATIVE_THRESHOLD = 0.02
export const CLOSE_VALUE_ABSOLUTE_THRESHOLD_USD = 1

export function hasMaterialCloseTargetChange(confirmed, fresh) {
  if (!confirmed || !fresh) return true
  if (String(confirmed.token_id ?? '') !== String(fresh.token_id ?? '')) return true
  if (!confirmed.external && String(confirmed.id ?? '') !== String(fresh.id ?? '')) return true
  const oldShares = Number(confirmed.shares)
  const newShares = Number(fresh.shares)
  if (!Number.isFinite(oldShares) || !Number.isFinite(newShares)) {
    return Number.isFinite(oldShares) !== Number.isFinite(newShares)
  }
  if (Math.abs(oldShares - newShares) > CLOSE_SHARES_EPSILON + Number.EPSILON * 100) return true
  const oldValue = oldShares * Number(confirmed.current_price)
  const newValue = newShares * Number(fresh.current_price)
  if (!Number.isFinite(oldValue) || !Number.isFinite(newValue)) {
    return Number.isFinite(oldValue) !== Number.isFinite(newValue)
  }
  const threshold = Math.max(
    CLOSE_VALUE_ABSOLUTE_THRESHOLD_USD,
    Math.abs(oldValue) * CLOSE_VALUE_RELATIVE_THRESHOLD,
  )
  return Math.abs(newValue - oldValue) > threshold + 1e-9
}

const isExplicitlyClosed = (row) => row?.status === 'closed' || row?.status === 'resolved'

export function closeTargetIdentity(target) {
  if (!target) return null
  if (target.external) {
    return target.token_id == null ? null : `external:${String(target.token_id)}`
  }
  return target.id == null ? null : `tracked:${String(target.id)}`
}

export function isReconciliationRequestCurrent(request, current) {
  const requestIdentity = closeTargetIdentity(request?.target)
  return Boolean(request && current && requestIdentity)
    && request.generation === current.generation
    && request.refreshSequence === current.refreshSequence
    && request.state === S.RECONCILIATION_REQUIRED
    && current.state === S.RECONCILIATION_REQUIRED
    && requestIdentity === closeTargetIdentity(current.target)
}

export async function refreshReconciliationStatus({
  target,
  openPositions,
  closedPositions,
  updateRows = () => {},
  shouldApply = () => true,
}) {
  let rows
  try {
    rows = await openPositions()
  } catch (error) {
    return {
      ok: false,
      error,
      detail: 'Status refresh failed. Execution status remains uncertain.',
    }
  }

  if (!shouldApply()) return { ok: false, stale: true, detail: 'Stale status refresh ignored.' }
  updateRows(rows)

  const operationId = target?.external ? target.operation_position_id : target?.id
  const operationMatch = (row) => operationId != null
    && String(row?.id) === String(operationId)
    && (target?.token_id == null || String(row?.token_id) === String(target.token_id))
  const openMatch = Array.isArray(rows)
    ? rows.find((row) => target?.external && operationId != null
      ? operationMatch(row)
      : isSameCloseTarget(target, row))
    : null
  if (openMatch) {
    if (isReconciliationRow(openMatch)) {
      return {
        ok: true,
        resolution: 'reconciliation_required',
        allowDismiss: true,
        ...(target?.external && operationId == null && openMatch.id != null
          ? { operationPositionId: openMatch.id }
          : {}),
        rows,
        detail: UNCERTAIN_EXECUTION_DETAIL,
      }
    }
    if (findFreshCloseTarget(target, [openMatch]).ok) {
      return {
        ok: true,
        resolution: 'rejected',
        event: E.RECONCILIATION_REJECTED,
        rows,
        detail: 'Fresh Open positions verify this position is still open and closeable. No SELL occurred; it is safe to try closing again.',
      }
    }
    return {
      ok: true,
      resolution: 'uncertain',
      rows,
      detail: 'The position is visible but is not explicitly closeable or reconciling. Execution status remains uncertain.',
    }
  }

  let closedRows
  try {
    closedRows = await closedPositions()
  } catch (error) {
    return {
      ok: false,
      error,
      rows,
      detail: 'Closed positions could not be read. Execution status remains uncertain.',
    }
  }
  if (!shouldApply()) return { ok: false, stale: true, detail: 'Stale status refresh ignored.' }

  const closedMatch = Array.isArray(closedRows)
    ? closedRows.find((row) => operationMatch(row) && isExplicitlyClosed(row))
    : null
  if (closedMatch) {
    return {
      ok: true,
      resolution: 'confirmed',
      event: E.RECONCILIATION_CONFIRMED,
      rows,
      closedRows,
      detail: 'Verified closed in Closed positions. No additional SELL was submitted.',
    }
  }
  return {
    ok: true,
    resolution: 'uncertain',
    rows,
    closedRows,
    detail: 'The position is absent from both Open and Closed positions. Execution status remains uncertain.',
  }
}

export function createCloseSubmissionGuard() {
  let active = false
  return Object.freeze({
    async run(operation) {
      if (active) return { accepted: false }
      active = true
      try {
        return { accepted: true, value: await operation() }
      } finally {
        active = false
      }
    },
  })
}

async function executeCloseAttempt({
  target,
  slippage,
  closeTracked,
  closeExternal,
  haptic,
  refresh,
}) {
  let response
  try {
    response = target.external
      ? await closeExternal(target.token_id, slippage)
      : await closeTracked(target.id, slippage)
  } catch (error) {
    return {
      event: E.UNCERTAIN_EXECUTION,
      detail: UNCERTAIN_EXECUTION_DETAIL,
      error,
    }
  }

  if (response?.ok === true) {
    try { await haptic('success') } catch { /* cosmetic */ }
    let refreshError
    try { await refresh() } catch (error) { refreshError = error }
    return {
      event: E.VERIFIED_SUCCESS,
      detail: refreshError
        ? 'CLOSED ✓ Position refresh unavailable; reload to update the list.'
        : 'CLOSED ✓',
      response,
      ...(refreshError ? { refreshError } : {}),
    }
  }

  if (response?.ok === false && response.reconciliation_required === false) {
    return {
      event: E.EXCHANGE_REJECTED,
      detail: response.reason || 'Close rejected before execution.',
      response,
    }
  }

  return {
    event: E.UNCERTAIN_EXECUTION,
    detail: UNCERTAIN_EXECUTION_DETAIL,
    response,
    ...(target.external && response?.position_id != null
      ? { target: createCloseTarget({ ...target, operation_position_id: response.position_id }) }
      : {}),
  }
}

export function executeFreshCloseAttempt({
  guard,
  openPositions,
  updateRows = () => {},
  updateTarget = () => {},
  ...dependencies
}) {
  return guard.run(async () => {
    let rows
    try {
      rows = await openPositions()
    } catch (error) {
      return {
        event: E.CLOSE_VALIDATION_FAILED,
        detail: 'Could not refresh Open positions. No SELL was submitted; refresh status before trying again.',
        error,
      }
    }
    updateRows(rows)
    const fresh = findFreshCloseTarget(dependencies.target, rows)
    if (!fresh.ok) return { event: E.CLOSE_VALIDATION_FAILED, detail: fresh.detail }
    updateTarget(fresh.target)
    if (hasMaterialCloseTargetChange(dependencies.target, fresh.target)) {
      return {
        event: E.TARGET_CHANGED,
        detail: 'Position changed. Review the updated quantity and confirm again.',
        target: fresh.target,
      }
    }
    const result = await executeCloseAttempt({ ...dependencies, target: fresh.target })
    return { ...result, target: result.target || fresh.target }
  })
}
