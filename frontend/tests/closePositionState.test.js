import test from 'node:test'
import assert from 'node:assert/strict'

import {
  CLOSE_POSITION_EVENT,
  CLOSE_POSITION_STATE,
  canDismissClosePosition,
  dismissClosePosition,
  reduceClosePosition,
} from '../src/closePositionState.js'

const S = CLOSE_POSITION_STATE
const E = CLOSE_POSITION_EVENT

test('follows the verified-success close path', () => {
  assert.equal(reduceClosePosition(S.IDLE, E.REQUEST_CLOSE), S.CONFIRMING)
  assert.equal(reduceClosePosition(S.CONFIRMING, E.CONFIRM), S.SUBMITTING)
  assert.equal(reduceClosePosition(S.SUBMITTING, E.VERIFIED_SUCCESS), S.CONFIRMED)
})

test('uncertain execution requires reconciliation', () => {
  assert.equal(
    reduceClosePosition(S.SUBMITTING, E.UNCERTAIN_EXECUTION),
    S.RECONCILIATION_REQUIRED,
  )
})

test('known exchange rejection is distinct from execution uncertainty', () => {
  assert.equal(reduceClosePosition(S.SUBMITTING, E.EXCHANGE_REJECTED), S.REJECTED)
})

test('transport or server failure is distinct from exchange rejection', () => {
  assert.equal(reduceClosePosition(S.SUBMITTING, E.OPERATION_FAILED), S.FAILED)
})

test('retry is allowed only from known-safe rejected and failed states', () => {
  assert.equal(reduceClosePosition(S.REJECTED, E.RETRY), S.SUBMITTING)
  assert.equal(reduceClosePosition(S.FAILED, E.RETRY), S.SUBMITTING)

  for (const state of [
    S.IDLE,
    S.CONFIRMING,
    S.SUBMITTING,
    S.CONFIRMED,
    S.RECONCILIATION_REQUIRED,
  ]) {
    assert.equal(reduceClosePosition(state, E.RETRY), state, `retry from ${state}`)
  }
})

test('external reconciliation, not manual retry, resolves uncertain execution', () => {
  assert.equal(
    reduceClosePosition(S.RECONCILIATION_REQUIRED, E.RECONCILIATION_CONFIRMED),
    S.CONFIRMED,
  )
  assert.equal(
    reduceClosePosition(S.RECONCILIATION_REQUIRED, E.RECONCILIATION_REJECTED),
    S.REJECTED,
  )
  assert.equal(
    reduceClosePosition(S.RECONCILIATION_REQUIRED, E.RECONCILIATION_FAILED),
    S.FAILED,
  )
})

test('backdrop and Escape dismissal are ignored while submitting', () => {
  assert.equal(canDismissClosePosition(S.SUBMITTING), false)
  assert.equal(dismissClosePosition(S.SUBMITTING, 'backdrop'), S.SUBMITTING)
  assert.equal(dismissClosePosition(S.SUBMITTING, 'escape'), S.SUBMITTING)
})

test('backdrop and Escape dismissal are ignored while reconciliation is required', () => {
  assert.equal(canDismissClosePosition(S.RECONCILIATION_REQUIRED), false)
  assert.equal(
    dismissClosePosition(S.RECONCILIATION_REQUIRED, 'backdrop'),
    S.RECONCILIATION_REQUIRED,
  )
  assert.equal(
    dismissClosePosition(S.RECONCILIATION_REQUIRED, 'escape'),
    S.RECONCILIATION_REQUIRED,
  )
})

test('dismissal behavior is explicit for every other state', () => {
  assert.equal(canDismissClosePosition(S.IDLE), false)
  assert.equal(dismissClosePosition(S.IDLE, 'escape'), S.IDLE)

  for (const state of [S.CONFIRMING, S.CONFIRMED, S.REJECTED, S.FAILED]) {
    assert.equal(canDismissClosePosition(state), true, `can dismiss ${state}`)
    assert.equal(dismissClosePosition(state, 'backdrop'), S.IDLE, `backdrop from ${state}`)
    assert.equal(dismissClosePosition(state, 'escape'), S.IDLE, `Escape from ${state}`)
  }
})

test('all unrelated events fail closed without changing state', () => {
  const validTransitionByState = {
    [S.IDLE]: new Set([E.REQUEST_CLOSE]),
    [S.CONFIRMING]: new Set([E.CONFIRM, E.DISMISS]),
    [S.SUBMITTING]: new Set([
      E.VERIFIED_SUCCESS,
      E.UNCERTAIN_EXECUTION,
      E.EXCHANGE_REJECTED,
      E.OPERATION_FAILED,
    ]),
    [S.CONFIRMED]: new Set([E.DISMISS]),
    [S.RECONCILIATION_REQUIRED]: new Set([
      E.RECONCILIATION_CONFIRMED,
      E.RECONCILIATION_REJECTED,
      E.RECONCILIATION_FAILED,
    ]),
    [S.REJECTED]: new Set([E.RETRY, E.DISMISS]),
    [S.FAILED]: new Set([E.RETRY, E.DISMISS]),
  }

  for (const state of Object.values(S)) {
    for (const event of Object.values(E)) {
      if (!validTransitionByState[state].has(event)) {
        assert.equal(reduceClosePosition(state, event), state, `${state} + ${event}`)
      }
    }
  }
  assert.equal(reduceClosePosition(S.CONFIRMING, 'unknown-event'), S.CONFIRMING)
})

test('prototype-key events preserve every current state', () => {
  for (const state of Object.values(S)) {
    for (const event of ['constructor', 'toString', '__proto__']) {
      assert.equal(
        reduceClosePosition(state, event),
        state,
        `${state} + ${event} must not return an inherited property`,
      )
    }
  }
})

test('unknown states and unsupported dismissal sources are rejected clearly', () => {
  assert.throws(() => reduceClosePosition('mystery', E.CONFIRM), /Unknown close-position state/)
  assert.throws(() => canDismissClosePosition('mystery'), /Unknown close-position state/)
  assert.throws(
    () => dismissClosePosition('mystery', 'backdrop'),
    /Unknown close-position state/,
  )
  assert.throws(() => dismissClosePosition(S.CONFIRMING, 'button'), /Unknown dismissal source/)
})
