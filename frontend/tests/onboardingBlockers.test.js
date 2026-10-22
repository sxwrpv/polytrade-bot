import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { acceptFundingAndLoadAddresses } from '../src/fundingAcknowledgement.js'
import { bootstrapLaunch } from '../src/authBootstrap.js'
import {
  DRAFT_TTL_MS, describeStatus, groupAddress, isAddressShape, lastFourMatches, launchAge,
  loadDraft, newIdempotencyKey, normalizeAmount, saveDraft, stageState,
} from '../src/withdrawForm.js'


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


const DEST = '0x3c3C3c3C3c3C3C3c3C3c3c3c3c3C3C3c3c3C3c3C'

test('withdraw form accepts whole-cent amounts and well-formed addresses only', () => {
  assert.equal(normalizeAmount('25.5'), '25.50')
  assert.equal(normalizeAmount('$2'), '2.00')
  for (const bad of ['', '0', '-1', '2.001', 'abc', '1e3', '.5']) {
    assert.equal(normalizeAmount(bad), null, bad)
  }
  assert.equal(isAddressShape(` ${DEST} `), true)
  assert.equal(isAddressShape(DEST.slice(0, 41)), false)
  assert.equal(isAddressShape(`${DEST}00`), false)
})

test('the confirm step needs the address last 4 retyped, in any case', () => {
  assert.equal(lastFourMatches(DEST, DEST.slice(-4).toUpperCase()), true)
  assert.equal(lastFourMatches(DEST, ` ${DEST.slice(-4)} `), true)
  assert.equal(lastFourMatches(DEST, '0000'), false)
  assert.equal(lastFourMatches('', ''), false)
  assert.deepEqual(groupAddress(DEST).slice(0, 2), ['0x', DEST.slice(2, 6)])
  assert.equal(groupAddress(DEST).length, 11)
})

test('a withdrawal needs Telegram launch data and its age is known', () => {
  const now = 1_800_000_000_000
  assert.equal(launchAge(null, now), null)
  assert.equal(launchAge({ initData: '', initDataUnsafe: { auth_date: 1 } }, now), null)
  assert.equal(launchAge({ initData: 'signed', initDataUnsafe: { auth_date: now / 1000 - 250 } }, now), 250)
})

test('one idempotency key per confirm screen, in the shape the server accepts', () => {
  const key = newIdempotencyKey()
  assert.match(key, /^[A-Za-z0-9_-]{8,64}$/)
  assert.notEqual(newIdempotencyKey(), key)
  const fallback = newIdempotencyKey({ getRandomValues: (bytes) => bytes.fill(7) })
  assert.equal(fallback, '07'.repeat(16))
})

test('the draft survives a relaunch for half an hour', () => {
  const store = new Map()
  const storage = { setItem: (k, v) => store.set(k, v), getItem: (k) => store.get(k) ?? null }
  saveDraft(storage, { destination: DEST, amount: '12.00' }, 1000)
  assert.deepEqual(loadDraft(storage, 1000 + DRAFT_TTL_MS), { destination: DEST, amount: '12.00' })
  assert.equal(loadDraft(storage, 1001 + DRAFT_TTL_MS), null)
  assert.equal(loadDraft({ getItem: () => { throw new Error('blocked') } }), null)
})

test('withdrawal statuses read as three stages', () => {
  const sending = describeStatus({ status: 'submitted', amount: '5.00' })
  assert.deepEqual([sending.step, sending.tone, stageState(1, sending), stageState(2, sending)],
    [1, 'processing', 'active', 'todo'])
  const delivered = describeStatus({ status: 'confirmed', amount: '5.00', delivered: '4.99', destination: DEST })
  assert.deepEqual([1, 2, 3].map((s) => stageState(s, delivered)), ['done', 'done', 'done'])
  assert.match(delivered.text, /4\.99 USDC arrived/)
  const failed = describeStatus({ status: 'failed', amount: '5.00', error: 'Not sent: the relayer is busy.' })
  assert.equal(failed.tone, 'error')
  assert.match(failed.text, /stayed in your PolyTrade wallet/)
  const held = describeStatus({ status: 'submitting', amount: '5.00', error: 'Relayer: reverted',
    held_until: '2026-10-22T13:30:00+00:00' })
  assert.equal(held.tone, 'warn')
  assert.equal(describeStatus({ status: 'attention', amount: '5.00' }).label, 'Needs a check')
})

test('withdrawing sends the Telegram step-up proof and the confirm details', async () => {
  const root = new URL('../', import.meta.url)
  const [api, panel, user] = await Promise.all([
    readFile(new URL('src/api.js', root), 'utf8'),
    readFile(new URL('src/components/Withdraw.jsx', root), 'utf8'),
    readFile(new URL('src/pages/User.jsx', root), 'utf8'),
  ])
  assert.match(api, /createWithdrawal: \(body, initData\)/)
  assert.match(api, /init_data: initData/)
  assert.match(panel, /window\.Telegram\?\.WebApp\?\.initData/)
  assert.match(panel, /confirm_last4: typed\.trim\(\)/)
  assert.match(panel, /idempotency_key: key\.current/)
  assert.match(panel, /disabled=\{!matches \|\| sending\}/)
  assert.match(user, /withdrawals_enabled && \(/)
})

test('withdrawal wording follows the server flag and leaves the redemption wording alone', async () => {
  const root = new URL('../', import.meta.url)
  const sources = await Promise.all(['src/pages/PublicHome.jsx', 'src/pages/Onboarding.jsx',
    'src/components/DepositAddresses.jsx'].map((p) => readFile(new URL(p, root), 'utf8')))
  for (const source of sources) {
    assert.match(source, /useWithdrawStatus\(\)/)
    assert.match(source, /withdrawals\.open \?/)
    assert.match(source, /redeem\.auto/)
    assert.match(source, /USDC on Polygon/)
  }
})
