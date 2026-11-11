import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { loadVersion } from '../src/serverVersion.js'
import { loadRedeemStatus } from '../src/redeemStatus.js'
import { loadWithdrawStatus } from '../src/withdrawStatus.js'

// The modules share one /api/version read for the life of the process, so the
// steps below run in order against a fetch the test answers by hand.
const calls = []
globalThis.fetch = (url) => new Promise((resolve, reject) => { calls.push({ url, resolve, reject }) })
const answer = (body) => ({ ok: true, status: 200, json: async () => body })

const MANUAL = { auto: false, losses: false, every_minutes: null }
const CLOSED = { open: false }

test('redeem and withdraw wording share one /api/version request', async () => {
  const redeem = loadRedeemStatus()
  const withdrawals = loadWithdrawStatus()
  const again = loadRedeemStatus()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, '/api/version')

  // The read fails: both fall back to the wording that is always true.
  calls[0].reject(new TypeError('Failed to fetch'))
  assert.deepEqual(await redeem, MANUAL)
  assert.deepEqual(await withdrawals, CLOSED)
  assert.deepEqual(await again, MANUAL)
  assert.equal(calls.length, 1)
})

test('a failed read is forgotten, so the next caller asks the server again', async () => {
  const redeem = loadRedeemStatus()
  const withdrawals = loadWithdrawStatus()
  assert.equal(calls.length, 2)

  calls[1].resolve(answer({
    version: 'e51225b',
    redeem: { auto: true, losses: true, every_minutes: 20 },
    withdrawals: { open: true },
  }))
  assert.deepEqual(await redeem, { auto: true, losses: true, every_minutes: 20 })
  assert.deepEqual(await withdrawals, { open: true })
})

test('a good read is kept and serves every later caller', async () => {
  assert.deepEqual(await loadRedeemStatus(), { auto: true, losses: true, every_minutes: 20 })
  assert.deepEqual(await loadWithdrawStatus(), { open: true })
  assert.equal(loadVersion(), loadVersion())
  assert.equal(calls.length, 2)
})

test('an error status from /api/version is retried the same way', async () => {
  // A fresh copy of the module, with its own memo.
  const fresh = await import('../src/serverVersion.js?fresh')
  const before = calls.length
  const first = fresh.loadVersion()
  calls[before].resolve({ ok: false, status: 503, statusText: 'Service Unavailable', json: async () => ({}) })
  await assert.rejects(first, /Service Unavailable/)
  const second = fresh.loadVersion()
  assert.notEqual(second, first)
  assert.equal(calls.length, before + 2)
  calls[before + 1].resolve(answer({ redeem: { auto: true } }))
  assert.deepEqual(await second, { redeem: { auto: true } })
  assert.equal(fresh.loadVersion(), second)
})

test('neither status module keeps a read of its own', () => {
  for (const file of ['../src/redeemStatus.js', '../src/withdrawStatus.js']) {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8')
    assert.match(source, /import \{ loadVersion \} from '\.\/serverVersion\.js'/)
    assert.doesNotMatch(source, /api\.version\(\)/)
    assert.doesNotMatch(source, /let pending/)
  }
})
