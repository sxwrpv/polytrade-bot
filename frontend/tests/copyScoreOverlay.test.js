import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  CLASS_CUTS,
  COHORT_PERIOD_LABEL,
  RECOMMENDED,
  bandRuns,
  buildCohortBoard,
  classDef,
  cohortAge,
  cohortFilterChips,
  cohortToCsv,
  passesCohortFilters,
  roiIn,
  scoreIsNumeric,
  scoreMagnitude,
  scoreSigned,
  toCohortPeriod,
} from '../src/screener/cohortModel.js'
import { buildPublicQuery, SORTS, decodeScreenerState, encodeScreenerState } from '../src/screener/screenerModel.js'

const root = new URL('../', import.meta.url)
const read = (path) => readFile(new URL(path, root), 'utf8')

const wallet = (over = {}) => ({
  w: '0x' + '11'.repeat(20),
  name: 'someone',
  copyClass: 'strong',
  copyNet: 12,
  pnl: 1000, d7: 100, d30: 500,
  vol: 200000, d7Vol: 40000, d30Vol: 90000,
  winRate: 0.6, activeDays: 90, avgSize: 120, openVal: 0,
  mm: 'directional', arb: null, freq: null, cats: ['Sports'],
  lastTradeDay: '2026-08-21',
  ...over,
})

/* ---- the promise the public API makes is unchanged ---------------------- */

test('adding Copy Score does not widen what the public API is asked to rank by', () => {
  // The route publishes no composite score, and this overlay does not change
  // that: the score is a third party's and travels as its own asset.
  assert.deepEqual(SORTS.map(([key]) => key), ['pnl', 'winrate', 'volume'])
  assert.throws(() => buildPublicQuery({ sort: 'copy' }), /sort/)
  assert.throws(() => buildPublicQuery({ sort: 'roi' }), /sort/)
})

test('the overlay is a static asset, never a field on the screener API', async () => {
  const [loader, route] = await Promise.all([
    read('src/screener/cohort.js'),
    read('../backend/api/routes_public_screener.py'),
  ])
  assert.match(loader, /screener-cohort\.json/)
  // The loader must not reach the API namespace for a score.
  assert.doesNotMatch(loader, /public\/screener\/wallets/)
  // And the route still refuses to publish one.
  assert.doesNotMatch(route, /copy_net|copy_class|copy_score/)
})

/* ---- whose number it is reaches the reader ------------------------------ */

test('every surface that shows a score says whose it is and how old', async () => {
  const page = await read('src/screener/ScreenerPage.jsx')
  assert.match(page, /CohortNotice/)
  // Named, dated, and distinguished from PolyTrade's own measurement.
  assert.match(page, /scoreOwner/)
  assert.match(page, /not PolyTrade&rsquo;s live cache/)
  assert.match(page, /PolyTrade publishes no composite\s+score of its own/)
})

test('the shipped cohort asset carries its provenance and generation date', async () => {
  const raw = JSON.parse(await read('public/screener-cohort.json'))
  assert.ok(raw.meta.generatedAt, 'generatedAt')
  assert.match(raw.meta.source, /polycopy/i)
  assert.ok(raw.traders.length > 1000, 'a cohort, not a sample')
  // The projection is an allowlist: no field arrives that the board does not
  // knowingly render.
  const allowed = new Set([
    'w', 'name', 'followers', 'pnl', 'd7', 'd30', 'winRate', 'vol', 'd7Vol', 'd30Vol',
    'openVal', 'copyClass', 'copyNet', 'mm', 'arb', 'freq', 'cats', 'lastTradeDay',
    'fills', 'activeDays', 'maker', 'hold', 'weeksUp', 'vola', 'avgSize', 'maxDD', 'niche',
  ])
  for (const key of Object.keys(raw.traders[0])) assert.ok(allowed.has(key), key)
})

test('a cohort more than two days old is called stale rather than current', () => {
  const now = Date.parse('2026-08-23T00:00:00Z')
  assert.equal(cohortAge('2026-08-22T20:00:00Z', now).stale, false)
  assert.equal(cohortAge('2026-08-20T00:00:00Z', now).stale, true)
  assert.equal(cohortAge('2026-08-20T00:00:00Z', now).label, '3d old')
  // An undated snapshot is treated as stale, not as fresh.
  assert.equal(cohortAge(null, now).stale, true)
})

