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
  RECONCILIATION_CONFIRMED: 'reconciliation_confirmed',
  RECONCILIATION_REJECTED: 'reconciliation_rejected',
  RECONCILIATION_FAILED: 'reconciliation_failed',
  DISMISS: 'dismiss',
})

const S = CLOSE_POSITION_STATE
const E = CLOSE_POSITION_EVENT
const knownStates = new Set(Object.values(S))

const transitions = Object.freeze({
  [S.IDLE]: Object.freeze({
    [E.REQUEST_CLOSE]: S.CONFIRMING,
  }),
  [S.CONFIRMING]: Object.freeze({
    [E.CONFIRM]: S.SUBMITTING,
    [E.DISMISS]: S.IDLE,
  }),
  [S.SUBMITTING]: Object.freeze({
    [E.VERIFIED_SUCCESS]: S.CONFIRMED,
    [E.UNCERTAIN_EXECUTION]: S.RECONCILIATION_REQUIRED,
    [E.EXCHANGE_REJECTED]: S.REJECTED,
    [E.OPERATION_FAILED]: S.FAILED,
  }),
  [S.CONFIRMED]: Object.freeze({
    [E.DISMISS]: S.IDLE,
  }),
  [S.RECONCILIATION_REQUIRED]: Object.freeze({
    [E.RECONCILIATION_CONFIRMED]: S.CONFIRMED,
    [E.RECONCILIATION_REJECTED]: S.REJECTED,
    [E.RECONCILIATION_FAILED]: S.FAILED,
  }),
  [S.REJECTED]: Object.freeze({
    [E.RETRY]: S.SUBMITTING,
    [E.DISMISS]: S.IDLE,
  }),
  [S.FAILED]: Object.freeze({
    [E.RETRY]: S.SUBMITTING,
    [E.DISMISS]: S.IDLE,
  }),
})

/**
 * Pure close-operation reducer. Unsupported events fail closed by preserving
 * the current state; unknown states throw so corrupted state cannot be hidden.
 */
export function reduceClosePosition(state, event) {
  if (!knownStates.has(state)) {
    throw new TypeError(`Unknown close-position state: ${String(state)}`)
  }
  const stateTransitions = transitions[state]
  return Object.hasOwn(stateTransitions, event) ? stateTransitions[event] : state
}

/** Whether a currently visible close-operation surface may be dismissed. */
export function canDismissClosePosition(state) {
  if (!knownStates.has(state)) {
    throw new TypeError(`Unknown close-position state: ${String(state)}`)
  }
  return state === S.CONFIRMING
    || state === S.CONFIRMED
    || state === S.REJECTED
    || state === S.FAILED
}

/** Apply a backdrop or Escape dismissal without coupling policy to a UI. */
export function dismissClosePosition(state, source) {
  if (source !== 'backdrop' && source !== 'escape') {
    throw new TypeError(`Unknown dismissal source: ${String(source)}`)
  }
  return canDismissClosePosition(state)
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

/**
 * Capture the row shown in a confirmation. A later polling response can update
 * or replace its source row without changing this operation's target.
 */
export function createCloseTarget(position) {
  if (!position || typeof position !== 'object') {
    throw new TypeError('A position is required to request close')
  }
  return snapshotValue(position)
}

/** Request a close only from idle; active operations retain their exact target. */
export function requestClosePosition(state, currentTarget, position) {
  const nextState = reduceClosePosition(state, E.REQUEST_CLOSE)
  if (nextState === state) return { state, target: currentTarget }
  return { state: nextState, target: createCloseTarget(position) }
}

/** A synchronous latch around an async operation, independent of React renders. */
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

/**
 * Execute one close attempt with injected side effects. API outcome
 * classification is completed before any post-success UI work, so a verified
 * server response can never be downgraded by haptic or refresh failures.
 */
export function executeCloseSubmission({
  guard,
  target,
  slippage,
  closeTracked,
  closeExternal,
  haptic,
  refresh,
}) {
  return guard.run(async () => {
    let response
    try {
      response = target.external
        ? await closeExternal(target.token_id, slippage)
        : await closeTracked(target.id, slippage)
    } catch (error) {
      // A transport exception can happen after the SELL reached the server.
      // Never invite a duplicate submission when execution is ambiguous.
      return {
        event: E.UNCERTAIN_EXECUTION,
        detail: 'Execution status unknown. The close is being reconciled; do not retry.',
        error,
      }
    }

    if (response?.ok === true) {
      try {
        await haptic('success')
      } catch {
        // Haptics are cosmetic and must not affect a verified financial result.
      }

      let refreshError
      try {
        await refresh()
      } catch (error) {
        refreshError = error
      }

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
      detail: 'Execution status unknown. The close is being reconciled; do not retry.',
      response,
    }
  })
}
