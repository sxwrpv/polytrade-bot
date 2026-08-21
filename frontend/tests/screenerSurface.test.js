import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  DEFAULT_FILTERS,
  DEFAULT_PERIOD,
  DEFAULT_SORT,
  PERIODS,
  SORTS,
  SUPPORTS_WALLET_DEEP_LINK,
  botDeepLink,
  buildPublicQuery,
  coverageLabel,
  formatMetric,
  walletRows,
} from '../src/screener/screenerModel.js'

const root = new URL('../', import.meta.url)
const read = (path) => readFile(new URL(path, root), 'utf8')

test('the public query is period-aware and omits inactive filters', () => {
  assert.equal(DEFAULT_PERIOD, '30d')
  assert.equal(DEFAULT_SORT, 'pnl')
  assert.deepEqual(PERIODS, ['7d', '30d', '90d'])

  assert.deepEqual(buildPublicQuery(), { period: '30d', sort: 'pnl', limit: 50 })
  assert.deepEqual(
    buildPublicQuery({ period: '7d', sort: 'volume', search: ' 0xABC ' }),
    { period: '7d', sort: 'volume', limit: 50, search: '0xABC' },
  )
  // Blank and non-finite filter values are dropped, not sent as zero.
  assert.deepEqual(
    buildPublicQuery({ filters: { ...DEFAULT_FILTERS, pnlMin: '', winrateMin: 'abc' } }),
    { period: '30d', sort: 'pnl', limit: 50 },
  )
  // Win rate is a percentage in the UI and a fraction on the wire.
  assert.equal(buildPublicQuery({ filters: { ...DEFAULT_FILTERS, winrateMin: '60' } }).winrate_min, 0.6)
  assert.equal(buildPublicQuery({ filters: { ...DEFAULT_FILTERS, pnlMin: '0' } }).pnl_min, 0)
  assert.equal(
    buildPublicQuery({ completeHistoryOnly: true }).complete_history_only, true,
  )
})

test('a rejected sort or period never silently becomes a different query', () => {
  assert.throws(() => buildPublicQuery({ period: '1y' }), /period/)
  assert.throws(() => buildPublicQuery({ sort: 'copyability' }), /sort/)
  assert.deepEqual(SORTS.map(([key]) => key), ['pnl', 'winrate', 'volume'])
})

test('an unavailable metric renders as unavailable, never as zero', () => {
  assert.equal(formatMetric(null, 'money'), '—')
  assert.equal(formatMetric(undefined, 'percent'), '—')
  assert.equal(formatMetric(Number.NaN, 'count'), '—')
  // A real zero is still a real zero.
  assert.equal(formatMetric(0, 'money'), '$0')
  assert.equal(formatMetric(0, 'count'), '0')
  assert.equal(formatMetric(0.62, 'percent'), '62%')
  assert.equal(formatMetric(-1234.5, 'money'), '-$1,235')
})

test('coverage states partial history honestly and never claims completeness', () => {
  assert.equal(coverageLabel({ history_days: null, period_days: 30 }), 'UNAVAILABLE')
  assert.equal(coverageLabel({ history_days: 6, period_days: 30, history_partial: true }),
    'PARTIAL · ~6D OF 30D')
  assert.equal(coverageLabel({ history_days: 30, period_days: 30, history_partial: false }),
    '~30D OBSERVED')
})

test('rows carry provenance and never fabricate an aggregate score', () => {
  const rows = walletRows({
    period: '30d',
    period_days: 30,
    wallets: [
      { address: '0xabc', pnl: 10, win_rate: null, volume: null, active_positions: null,
        history_days: null, history_partial: true, stats_refreshed_at: null },
    ],
  })

  assert.equal(rows.length, 1)
  assert.equal(rows[0].pnl, 10)
  assert.equal(rows[0].winRate, null)
  assert.equal(rows[0].activePositions, null)
  assert.equal(rows[0].coverage, 'UNAVAILABLE')
  for (const forbidden of ['score', 'copyability', 'rating', 'tier']) {
    assert.ok(!(forbidden in rows[0]), forbidden)
  }
})

test('copying a wallet hands off to Telegram without inventing a deep link', () => {
  const address = `0x${'a1'.repeat(20)}`

  // Nothing in this repository reads Telegram's `start` payload, so the link
  // must not pretend the selected wallet travels with it.
  assert.equal(SUPPORTS_WALLET_DEEP_LINK, false)
  assert.equal(botDeepLink(address), 'https://t.me/cpolytrade_bot')
  assert.equal(botDeepLink(null), 'https://t.me/cpolytrade_bot')
  assert.equal(botDeepLink('not-an-address'), 'https://t.me/cpolytrade_bot')
})

