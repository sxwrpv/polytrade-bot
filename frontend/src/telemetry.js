import { api } from './api.js'

const STORAGE_KEY = 'polytrade.telemetry.session.v1'
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SOURCES = new Set(['positions'])
const STATES = new Set(['confirming', 'confirmed', 'rejected', 'reconciliation_required', 'failed'])

const EVENT_KEYS = Object.freeze({
  close_modal_opened: ['source'],
  close_submitted: ['source'],
  close_confirmed: ['duration_ms'],
  close_rejected: ['duration_ms'],
  close_reconciliation_required: ['duration_ms'],
  close_failed: ['duration_ms'],
  modal_dismissed: ['state', 'source'],
})

const validProperty = (key, value) => {
  if (key === 'source') return SOURCES.has(value)
  if (key === 'duration_ms') return Number.isInteger(value) && value >= 0 && value <= 3_600_000
  if (key === 'state') return STATES.has(value)
  return false
}

const CLOSE_OUTCOME_EVENTS = Object.freeze({
  verified_success: ['close_confirmed', true],
  exchange_rejected: ['close_rejected', true],
  uncertain_execution: ['close_reconciliation_required', false],
  operation_failed: ['close_failed', true],
  close_validation_failed: ['close_failed', true],
  reconciliation_confirmed: ['close_confirmed', true],
  reconciliation_rejected: ['close_rejected', true],
  reconciliation_failed: ['close_failed', true],
})

export function buildCloseOutcomeTelemetry(event, submittedAt, now = Date.now()) {
  const mapped = CLOSE_OUTCOME_EVENTS[event]
  if (!mapped || !Number.isFinite(submittedAt) || !Number.isFinite(now)) return null
  return {
    eventName: mapped[0],
    properties: {
      duration_ms: Math.max(0, Math.min(3_600_000, Math.round(now - submittedAt))),
    },
    terminal: mapped[1],
  }
}

export function getTelemetrySessionId(storage = globalThis.sessionStorage, cryptoApi = globalThis.crypto) {
  if (!storage || !cryptoApi?.randomUUID) return null
  try {
    const existing = storage.getItem(STORAGE_KEY)
    if (existing && UUID_RE.test(existing)) return existing
    const created = cryptoApi.randomUUID()
    if (!UUID_RE.test(created)) return null
    storage.setItem(STORAGE_KEY, created)
    return created
  } catch {
    return null
  }
}

export function buildTelemetryEvent(eventName, suppliedProperties, sessionId) {
  const keys = EVENT_KEYS[eventName]
  if (!keys || !UUID_RE.test(sessionId || '')) return null
  const source = suppliedProperties && typeof suppliedProperties === 'object'
    ? suppliedProperties
    : {}
  const properties = {}
  for (const key of keys) {
    const value = source[key]
    if (!validProperty(key, value)) return null
    properties[key] = value
  }
  return { session_id: sessionId, event_name: eventName, properties }
}

export async function sendTelemetry(eventName, properties, options = {}) {
  const storage = options.storage ?? globalThis.sessionStorage
  const cryptoApi = options.cryptoApi ?? globalThis.crypto
  const send = options.send ?? api.telemetryEvent
  const sessionId = getTelemetrySessionId(storage, cryptoApi)
  const payload = buildTelemetryEvent(eventName, properties, sessionId)
  if (!payload) return false
  try {
    await send(payload)
    return true
  } catch {
    return false
  }
}

export function trackTelemetry(eventName, properties) {
  void sendTelemetry(eventName, properties)
}
