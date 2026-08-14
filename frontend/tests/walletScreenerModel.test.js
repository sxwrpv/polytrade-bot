import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  DEFAULT_FILTERS,
  DEFAULT_PERIOD,
  DEFAULT_SORT,
  PERIODS,
  SORTS,
  activeFilterChips,
  buildScreenerQuery,
} from '../src/components/walletScreenerModel.js'

const readSource = (path) => readFile(new URL(path, import.meta.url), 'utf8')

const state = (overrides = {}) => ({
  period: DEFAULT_PERIOD,
  sort: DEFAULT_SORT,
  search: '',
  includePartialHistory: true,
  filters: { ...DEFAULT_FILTERS },
  ...overrides,
})

test('defaults expose only period-aware PNL, win rate, and volume sorting', () => {
  assert.equal(DEFAULT_PERIOD, '30d')
  assert.equal(DEFAULT_SORT, 'pnl')
  assert.deepEqual(PERIODS, ['7d', '30d', '90d'])
  assert.deepEqual(SORTS, [
    ['pnl', 'PNL'],
    ['winrate', 'WIN RATE'],
    ['volume', 'VOLUME'],
  ])
  assert.deepEqual(buildScreenerQuery(state()), { sort: 'pnl_30d', limit: 50 })
})

test('query construction maps every supported period and sort to a selected-period key', () => {
  for (const period of PERIODS) {
    for (const [sort] of SORTS) {
      const query = buildScreenerQuery(state({ period, sort }))
      assert.equal(query.sort, `${sort}_${period}`)
      assert.notEqual(query.sort, sort)
    }
  }
})

test('off, empty, and null basic filters emit no filter parameters', () => {
  for (const off of ['', null, undefined]) {
    const filters = {
      ...DEFAULT_FILTERS,
      pnlMin: off,
      winrateMin: off,
      volumeMin: off,
      consistencyRatioMin: off,
      fillExitMin: off,
      fillExitMax: off,
    }
    assert.deepEqual(buildScreenerQuery(state({ filters })), { sort: 'pnl_30d', limit: 50 })
  }
})

test('basic and advanced filters map to selected-period backend units', () => {
  const filters = {
    ...DEFAULT_FILTERS,
    pnlMin: '125',
    winrateMin: '65',
    volumeMin: '2500',
    consistencyRatioMin: '70',
    fillExitMin: '25',
    fillExitMax: '175',
  }
  assert.deepEqual(buildScreenerQuery(state({ period: '90d', search: ' alice ', filters })), {
    sort: 'pnl_90d',
    limit: 50,
    search: 'alice',
    pnl_90d_min: 125,
    winrate_90d_min: 0.65,
    volume_90d_min: 2500,
    consistency_ratio_90d_min: 0.7,
    fill_exit_ratio_90d_min: 25,
    fill_exit_ratio_90d_max: 175,
  })
})

test('non-numeric and non-finite values produce neither query parameters nor chips', () => {
  for (const invalid of ['abc', 'NaN', 'Infinity', '-Infinity', Number.NaN, Infinity]) {
    const filters = Object.fromEntries(Object.keys(DEFAULT_FILTERS).map((key) => [key, invalid]))
    assert.deepEqual(buildScreenerQuery(state({ filters })), { sort: 'pnl_30d', limit: 50 })
    assert.deepEqual(activeFilterChips(state({ filters })), [])
  }
})

test('coverage control includes partial history by default and applies exact period minimum when disabled', () => {
  assert.equal(buildScreenerQuery(state()).history_days_min, undefined)
  for (const period of PERIODS) {
    assert.equal(
      buildScreenerQuery(state({ period, includePartialHistory: false })).history_days_min,
      Number.parseInt(period, 10),
    )
  }
})

test('active chips use truthful metric and fetched TRADE history labels', () => {
  const filters = {
    ...DEFAULT_FILTERS,
    consistencyRatioMin: '60',
    fillExitMin: '50',
    fillExitMax: '150',
  }
  const chips = activeFilterChips(state({ period: '7d', includePartialHistory: false, filters }))
  assert.deepEqual(chips, [
    ['consistencyRatioMin', 'POSITIVE CLOSE-DAY RATIO 7D ≥ 60%'],
    ['fillExitMin', 'SELL / BUY EVENT COUNT 7D ≥ 50%'],
    ['fillExitMax', 'SELL / BUY EVENT COUNT 7D ≤ 150%'],
    ['includePartialHistory', 'FETCHED TRADE HISTORY ≥ 7D'],
  ])
})

test('WalletScreener has progressive controls and no experimental or all-time surface', async () => {
  const source = await readSource('../src/components/WalletScreener.jsx')
  assert.match(source, /INCLUDE PARTIAL TRADE HISTORY/)
  assert.match(source, /ADVANCED FILTERS/)
  assert.match(source, /POSITIVE CLOSE-DAY RATIO/)
  assert.match(source, /positive \/ \(positive \+ negative\) realized close days; flat\/no-close days omitted/i)
  assert.match(source, /SELL activity row count \/ BUY activity row count × 100; not capital, shares, or position close rate/i)
  assert.doesNotMatch(source, /PNL QUALITY|consistency_score|tier|fake score/i)
  assert.doesNotMatch(source, /\['pnl',\s*'total|sort:\s*['"](?:pnl|winrate|volume)['"]/)
})

test('exact lookup is separately labelled, address-tagged, current-only, and clears checking before exits', async () => {
  const source = await readSource('../src/components/WalletScreener.jsx')
  assert.match(source, /DIRECT WALLET LOOKUP · ACTIVE SCREENER FILTERS DO NOT APPLY/)
  assert.match(source, /setChecked\(\{\s*requestedAddress:\s*searchAddr,\s*trader\s*\}\)/)
  assert.match(source, /checked\?\.requestedAddress === searchAddr/)

  const effectStart = source.indexOf('// Live wallet check')
  const firstEarlyReturn = source.indexOf('if (!searchAddr', effectStart)
  const clearChecking = source.indexOf('setChecking(false)', effectStart)
  assert.ok(clearChecking > effectStart && clearChecking < firstEarlyReturn)
  assert.match(source, /let alive = true[\s\S]*setChecking\(true\)[\s\S]*return \(\) => \{\s*alive = false/)
})

test('API leaderboard wrapper defaults to pnl_30d', async () => {
  const source = await readSource('../src/api.js')
  assert.match(source, /Object\.entries\(\{ sort: 'pnl_30d', limit: 50, \.\.\.params \}\)/)
  assert.doesNotMatch(source, /Object\.entries\(\{ sort: 'consistency'/)
})

test('FilterSlider keeps text state but gives range input a finite fallback', async () => {
  const source = await readSource('../src/components/FilterSlider.jsx')
  assert.match(source, /Number\.isFinite/)
  assert.match(source, /value=\{value\}/)
})
