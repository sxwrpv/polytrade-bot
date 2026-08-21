import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { bootstrapLaunch, performLogout } from '../src/authBootstrap.js'

const unauthorized = Object.assign(new Error('unauthorized'), { status: 401 })

test('regular browser without cached state probes cookie session once and shows public page on 401', async () => {
  let probes = 0
  const result = await bootstrapLaunch({
    cachedSession: null,
    initData: '',
    api: { me: async () => { probes += 1; throw unauthorized } },
    saveSession() {},
    clearSession() {},
  })
  assert.equal(probes, 1)
  assert.deepEqual(result, { mode: 'public', session: null })
})

test('stale cached browser session is cleared after one probe and goes public', async () => {
  let probes = 0
  let clears = 0
  const result = await bootstrapLaunch({
    cachedSession: { address: '0xstale' },
    initData: '',
    api: { me: async () => { probes += 1; throw unauthorized } },
    saveSession() {},
    clearSession: () => { clears += 1 },
  })
  assert.equal(probes, 1)
  assert.equal(clears, 1)
  assert.deepEqual(result, { mode: 'public', session: null })
})

test('regular browser recovers a valid cookie-only session', async () => {
  const saved = []
  const result = await bootstrapLaunch({
    cachedSession: null,
    initData: '',
    api: { me: async () => ({ address: '0xcookie' }) },
    saveSession: (session) => saved.push(session),
    clearSession() {},
  })
  assert.deepEqual(result, { mode: 'authenticated', session: { address: '0xcookie' } })
  assert.deepEqual(saved, [{ address: '0xcookie' }])
})

test('valid Telegram launch without a linked wallet enters Telegram onboarding', async () => {
  const result = await bootstrapLaunch({
    cachedSession: null,
    initData: 'signed',
    api: { telegramAuth: async () => ({ address: null, linked: false }) },
    saveSession() {},
    clearSession() {},
  })
  assert.deepEqual(result, { mode: 'telegram-onboarding', session: null })
})

test('invalid Telegram login enters retry state rather than browser onboarding', async () => {
  const result = await bootstrapLaunch({
    cachedSession: null,
    initData: 'expired',
    api: { telegramAuth: async () => { throw unauthorized } },
    saveSession() {},
    clearSession() {},
  })
  assert.equal(result.mode, 'telegram-error')
  assert.match(result.message, /Telegram/i)
})

test('valid Telegram linked account authenticates and stores public address', async () => {
  const saved = []
  const result = await bootstrapLaunch({
    cachedSession: null,
    initData: 'signed',
    api: { telegramAuth: async () => ({ address: '0xlinked' }) },
    saveSession: (session) => saved.push(session),
    clearSession() {},
  })
  assert.deepEqual(result, { mode: 'authenticated', session: { address: '0xlinked' } })
  assert.deepEqual(saved, [{ address: '0xlinked' }])
})

test('Telegram account B takes precedence over cached browser account A', async () => {
  const calls = []
  const saved = []
  const result = await bootstrapLaunch({
    cachedSession: { address: '0xaccount-a' },
    initData: 'telegram-account-b',
    api: {
      me: async () => { calls.push('me'); return { address: '0xaccount-a' } },
      telegramAuth: async (proof) => { calls.push(`telegram:${proof}`); return { address: '0xaccount-b' } },
    },
    saveSession: (session) => saved.push(session),
    clearSession() {},
  })
  assert.deepEqual(calls, ['telegram:telegram-account-b'])
  assert.deepEqual(result, { mode: 'authenticated', session: { address: '0xaccount-b' } })
  assert.deepEqual(saved, [{ address: '0xaccount-b' }])
})

test('trusted /me address overwrites a mismatched cached browser address', async () => {
  const saved = []
  const result = await bootstrapLaunch({
    cachedSession: { address: '0xstale-public-cache' },
    initData: '',
    api: { me: async () => ({ address: '0xtrusted-cookie-user' }) },
    saveSession: (session) => saved.push(session),
    clearSession() {},
  })
  assert.deepEqual(result, { mode: 'authenticated', session: { address: '0xtrusted-cookie-user' } })
  assert.deepEqual(saved, [{ address: '0xtrusted-cookie-user' }])
})

