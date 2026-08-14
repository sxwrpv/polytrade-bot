import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { acceptFundingAndLoadAddresses } from '../src/fundingAcknowledgement.js'
import { bootstrapLaunch } from '../src/authBootstrap.js'


test('deposit addresses are requested only after server accepts durable funding acknowledgement', async () => {
  const calls = []
  const result = await acceptFundingAndLoadAddresses({
    api: {
      acknowledgeFunding: async (body) => {
        calls.push(['ack', body])
        return { accepted: true, version: '2026-08-14' }
      },
      depositAddress: async () => {
        calls.push(['addresses'])
        return { addresses: [{ chain: 'evm', address: '0xbridge' }] }
      },
    },
    version: '2026-08-14',
  })
  assert.deepEqual(calls, [
    ['ack', { accepted: true, version: '2026-08-14' }],
    ['addresses'],
  ])
  assert.equal(result.addresses[0].address, '0xbridge')
})


test('failed server acknowledgement never requests or reveals deposit addresses', async () => {
  let addressCalls = 0
  await assert.rejects(() => acceptFundingAndLoadAddresses({
    api: {
      acknowledgeFunding: async () => { throw new Error('ack rejected') },
      depositAddress: async () => { addressCalls += 1; return { addresses: [] } },
    },
    version: '2026-08-14',
  }), /ack rejected/)
  assert.equal(addressCalls, 0)
})


test('Telegram bootstrap offers explicit recovery when an unlinked identity has a valid legacy cookie', async () => {
  const result = await bootstrapLaunch({
    cachedSession: { address: '0xlegacy' },
    initData: 'signed-telegram',
    api: {
      telegramAuth: async () => ({ address: null, linked: false }),
      me: async () => ({ address: '0xlegacy', telegram_linked: false }),
    },
    saveSession() {},
    clearSession() {},
  })
  assert.deepEqual(result, {
    mode: 'legacy-link',
    session: { address: '0xlegacy' },
    initData: 'signed-telegram',
  })
})


test('funding UI does not mount address loader before explicit acknowledgement', async () => {
  const root = new URL('../', import.meta.url)
  const [user, deposits] = await Promise.all([
    readFile(new URL('src/pages/User.jsx', root), 'utf8'),
    readFile(new URL('src/components/DepositAddresses.jsx', root), 'utf8'),
  ])
  assert.doesNotMatch(user, /<DepositAddresses\s+gasless=/)
  assert.match(user, /FundingAccess/)
  assert.match(deposits, /acceptFundingAndLoadAddresses/)
  assert.match(deposits.toLowerCase(), /before you fund/)
})


test('onboarding success copy distinguishes an existing linked wallet from a new one', async () => {
  const source = await readFile(new URL('../src/pages/Onboarding.jsx', import.meta.url), 'utf8')
  assert.match(source, /result\.created/)
  assert.match(source, /WALLET CREATED/)
  assert.match(source, /WALLET FOUND/)
  assert.match(source, /already linked/i)
})