test('the page does not imply that a selected address carries into Telegram', async () => {
  const page = await read('src/screener/ScreenerPage.jsx')

  assert.ok(!page.includes('Copy this wallet in Telegram'),
    'that label implies the wallet is preselected, which it is not')
  assert.doesNotMatch(page, /paste/i)
  assert.match(page, /Adding a new copied wallet is not yet available/i)
})

test('the public client is anonymous and cannot mutate anything', async () => {
  const api = await read('src/screener/publicApi.js')

  // No cookies cross the wire: the public screener is anonymous, which is also
  // what lets it move to a subdomain without cross-site cookie relaxation.
  assert.match(api, /credentials:\s*'omit'/)
  assert.match(api, /\/public\/screener\//)
  // Read-only by construction.
  assert.doesNotMatch(api, /method:\s*'(POST|PUT|PATCH|DELETE)'/)
  for (const authed of ['/traders/', '/user/', '/positions/', '/auth/', '/telemetry/']) {
    assert.ok(!api.includes(authed), `public client must not call ${authed}`)
  }
})

test('the screener is a separate entry so it can move to its own host', async () => {
  const [config, html, entry] = await Promise.all([
    read('vite.config.js'),
    read('screener.html'),
    read('src/screener/main.jsx'),
  ])

  assert.match(config, /rollupOptions/)
  assert.match(config, /screener\.html/)
  assert.match(html, /src\/screener\/main\.jsx/)
  assert.match(entry, /ScreenerPage/)
  // The API origin is configurable, so the same bundle serves both
  // polytradebot.live/screener and screener.polytradebot.live.
  const api = await read('src/screener/publicApi.js')
  assert.match(api, /VITE_API_BASE/)
})

test('the public screener page never renders account state or financial actions', async () => {
  const page = await read('src/screener/ScreenerPage.jsx')

  for (const forbidden of [
    'api.follow', 'api.me(', 'createWallet', 'closePosition', 'balance',
    'CONFIRM COPY', 'depositAddress',
  ]) {
    assert.ok(!page.includes(forbidden), `public screener must not reference ${forbidden}`)
  }
  // The primary action is a hand-off, not a trade.
  assert.match(page, /botDeepLink/)
})

test('the screener explains its data and uses the restrained button system', async () => {
  const [page, css] = await Promise.all([
    read('src/screener/ScreenerPage.jsx'),
    read('src/styles/screener.css'),
  ])

  assert.match(page, /ANALYZE/)
  assert.match(page, /provenance/i)
  // The analyze action uses the shared .btn system, which is now the
  // documentation green — not the old neon fill.
  assert.doesNotMatch(css, /#3ddc84/i)
  assert.doesNotMatch(css, /backdrop-filter/)
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/)
  assert.match(css, /@media \(max-width/)
})

test('the Mini App home contains only copied wallets and a link to the standalone screener', async () => {
  const home = await read('src/pages/Home.jsx')

  assert.doesNotMatch(home, /<WalletScreener/)
  assert.match(home, /href="\/screener"/)
  assert.match(home, /CopiedWallets/)

  for (const removed of [
    'AddWalletByAddress',
    'TraderCard',
    'COPY A WALLET BY ADDRESS',
    'GettingStarted',
    'KpiStrip',
    'api.trader(',
  ]) {
    assert.ok(!home.includes(removed), `Mini App home must not include ${removed}`)
  }
})

test('the results table scrolls itself and never drags the page sideways', async () => {
  const css = await read('src/styles/screener.css')

  const scroller = css.match(/\.table-scroll\s*{[^}]*}/)[0]
  assert.match(scroller, /overflow-x:\s*auto/)
  // The visually-hidden <caption> is absolutely positioned; without a
  // positioned ancestor it escapes the overflow clip and extends the
  // document's scroll width, making the whole page pan at mobile widths.
  assert.match(scroller, /position:\s*relative/)
})

test('the standalone screener does not promise an in-app address lookup that no longer exists', async () => {
  const [home, page] = await Promise.all([
    read('src/pages/Home.jsx'),
    read('src/screener/ScreenerPage.jsx'),
  ])

  assert.doesNotMatch(home, /AddWalletByAddress|api\.trader\(/)
  assert.doesNotMatch(page, /Copy a wallet by address|look up any wallet directly/i)
  assert.match(page, /Adding a new copied wallet is not yet available/i)
})