/* ---- the score itself --------------------------------------------------- */

test('a wallet the cohort does not cover is not scored, which is not a zero', () => {
  assert.equal(scoreIsNumeric('none', null), false)
  assert.equal(scoreIsNumeric(null, 0), false)
  assert.equal(scoreSigned(null), null)
  assert.equal(scoreMagnitude(null), null)
  assert.equal(classDef(null).chip, 'Not scored')
  assert.match(classDef(null).line, /absence of one/)
  // A genuine zero still renders as a zero.
  assert.equal(scoreSigned(0), '0')
  assert.equal(scoreIsNumeric('marginal', 0), true)
})

test('the score prints signed standalone and as a magnitude in the column', () => {
  assert.equal(scoreSigned(6), '+6')
  assert.equal(scoreSigned(-4), '−4')
  assert.equal(scoreMagnitude(11), '11%')
  assert.equal(scoreMagnitude(-4), '−4%')
})

test('the inferred class cuts are the ones the board was fitted to', () => {
  assert.deepEqual(CLASS_CUTS, { strong: 10, marginal: 0 })
})

/* ---- ordering ----------------------------------------------------------- */

test('Copy Score orders by band first and uses money only as the tie-break', () => {
  const rows = [
    wallet({ w: '0xa', copyClass: 'marginal', copyNet: 3, d30: 900_000 }),
    wallet({ w: '0xb', copyClass: 'strong', copyNet: 11, d30: 10 }),
    wallet({ w: '0xc', copyClass: 'strong', copyNet: 40, d30: 20 }),
  ]
  const board = buildCohortBoard(rows, { metric: 'copy', period: 'd30' })
  // Both strong wallets outrank the far richer marginal one; between them the
  // bigger window PnL wins.
  assert.deepEqual(board.map((r) => r.w), ['0xc', '0xb', '0xa'])
})

test('the volume floor a rate-based ordering needs is applied, and stated', () => {
  const thin = wallet({ w: '0xthin', d30Vol: 10 })
  const thick = wallet({ w: '0xthick', d30Vol: 90_000 })
  const board = buildCohortBoard([thin, thick], { metric: 'copy', period: 'd30' })
  assert.deepEqual(board.map((r) => r.w), ['0xthick'])

  // And the reader can see the cut that removed it.
  const chips = cohortFilterChips({ metric: 'copy', period: 'd30', bands: RECOMMENDED })
  const floor = chips.find((chip) => chip.key === 'minVolume')
  assert.equal(floor.text, 'Volume 30D ≥ $60K')
  // Structural cuts are not clearable — they belong to the ordering.
  assert.equal(floor.clearable, false)
})

test('the default board shows only the bands that clear copy costs', () => {
  const rows = [
    wallet({ w: '0xa', copyClass: 'strong' }),
    wallet({ w: '0xb', copyClass: 'loss_making', copyNet: -30 }),
  ]
  const board = buildCohortBoard(rows, { metric: 'copy', period: 'd30' })
  assert.deepEqual(board.map((r) => r.w), ['0xa'])
  assert.deepEqual([...RECOMMENDED].sort(), ['marginal', 'strong'])
})

test('band headers are printed only under Copy Score ordering', () => {
  const rows = [{ copyClass: 'strong' }, { copyClass: 'strong' }, { copyClass: 'marginal' }]
  assert.deepEqual(bandRuns(rows, 'copy').map((run) => [run.index, run.band, run.total]),
    [[0, 'strong', 2], [2, 'marginal', 1]])
  // Under any other ordering the bands interleave, so a header would describe
  // rows that do not follow it.
  assert.deepEqual(bandRuns(rows, 'pnl'), [])
  assert.deepEqual(bandRuns(rows, null), [])
})

/* ---- filters ------------------------------------------------------------ */

