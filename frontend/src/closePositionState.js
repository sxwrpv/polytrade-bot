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
