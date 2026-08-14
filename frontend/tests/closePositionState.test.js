import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  CLOSE_POSITION_EVENT,
  CLOSE_POSITION_STATE,
  canDismissClosePosition,
  createCloseSubmissionGuard,
  dismissClosePosition,
  executeCloseSubmission,
  reduceClosePosition,
  requestClosePosition,
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

test('request captures an immutable target snapshot that polling cannot mutate or replace', () => {
  const source = {
    id: 41,
    token_id: 'token-original',
    external: false,
    market_title: 'Original market',
    shares: 12.5,
    current_price: 0.61,
  }
  const requested = requestClosePosition(S.IDLE, null, source)
  assert.equal(requested.state, S.CONFIRMING)
  assert.equal(Object.isFrozen(requested.target), true)

  source.market_title = 'Mutated by polling'
  source.shares = 99
  assert.equal(requested.target.market_title, 'Original market')
  assert.equal(requested.target.shares, 12.5)

  const replacement = { ...source, id: 42, market_title: 'Replacement row' }
  const ignored = requestClosePosition(requested.state, requested.target, replacement)
  assert.equal(ignored.state, S.CONFIRMING)
  assert.strictEqual(ignored.target, requested.target)
  assert.equal(ignored.target.id, 41)
})

test('submission guard permits only one concurrent close API call', async () => {
  const guard = createCloseSubmissionGuard()
  let release
  const pending = new Promise((resolve) => { release = resolve })
  let calls = 0
  const deps = {
    guard,
    target: { id: 7, external: false },
    slippage: 2,
    closeTracked: async () => { calls += 1; await pending; return { ok: true } },
    closeExternal: async () => { throw new Error('wrong endpoint') },
    haptic: () => {},
    refresh: () => {},
  }

  const first = executeCloseSubmission(deps)
  const second = await executeCloseSubmission(deps)
  assert.equal(second.accepted, false)
  assert.equal(calls, 1)
  release()
  assert.equal((await first).accepted, true)
  assert.equal(calls, 1)
})

test('successful close uses the selected endpoint and refreshes exactly once without timers', async () => {
  const events = []
  const originalSetTimeout = globalThis.setTimeout
  globalThis.setTimeout = () => { throw new Error('close success must not depend on a timer') }
  try {
    const result = await executeCloseSubmission({
      guard: createCloseSubmissionGuard(),
      target: { token_id: 'external-token', external: true },
      slippage: 3.5,
      closeTracked: async () => { throw new Error('wrong endpoint') },
      closeExternal: async (tokenId, slippage) => {
        events.push(['api', tokenId, slippage])
        return { ok: true }
      },
      haptic: (kind) => events.push(['haptic', kind]),
      refresh: () => events.push(['refresh']),
    })

    assert.equal(result.accepted, true)
    assert.equal(result.value.event, E.VERIFIED_SUCCESS)
    assert.deepEqual(events, [
      ['api', 'external-token', 3.5],
      ['haptic', 'success'],
      ['refresh'],
    ])
  } finally {
    globalThis.setTimeout = originalSetTimeout
  }
})

const submissionBase = {
  target: { id: 7, external: false },
  slippage: 2,
  closeExternal: async () => ({ ok: true }),
  haptic: () => {},
  refresh: () => {},
}

test('only an explicit safe server rejection is retryable', async () => {
  const uncertain = await executeCloseSubmission({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    closeTracked: async () => ({ ok: false, reconciliation_required: true, reason: 'check' }),
  })
  assert.equal(uncertain.value.event, E.UNCERTAIN_EXECUTION)
  assert.match(uncertain.value.detail, /status unknown/i)
  assert.match(uncertain.value.detail, /reconcil/i)

  const rejected = await executeCloseSubmission({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    closeTracked: async () => ({
      ok: false,
      reconciliation_required: false,
      reason: 'no liquidity',
    }),
  })
  assert.equal(rejected.value.event, E.EXCHANGE_REJECTED)
  assert.equal(rejected.value.detail, 'no liquidity')

  const ambiguousNonOk = await executeCloseSubmission({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    closeTracked: async () => ({ ok: false, reason: 'request interrupted' }),
  })
  assert.equal(ambiguousNonOk.value.event, E.UNCERTAIN_EXECUTION)
  assert.match(ambiguousNonOk.value.detail, /status unknown/i)
})

test('a thrown close client error is uncertain and cannot enter a retry state', async () => {
  const result = await executeCloseSubmission({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    closeTracked: async () => { throw new Error('network down') },
  })
  assert.equal(result.value.event, E.UNCERTAIN_EXECUTION)
  assert.match(result.value.detail, /status unknown/i)
  assert.match(result.value.detail, /reconcil/i)

  const state = reduceClosePosition(S.SUBMITTING, result.value.event)
  assert.equal(state, S.RECONCILIATION_REQUIRED)
  assert.equal(reduceClosePosition(state, E.RETRY), S.RECONCILIATION_REQUIRED)
})

test('throwing haptic cannot overwrite verified close success and refresh still runs once', async () => {
  let refreshCalls = 0
  const result = await executeCloseSubmission({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    closeTracked: async () => ({ ok: true }),
    haptic: () => { throw new Error('haptic unavailable') },
    refresh: () => { refreshCalls += 1 },
  })

  assert.equal(result.value.event, E.VERIFIED_SUCCESS)
  assert.equal(reduceClosePosition(S.SUBMITTING, result.value.event), S.CONFIRMED)
  assert.equal(refreshCalls, 1)
})

test('throwing refresh cannot overwrite verified close success and is attempted once', async () => {
  let refreshCalls = 0
  const result = await executeCloseSubmission({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    closeTracked: async () => ({ ok: true }),
    refresh: async () => {
      refreshCalls += 1
      throw new Error('positions unavailable')
    },
  })

  assert.equal(result.value.event, E.VERIFIED_SUCCESS)
  assert.equal(reduceClosePosition(S.SUBMITTING, result.value.event), S.CONFIRMED)
  assert.equal(refreshCalls, 1)
  assert.match(result.value.detail, /closed/i)
  assert.match(result.value.detail, /refresh/i)
  assert.doesNotMatch(result.value.detail, /execution.*(failed|unknown)/i)
})

test('PositionCard source is presentational and delegates close requests', async () => {
  const source = await readFile(new URL('../src/components/PositionCard.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /useState|\bapi\b|\bhaptic\b|<Modal|setTimeout/)
  assert.match(source, /onRequestClose\(p\)/)
})
