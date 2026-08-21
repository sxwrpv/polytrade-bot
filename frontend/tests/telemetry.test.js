import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  buildCloseOutcomeTelemetry,
  buildTelemetryEvent,
  getTelemetrySessionId,
  sendTelemetry,
} from '../src/telemetry.js'

const SESSION = '123e4567-e89b-42d3-a456-426614174000'

const memoryStorage = () => {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }
}

const readSource = (path) => readFile(new URL(path, import.meta.url), 'utf8')

test('opaque session id is generated once and reused from session storage', () => {
  const storage = memoryStorage()
  const cryptoApi = { randomUUID: () => SESSION }
  assert.equal(getTelemetrySessionId(storage, cryptoApi), SESSION)
  cryptoApi.randomUUID = () => { throw new Error('must not regenerate') }
  assert.equal(getTelemetrySessionId(storage, cryptoApi), SESSION)
})

test('payload builder retains only per-event aggregate properties', () => {
  const event = buildTelemetryEvent('screener_search_submitted', {
    query_kind: 'address',
    period: '30d',
    active_filters: true,
    query: '0x' + 'a'.repeat(40),
    wallet: '0x' + 'b'.repeat(40),
    private_key: 'secret',
    token: 'secret',
    initData: 'secret',
    user_id: 'secret',
  }, SESSION)

  assert.deepEqual(event, {
    session_id: SESSION,
    event_name: 'screener_search_submitted',
    properties: { query_kind: 'address', period: '30d', active_filters: true },
  })
  assert.doesNotMatch(JSON.stringify(event), /0x[a-f0-9]{40}|private|secret|initData|user_id/i)
  assert.equal(buildTelemetryEvent('unknown', {}, SESSION), null)
})

test('telemetry delivery is fire-and-forget safe when the network rejects', async () => {
  const storage = memoryStorage()
  const result = await sendTelemetry(
    'period_changed',
    { period: '7d', source: 'screener' },
    {
      storage,
      cryptoApi: { randomUUID: () => SESSION },
      send: async () => { throw new Error('offline') },
    },
  )
  assert.equal(result, false)
})

test('uncertain close keeps timing alive until reconciliation emits a final outcome', () => {
  assert.deepEqual(buildCloseOutcomeTelemetry('uncertain_execution', 1000, 1250), {
    eventName: 'close_reconciliation_required',
    properties: { duration_ms: 250 },
    terminal: false,
  })
  assert.deepEqual(buildCloseOutcomeTelemetry('reconciliation_confirmed', 1000, 1600), {
    eventName: 'close_confirmed',
    properties: { duration_ms: 600 },
    terminal: true,
  })
  assert.deepEqual(buildCloseOutcomeTelemetry('reconciliation_rejected', 1000, 1700), {
    eventName: 'close_rejected',
    properties: { duration_ms: 700 },
    terminal: true,
  })
  assert.deepEqual(buildCloseOutcomeTelemetry('reconciliation_failed', 1000, 1800), {
    eventName: 'close_failed',
    properties: { duration_ms: 800 },
    terminal: true,
  })
  assert.equal(buildCloseOutcomeTelemetry('target_changed', 1000, 1200), null)
})

test('Release C telemetry hooks contain no wallet identifiers', async () => {
  // The screener's three events (screener_search_submitted, period_changed,
  // advanced_filters_opened) were emitted only by the in-app WalletScreener,
  // removed when the standalone /screener took over. The public screener is
  // anonymous and deliberately emits no telemetry, so there is no source file
  // left to assert against — their allowlist entries are covered below.
  const card = await readSource('../src/components/TraderCard.jsx')
  const positions = await readSource('../src/pages/Positions.jsx')
  const telemetry = await readSource('../src/telemetry.js')

  assert.match(card, /wallet_analysis_opened/)
  assert.match(card, /copy_settings_opened/)
  for (const event of ['close_modal_opened', 'close_submitted', 'modal_dismissed']) {
    assert.match(positions, new RegExp(event))
  }
  assert.match(positions, /buildCloseOutcomeTelemetry/)
  for (const event of [
    'close_confirmed', 'close_rejected', 'close_reconciliation_required', 'close_failed',
  ]) assert.match(telemetry, new RegExp(event))

  // TraderCard and Positions are now the only emitters left in src/ — the
  // screener's call sites went with the retired in-app component.
  const telemetryCalls = [
    ...card.matchAll(/trackTelemetry\([\s\S]*?\)/g),
    ...positions.matchAll(/trackTelemetry\([\s\S]*?\)/g),
  ]
  assert.ok(telemetryCalls.length >= 6)
  for (const call of telemetryCalls) {
    assert.doesNotMatch(
      call[0],
      /(?:\{|,)\s*(?:address|wallet|token_id|position_id|private_key|initData|user_id|query)\s*:/i,
    )
  }
})
