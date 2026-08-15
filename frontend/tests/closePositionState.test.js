import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  CLOSE_POSITION_EVENT,
  CLOSE_POSITION_STATE,
  canDismissClosePosition,
  createCloseSubmissionGuard,
  dismissClosePosition,
  executeFreshCloseAttempt,
  findFreshCloseTarget,
  hasMaterialCloseTargetChange,
  isReconciliationRequestCurrent,
  isReconciliationVisible,
  refreshReconciliationStatus,
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

test('retry is allowed only from a known-safe rejected state', () => {
  assert.equal(reduceClosePosition(S.REJECTED, E.RETRY), S.SUBMITTING)

  for (const state of [
    S.IDLE,
    S.CONFIRMING,
    S.SUBMITTING,
    S.CONFIRMED,
    S.RECONCILIATION_REQUIRED,
    S.FAILED,
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
      E.CLOSE_VALIDATION_FAILED,
      E.TARGET_CHANGED,
    ]),
    [S.CONFIRMED]: new Set([E.DISMISS]),
    [S.RECONCILIATION_REQUIRED]: new Set([
      E.RECONCILIATION_CONFIRMED,
      E.RECONCILIATION_REJECTED,
      E.RECONCILIATION_FAILED,
      E.DISMISS,
    ]),
    [S.REJECTED]: new Set([E.RETRY, E.DISMISS]),
    [S.FAILED]: new Set([E.DISMISS]),
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
    openPositions: async () => [{ id: 7, status: 'open', external: false }],
    updateRows: () => {},
    closeTracked: async () => { calls += 1; await pending; return { ok: true } },
    closeExternal: async () => { throw new Error('wrong endpoint') },
    haptic: () => {},
    refresh: () => {},
  }

  const first = executeFreshCloseAttempt(deps)
  const second = await executeFreshCloseAttempt(deps)
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
    const result = await executeFreshCloseAttempt({
      guard: createCloseSubmissionGuard(),
      target: { token_id: 'external-token', external: true },
      slippage: 3.5,
      openPositions: async () => [{ token_id: 'external-token', status: 'open', external: true }],
      updateRows: () => {},
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
  openPositions: async () => [{ id: 7, status: 'open', external: false }],
  updateRows: () => {},
  closeExternal: async () => ({ ok: true }),
  haptic: () => {},
  refresh: () => {},
}

test('only an explicit safe server rejection is retryable', async () => {
  const uncertain = await executeFreshCloseAttempt({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    closeTracked: async () => ({ ok: false, reconciliation_required: true, reason: 'check' }),
  })
  assert.equal(uncertain.value.event, E.UNCERTAIN_EXECUTION)
  assert.match(uncertain.value.detail, /^Execution status is being reconciled\./)
  assert.match(uncertain.value.detail, /reconcil/i)

  const rejected = await executeFreshCloseAttempt({
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

  const ambiguousNonOk = await executeFreshCloseAttempt({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    closeTracked: async () => ({ ok: false, reason: 'request interrupted' }),
  })
  assert.equal(ambiguousNonOk.value.event, E.UNCERTAIN_EXECUTION)
  assert.match(ambiguousNonOk.value.detail, /^Execution status is being reconciled\./)
})

test('a thrown close client error is uncertain and cannot enter a retry state', async () => {
  const result = await executeFreshCloseAttempt({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    closeTracked: async () => { throw new Error('network down') },
  })
  assert.equal(result.value.event, E.UNCERTAIN_EXECUTION)
  assert.match(result.value.detail, /^Execution status is being reconciled\./)
  assert.match(result.value.detail, /reconcil/i)

  const state = reduceClosePosition(S.SUBMITTING, result.value.event)
  assert.equal(state, S.RECONCILIATION_REQUIRED)
  assert.equal(reduceClosePosition(state, E.RETRY), S.RECONCILIATION_REQUIRED)
})

test('throwing haptic cannot overwrite verified close success and refresh still runs once', async () => {
  let refreshCalls = 0
  const result = await executeFreshCloseAttempt({
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
  const result = await executeFreshCloseAttempt({
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

test('uncertain close copy leads exactly and never claims no order was submitted', async () => {
  for (const closeTracked of [
    async () => ({ ok: false, reconciliation_required: true }),
    async () => { throw new Error('response lost') },
  ]) {
    const result = await executeFreshCloseAttempt({
      ...submissionBase,
      guard: createCloseSubmissionGuard(),
      closeTracked,
    })
    assert.match(result.value.detail, /^Execution status is being reconciled\./)
    assert.match(result.value.detail, /No new SELL will be submitted/)
    assert.doesNotMatch(result.value.detail, /no order was submitted/i)
  }
})

test('targeted reconciliation refresh preserves a visibly marked Open target and never submits', async () => {
  const calls = []
  const fresh = [{ id: 7, status: 'reconciliation_required' }]
  const result = await refreshReconciliationStatus({
    target: { id: 7, external: false },
    openPositions: async () => { calls.push('read'); return fresh },
    closedPositions: async () => { throw new Error('must not read Closed') },
    updateRows: (rows) => calls.push(['update', rows]),
  })
  assert.deepEqual(calls, ['read', ['update', fresh]])
  assert.equal(result.ok, true)
  assert.equal(result.resolution, 'reconciliation_required')
  assert.equal(result.allowDismiss, true)
  assert.equal(result.event, undefined)
})

test('failed reconciliation status refresh is truthful and cannot submit a SELL', async () => {
  let closeCalls = 0
  const result = await refreshReconciliationStatus({
    target: { id: 7, external: false },
    openPositions: async () => { throw new Error('positions unavailable') },
    closedPositions: async () => [],
    updateRows: () => { throw new Error('must not update on failed read') },
    closeTracked: () => { closeCalls += 1 },
  })
  assert.equal(closeCalls, 0)
  assert.equal(result.ok, false)
  assert.match(result.detail, /status refresh failed/i)
  assert.doesNotMatch(result.detail, /order (failed|rejected)/i)
})

test('explicitly open reconciliation target resolves rejected with a truthful safe-retry path', async () => {
  let sells = 0
  const result = await refreshReconciliationStatus({
    target: { id: 7, external: false },
    openPositions: async () => [{ id: 7, status: 'open', reconciliation_required: false }],
    closedPositions: async () => { throw new Error('must not read Closed') },
    closeTracked: () => { sells += 1 },
  })
  assert.equal(sells, 0)
  assert.equal(result.event, E.RECONCILIATION_REJECTED)
  assert.equal(result.resolution, 'rejected')
  assert.match(result.detail, /No SELL occurred/i)
  assert.match(result.detail, /safe to try closing again/i)
})

test('explicit Closed evidence resolves confirmed by tracked id or external operation position id', async () => {
  for (const [target, closedRow] of [
    [{ id: 7, external: false }, { id: 7, status: 'closed' }],
    [{ token_id: 'wallet-token', external: true, operation_position_id: 88 }, { id: 88, token_id: 'wallet-token', status: 'resolved' }],
  ]) {
    const result = await refreshReconciliationStatus({
      target,
      openPositions: async () => [],
      closedPositions: async () => [closedRow],
    })
    assert.equal(result.event, E.RECONCILIATION_CONFIRMED)
    assert.equal(result.resolution, 'confirmed')
    assert.match(result.detail, /Verified closed/i)
  }
})

test('absence from both reads and a failed required Closed read remain uncertain', async () => {
  const absent = await refreshReconciliationStatus({
    target: { id: 7, external: false },
    openPositions: async () => [],
    closedPositions: async () => [],
  })
  assert.equal(absent.resolution, 'uncertain')
  assert.equal(absent.event, undefined)
  assert.match(absent.detail, /remains uncertain/i)

  const failed = await refreshReconciliationStatus({
    target: { id: 7, external: false },
    openPositions: async () => [],
    closedPositions: async () => { throw new Error('offline') },
  })
  assert.equal(failed.ok, false)
  assert.equal(failed.event, undefined)
  assert.match(failed.detail, /remains uncertain/i)
})

test('stale reconciliation refresh cannot update or resolve another modal target', async () => {
  let release
  const pending = new Promise((resolve) => { release = resolve })
  const request = { generation: 1, state: S.RECONCILIATION_REQUIRED, target: { id: 7, external: false } }
  let current = { ...request }
  const updates = []
  const refresh = refreshReconciliationStatus({
    target: request.target,
    openPositions: async () => { await pending; return [{ id: 7, status: 'open' }] },
    closedPositions: async () => [],
    updateRows: (rows) => updates.push(rows),
    shouldApply: () => isReconciliationRequestCurrent(request, current),
  })
  current = { generation: 2, state: S.RECONCILIATION_REQUIRED, target: { id: 8, external: false } }
  release()
  const result = await refresh
  assert.equal(result.stale, true)
  assert.equal(result.event, undefined)
  assert.deepEqual(updates, [])
})

test('reconciliation dismissal requires the same marked target visibly preserved on Open', () => {
  const tracked = { id: 7, token_id: 't', external: false }
  const external = { token_id: 'wallet-token', external: true }
  const markedTracked = [{ id: 7, token_id: 't', status: 'closing' }]
  const markedExternal = [{ id: 'synthetic', token_id: 'wallet-token', reconciliation_required: true }]

  assert.equal(isReconciliationVisible(tracked, markedTracked, 'closed'), false)
  assert.equal(isReconciliationVisible(tracked, [{ ...tracked, status: 'open' }], 'open'), false)
  assert.equal(isReconciliationVisible(tracked, markedTracked, 'open'), true)
  assert.equal(isReconciliationVisible(external, markedExternal, 'open'), true)
  assert.equal(canDismissClosePosition(S.RECONCILIATION_REQUIRED, tracked, markedTracked, 'closed'), false)
  assert.equal(canDismissClosePosition(S.RECONCILIATION_REQUIRED, tracked, markedTracked, 'open'), false)
  assert.equal(canDismissClosePosition(S.RECONCILIATION_REQUIRED, tracked, markedTracked, 'open', true), true)
  assert.equal(
    dismissClosePosition(S.RECONCILIATION_REQUIRED, 'backdrop', tracked, markedTracked, 'open', true),
    S.IDLE,
  )
})

test('material share change refreshes immutable confirmation and submits no SELL', async () => {
  const events = []
  const stale = { id: 7, token_id: 't', status: 'open', shares: 10, external: false }
  const fresh = { ...stale, shares: 4, current_price: 0.42 }
  const result = await executeFreshCloseAttempt({
    guard: createCloseSubmissionGuard(),
    target: stale,
    slippage: 2,
    openPositions: async () => { events.push('read'); return [fresh] },
    updateRows: (rows) => events.push(['rows', rows[0].shares]),
    updateTarget: (target) => events.push(['snapshot', target.shares]),
    closeTracked: async (id) => { events.push(['sell', id]); return { ok: true } },
    closeExternal: async () => { throw new Error('wrong endpoint') },
    haptic: () => {},
    refresh: () => {},
  })
  assert.deepEqual(events, ['read', ['rows', 4], ['snapshot', 4]])
  assert.equal(result.value.event, E.TARGET_CHANGED)
  assert.match(result.value.detail, /Position changed\. Review the updated quantity and confirm again\./)
  assert.equal(result.value.target.shares, 4)
  assert.notStrictEqual(result.value.target, fresh)
})

test('missing, null-status, unknown-status, or non-closeable fresh target causes zero SELL submissions', async () => {
  for (const rows of [
    [],
    [{ id: 7 }],
    [{ id: 7, status: null }],
    [{ id: 7, status: 'mystery' }],
    [{ id: 7, status: 'closing' }],
    [{ id: 7, status: 'reconciliation_required' }],
  ]) {
    let closeCalls = 0
    const result = await executeFreshCloseAttempt({
      guard: createCloseSubmissionGuard(),
      target: { id: 7, external: false },
      slippage: 2,
      openPositions: async () => rows,
      updateRows: () => {},
      closeTracked: async () => { closeCalls += 1; return { ok: true } },
      closeExternal: async () => { closeCalls += 1; return { ok: true } },
      haptic: () => {},
      refresh: () => {},
    })
    assert.equal(closeCalls, 0)
    assert.equal(result.value.event, E.CLOSE_VALIDATION_FAILED)
    assert.equal(reduceClosePosition(S.SUBMITTING, result.value.event), S.FAILED)
    assert.match(result.value.detail, /No SELL was submitted/i)
  }
})

test('failed Open-position read causes zero SELL submissions and a non-retry refresh status', async () => {
  let closeCalls = 0
  const result = await executeFreshCloseAttempt({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    openPositions: async () => { throw new Error('offline') },
    closeTracked: async () => { closeCalls += 1; return { ok: true } },
  })
  assert.equal(closeCalls, 0)
  assert.equal(result.value.event, E.CLOSE_VALIDATION_FAILED)
  assert.equal(reduceClosePosition(S.SUBMITTING, result.value.event), S.FAILED)
  assert.match(result.value.detail, /refresh Open positions/i)
})

test('rejected dismiss, reopen, and confirm re-confirms a materially changed target before SELL', async () => {
  let state = dismissClosePosition(S.REJECTED, 'backdrop')
  const requested = requestClosePosition(state, null, { id: 7, shares: 10, external: false })
  state = reduceClosePosition(requested.state, E.CONFIRM)
  const events = []
  const result = await executeFreshCloseAttempt({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    target: requested.target,
    openPositions: async () => {
      events.push('read')
      return [{ id: 7, shares: 3, current_price: 0.4, status: 'open' }]
    },
    updateRows: () => events.push('rows'),
    updateTarget: (target) => events.push(['snapshot', target.shares, target.current_price]),
    closeTracked: async () => { events.push('sell'); return { ok: true } },
  })
  assert.equal(state, S.SUBMITTING)
  assert.deepEqual(events, ['read', 'rows', ['snapshot', 3, 0.4]])
  assert.equal(result.value.event, E.TARGET_CHANGED)
})

test('rejected dismiss, reopen, and confirm cannot SELL a missing or closing target', async () => {
  for (const rows of [[], [{ id: 7, status: 'closing' }]]) {
    const idle = dismissClosePosition(S.REJECTED, 'backdrop')
    const requested = requestClosePosition(idle, null, { id: 7, external: false })
    const submitting = reduceClosePosition(requested.state, E.CONFIRM)
    let closeCalls = 0
    const result = await executeFreshCloseAttempt({
      ...submissionBase,
      guard: createCloseSubmissionGuard(),
      target: requested.target,
      openPositions: async () => rows,
      closeTracked: async () => { closeCalls += 1; return { ok: true } },
    })
    assert.equal(submitting, S.SUBMITTING)
    assert.equal(closeCalls, 0)
    assert.equal(reduceClosePosition(submitting, result.value.event), S.FAILED)
  }
})

test('initial confirming close snapshots material fresh shares and price before reconfirmation', async () => {
  const requested = requestClosePosition(S.IDLE, null, {
    id: 19,
    shares: 20,
    current_price: 0.1,
    external: false,
  })
  const submitting = reduceClosePosition(requested.state, E.CONFIRM)
  const events = []
  const result = await executeFreshCloseAttempt({
    ...submissionBase,
    guard: createCloseSubmissionGuard(),
    target: requested.target,
    openPositions: async () => {
      events.push('read')
      return [{ id: 19, shares: 8, current_price: 0.55, status: 'open', external: false }]
    },
    updateTarget: (target) => events.push(['snapshot', target.shares, target.current_price]),
    closeTracked: async (id) => { events.push(['sell', id]); return { ok: true } },
  })
  assert.equal(submitting, S.SUBMITTING)
  assert.deepEqual(events, ['read', ['snapshot', 8, 0.55]])
  assert.equal(result.value.event, E.TARGET_CHANGED)
  assert.equal(result.value.target.shares, 8)
  assert.equal(result.value.target.current_price, 0.55)
  assert.equal(Object.isFrozen(result.value.target), true)
})

test('failed dismiss, reopen, and confirm cannot SELL when fresh validation fails', async () => {
  for (const openPositions of [
    async () => [],
    async () => [{ id: 7, status: 'closing' }],
    async () => { throw new Error('offline') },
  ]) {
    let state = dismissClosePosition(S.FAILED, 'escape')
    const requested = requestClosePosition(state, null, { id: 7, external: false })
    state = reduceClosePosition(requested.state, E.CONFIRM)
    let closeCalls = 0
    const result = await executeFreshCloseAttempt({
      ...submissionBase,
      guard: createCloseSubmissionGuard(),
      target: requested.target,
      openPositions,
      closeTracked: async () => { closeCalls += 1; return { ok: true } },
    })
    assert.equal(state, S.SUBMITTING)
    assert.equal(closeCalls, 0)
    assert.equal(reduceClosePosition(state, result.value.event), S.FAILED)
  }
})

test('fresh close target matching uses tracked id and external token id', () => {
  assert.equal(findFreshCloseTarget({ id: 2, external: false }, [{ id: 2, status: 'open' }]).ok, true)
  assert.equal(findFreshCloseTarget(
    { id: 'old', token_id: 'x', external: true },
    [{ id: 'new', token_id: 'x', external: true, status: 'open' }],
  ).ok, true)
})

test('two same-target refreshes completing out of order apply only the latest result', async () => {
  let releaseOld
  const oldPending = new Promise((resolve) => { releaseOld = resolve })
  const target = { id: 7, external: false }
  let current = { generation: 1, refreshSequence: 1, state: S.RECONCILIATION_REQUIRED, target }
  const updates = []
  const oldRequest = { ...current }
  const oldRefresh = refreshReconciliationStatus({
    target,
    openPositions: async () => { await oldPending; return [{ id: 7, status: 'closing', marker: 'old' }] },
    closedPositions: async () => [], updateRows: (rows) => updates.push(rows[0].marker),
    shouldApply: () => isReconciliationRequestCurrent(oldRequest, current),
  })
  current = { ...current, refreshSequence: 2 }
  const newRequest = { ...current }
  const newest = await refreshReconciliationStatus({
    target,
    openPositions: async () => [{ id: 7, status: 'open', marker: 'new' }],
    closedPositions: async () => [], updateRows: (rows) => updates.push(rows[0].marker),
    shouldApply: () => isReconciliationRequestCurrent(newRequest, current),
  })
  releaseOld()
  const old = await oldRefresh
  assert.equal(newest.resolution, 'rejected')
  assert.equal(old.stale, true)
  assert.deepEqual(updates, ['new'])
})

test('old same-token Closed history cannot confirm an unknown new external close', async () => {
  const result = await refreshReconciliationStatus({
    target: { token_id: 'wallet-token', external: true },
    openPositions: async () => [],
    closedPositions: async () => [{ id: 'old-close', token_id: 'wallet-token', status: 'closed' }],
  })
  assert.equal(result.resolution, 'uncertain')
  assert.equal(result.event, undefined)
})

test('a unique marked active external row supplies the operation position id', async () => {
  const result = await refreshReconciliationStatus({
    target: { token_id: 'wallet-token', external: true },
    openPositions: async () => [{ id: 'new-close', token_id: 'wallet-token', status: 'closing' }],
    closedPositions: async () => { throw new Error('must not read Closed') },
  })
  assert.equal(result.resolution, 'reconciliation_required')
  assert.equal(result.operationPositionId, 'new-close')
})

test('material target thresholds avoid quote noise and require reconfirmation at boundaries', () => {
  const base = { id: 7, token_id: 't', external: false, shares: 100, current_price: 0.5 }
  assert.equal(hasMaterialCloseTargetChange(base, { ...base, shares: 100.000001 }), false)
  assert.equal(hasMaterialCloseTargetChange(base, { ...base, shares: 100.000002 }), true)
  // Estimated-value changes must exceed the larger of $1 and 2% of the prior estimate.
  assert.equal(hasMaterialCloseTargetChange(base, { ...base, current_price: 0.51 }), false)
  assert.equal(hasMaterialCloseTargetChange(base, { ...base, current_price: 0.510001 }), true)
  assert.equal(hasMaterialCloseTargetChange(base, { ...base, token_id: 'other' }), true)
})

test('second confirmation with unchanged refreshed target submits exactly once', async () => {
  const guard = createCloseSubmissionGuard()
  const original = { id: 7, token_id: 't', external: false, status: 'open', shares: 10, current_price: .5 }
  const updated = { ...original, shares: 8 }
  let sells = 0
  const deps = {
    guard, target: original, slippage: 2,
    openPositions: async () => [updated], updateRows: () => {}, updateTarget: () => {},
    closeTracked: async () => { sells += 1; return { ok: true, position_id: 7 } },
    closeExternal: async () => { throw new Error('wrong endpoint') }, haptic: () => {}, refresh: () => {},
  }
  const first = await executeFreshCloseAttempt(deps)
  assert.equal(first.value.event, E.TARGET_CHANGED)
  assert.equal(sells, 0)
  const second = await executeFreshCloseAttempt({ ...deps, target: first.value.target })
  assert.equal(second.value.event, E.VERIFIED_SUCCESS)
  assert.equal(sells, 1)
})

test('uncertain external response preserves its durable operation position id', async () => {
  const result = await executeFreshCloseAttempt({
    guard: createCloseSubmissionGuard(),
    target: { token_id: 't', external: true, status: 'open', shares: 2, current_price: .5 },
    slippage: 2,
    openPositions: async () => [{ token_id: 't', external: true, status: 'open', shares: 2, current_price: .5 }],
    closeTracked: async () => { throw new Error('wrong endpoint') },
    closeExternal: async () => ({ ok: false, reconciliation_required: true, position_id: 'operation-1' }),
    haptic: () => {}, refresh: () => {},
  })
  assert.equal(result.value.event, E.UNCERTAIN_EXECUTION)
  assert.equal(result.value.target.operation_position_id, 'operation-1')
})

test('PositionCard resolved copy requires redemption on polymarket.com', async () => {
  const source = await readFile(new URL('../src/components/PositionCard.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /winnings redeem automatically/i)
  assert.match(source, /must be redeemed on polymarket\.com/i)
})