test('failed logout preserves authenticated client state and surfaces the error', async () => {
  let cleared = 0
  let loggedOut = 0
  await assert.rejects(() => performLogout({
    api: { logout: async () => { throw new Error('service unavailable') } },
    clearSession: () => { cleared += 1 },
    onLogout: () => { loggedOut += 1 },
  }), /service unavailable/)
  assert.equal(cleared, 0)
  assert.equal(loggedOut, 0)
})

test('successful logout clears public cache and changes UI state', async () => {
  const calls = []
  await performLogout({
    api: { logout: async () => { calls.push('server') } },
    clearSession: () => { calls.push('cache') },
    onLogout: () => { calls.push('ui') },
  })
  assert.deepEqual(calls, ['server', 'cache', 'ui'])
})

test('consumer copy and metadata include required disclosures and avoid overclaims', async () => {
  const root = new URL('../', import.meta.url)
  const [publicPage, onboarding, gettingStarted, deposits, html] = await Promise.all([
    readFile(new URL('src/pages/PublicHome.jsx', root), 'utf8'),
    readFile(new URL('src/pages/Onboarding.jsx', root), 'utf8'),
    readFile(new URL('src/components/GettingStarted.jsx', root), 'utf8'),
    readFile(new URL('src/components/DepositAddresses.jsx', root), 'utf8'),
    readFile(new URL('index.html', root), 'utf8'),
  ])
  const publicLower = publicPage.toLowerCase()
  for (const phrase of ['illustrative', 'real money', 'custodial', 'withdraw', 'eligib']) {
    assert.match(publicLower, new RegExp(phrase))
  }
  assert.match(publicPage, /https:\/\/t\.me\/cpolytrade_bot/)
  assert.match(onboarding.toLowerCase(), /acknowledge/)
  assert.match(onboarding.toLowerCase(), /security/)

  const guarded = `${publicPage}\n${onboarding}\n${gettingStarted}\n${deposits}`.toLowerCase()
  for (const banned of [
    'mirrors every trade',
    'any chain',
    'no matic ever',
    'trading approvals were prepared',
    'custodial wallet is ready',
  ]) {
    assert.doesNotMatch(guarded, new RegExp(banned))
  }
  assert.match(onboarding.toLowerCase(), /wallet created/)
  assert.match(onboarding.toLowerCase(), /setup.*may still be incomplete/)
  assert.match(html, /<title>PolyTrade/)
  assert.match(html, /rel="canonical"/)
  assert.match(html, /property="og:/)
  assert.match(html, /name="theme-color"/)
  assert.match(html, /<noscript>/)
})

test('material disclosures and app chrome expose accessibility semantics', async () => {
  const root = new URL('../', import.meta.url)
  const [app, publicPage, styles] = await Promise.all([
    readFile(new URL('src/App.jsx', root), 'utf8'),
    readFile(new URL('src/pages/PublicHome.jsx', root), 'utf8'),
    readFile(new URL('src/styles/brutalism.css', root), 'utf8'),
  ])
  assert.match(app, /aria-current=/)
  assert.match(app, /role="status"/)
  assert.match(app, /aria-live="polite"/)
  // The decorative icon cards are gone; the landing now explains itself with
  // diagrams. Same requirement, applied to what is actually on the page:
  // purely decorative glyphs stay hidden from assistive technology, and every
  // informative graphic is a labelled figure with a text equivalent.
  assert.match(publicPage, /<span aria-hidden="true">↗<\/span>/)
  const diagrams = publicPage.match(/<svg\b[^>]*>/g) || []
  assert.ok(diagrams.length >= 2)
  for (const opening of diagrams) {
    assert.match(opening, /role="img"/)
    assert.match(opening, /aria-labelledby=/)
  }
  assert.equal((publicPage.match(/<title id=/g) || []).length, diagrams.length)
  assert.equal((publicPage.match(/<desc id=/g) || []).length, diagrams.length)
  assert.match(styles, /\.onboard-terms p[^}]*font-size:\s*(?:1[4-9]|[2-9]\d)px/s)
  assert.match(styles, /\.security-check p[^}]*font-size:\s*(?:1[4-9]|[2-9]\d)px/s)
})
