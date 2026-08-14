import test from 'node:test'
import assert from 'node:assert/strict'

import { bootstrapSession } from '../src/authBootstrap.js'

test('re-authenticates with Telegram when cached address exists but cookie is invalid', async () => {
  const saved = []
  let cleared = 0
  const unauthorized = Object.assign(new Error('invalid session'), { status: 401 })
  const api = {
    me: async () => { throw unauthorized },
    telegramAuth: async (initData) => {
      assert.equal(initData, 'signed-init-data')
      return { address: '0xlinked' }
    },
  }

  const result = await bootstrapSession({
    cachedSession: { address: '0xstale' },
    initData: 'signed-init-data',
    api,
    saveSession: (session) => saved.push(session),
    clearSession: () => { cleared += 1 },
  })

  assert.deepEqual(result, { address: '0xlinked' })
  assert.deepEqual(saved, [{ address: '0xlinked' }])
  assert.equal(cleared, 0)
})