test('an absent metric fails a filter that asks about it, rather than passing', () => {
  const blank = wallet({ winRate: null, activeDays: null, copyNet: null })
  assert.equal(passesCohortFilters(blank, { winrateMin: '40' }, 'd30'), false)
  assert.equal(passesCohortFilters(blank, { activeDaysMin: '10' }, 'd30'), false)
  assert.equal(passesCohortFilters(blank, { copyNetMin: '0' }, 'd30'), false)
  // With no filter set it is not excluded for lacking the metric.
  assert.equal(passesCohortFilters(blank, {}, 'd30'), true)
})

test('average fill is a ceiling: too big to mirror is a reason to hide a wallet', () => {
  const whale = wallet({ avgSize: 220_000 })
  const retail = wallet({ avgSize: 120 })
  assert.equal(passesCohortFilters(whale, { avgSizeMax: '500' }, 'd30'), false)
  assert.equal(passesCohortFilters(retail, { avgSizeMax: '500' }, 'd30'), true)
})

test('wallets whose edge a copy cannot follow can be dropped in one move', () => {
  const maker = wallet({ mm: 'market_maker' })
  const arb = wallet({ arb: 'arb' })
  const fast = wallet({ freq: 'vhft' })
  const plain = wallet()
  for (const row of [maker, arb, fast]) {
    assert.equal(passesCohortFilters(row, { excludeHardToMirror: true }, 'd30'), false)
  }
  assert.equal(passesCohortFilters(plain, { excludeHardToMirror: true }, 'd30'), true)
})

test('ROI is unavailable without volume — a wallet that traded nothing is not 0%', () => {
  assert.equal(roiIn(wallet({ d30: 100, d30Vol: 0 }), 'd30'), null)
  assert.equal(roiIn(wallet({ d30: 100, d30Vol: 1000 }), 'd30'), 10)
})

/* ---- windows ------------------------------------------------------------ */

test('the lifetime window is labelled ALL, never relabelled as the live 90D', () => {
  assert.equal(toCohortPeriod('90d'), 'all')
  assert.equal(COHORT_PERIOD_LABEL.all, 'ALL')
  assert.equal(COHORT_PERIOD_LABEL[toCohortPeriod('7d')], '7D')
  assert.equal(COHORT_PERIOD_LABEL[toCohortPeriod('30d')], '30D')
})

/* ---- shareable state ---------------------------------------------------- */

test('a Copy Score view links like any other, and a bare link is the default board', () => {
  const defaults = decodeScreenerState('')
  assert.equal(defaults.sort, 'pnl')
  assert.equal(defaults.category, 'all')
  assert.equal(defaults.direction, 'desc')
  assert.deepEqual([...defaults.bands].sort(), ['marginal', 'strong'])
  // Nothing is written for a default board.
  assert.equal(encodeScreenerState(defaults), '')

  const encoded = encodeScreenerState({
    ...defaults,
    sort: 'copy',
    category: 'Sports',
    direction: 'asc',
    bands: new Set(['strong']),
    filters: { ...defaults.filters, avgSizeMax: '500', excludeHardToMirror: true },
  })
  const back = decodeScreenerState(encoded)
  assert.equal(back.sort, 'copy')
  assert.equal(back.category, 'Sports')
  assert.equal(back.direction, 'asc')
  assert.deepEqual([...back.bands], ['strong'])
  assert.equal(back.filters.avgSizeMax, '500')
  assert.equal(back.filters.excludeHardToMirror, true)
})

test('a junk band or direction in a link is ignored, not adopted', () => {
  const state = decodeScreenerState('bands=nonsense&direction=sideways')
  assert.deepEqual([...state.bands].sort(), ['marginal', 'strong'])
  assert.equal(state.direction, 'desc')
})

/* ---- export ------------------------------------------------------------- */

test('an absent metric exports as an empty cell, never as a zero', () => {
  const csv = cohortToCsv([wallet({ copyNet: null, winRate: null, d30Vol: 0, d30: 5 })], 'd30')
  const [header, row] = csv.split('\n')
  const columns = header.split(',')
  const cells = row.split(',')
  assert.equal(cells[columns.indexOf('copy_score')], '')
  assert.equal(cells[columns.indexOf('win_rate')], '')
  // ROI is undefined at zero volume, so it exports empty rather than as 0.
  assert.equal(cells[columns.indexOf('roi_pct')], '')
  // A real figure still exports.
  assert.equal(cells[columns.indexOf('pnl_usd')], '5')
})
