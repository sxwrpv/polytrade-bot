import test from 'node:test'
import assert from 'node:assert/strict'

import * as apiModule from '../src/api.js'

const { api } = apiModule

// One failed response from the server, as fetch would deliver it.
async function failure(status, statusText, body) {
  const original = globalThis.fetch
  globalThis.fetch = async () => ({
    ok: false,
    status,
    statusText,
    json: async () => { if (body === undefined) throw new SyntaxError('no JSON'); return body },
  })
  try {
    await api.followSettings('0xabc', { copy_ratio_pct: 25 })
  } catch (error) {
    return error
  } finally {
    globalThis.fetch = original
  }
  throw new Error('the request was expected to fail')
}

test('a FastAPI validation error reads as its messages', async () => {
  const error = await failure(422, 'Unprocessable Entity', {
    detail: [
      { type: 'less_than_equal', loc: ['body', 'copy_ratio_pct'], msg: 'Input should be less than or equal to 20', input: 25 },
      { type: 'greater_than_equal', loc: ['body', 'max_position_usd'], msg: 'Input should be greater than or equal to 1', input: 0 },
    ],
  })
  assert.equal(error.message, 'Input should be less than or equal to 20; Input should be greater than or equal to 1')
  assert.equal(error.status, 422)
  assert.doesNotMatch(error.message, /\[object Object\]/)
})

test('a string detail is passed through unchanged', async () => {
  const error = await failure(400, 'Bad Request', { detail: 'Already following this wallet' })
  assert.equal(error.message, 'Already following this wallet')
  assert.equal(error.status, 400)
})

test('a list of plain strings is joined as well', async () => {
  const error = await failure(422, 'Unprocessable Entity', { detail: ['first', 'second'] })
  assert.equal(error.message, 'first; second')
})

test('a missing, empty or unreadable detail falls back to the status text', async () => {
  for (const body of [{}, { detail: [] }, { detail: [{ loc: ['body'] }] }, null, undefined]) {
    const error = await failure(502, 'Bad Gateway', body)
    assert.equal(error.message, 'Bad Gateway', JSON.stringify(body))
    assert.equal(error.status, 502)
  }
})

test('the unused getWallet export is gone and the session helpers remain', () => {
  assert.equal('getWallet' in apiModule, false)
  for (const name of ['getSession', 'saveSession', 'clearWallet', 'api', 'haptic']) {
    assert.ok(name in apiModule, name)
  }
})
