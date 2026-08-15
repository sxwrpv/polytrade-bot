import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  analysisMetrics,
  positionPreview,
  selectCurrentEvidence,
  sliceDailyPnl,
} from '../src/components/traderAnalysisModel.js'

const readSource = (path) => readFile(new URL(path, import.meta.url), 'utf8')

test('daily PnL distinguishes unavailable payloads from a valid fetched empty object', () => {
  assert.equal(sliceDailyPnl(null, '30d', '2026-08-14T12:00:00Z'), null)
  assert.equal(sliceDailyPnl('{broken', '30d', '2026-08-14T12:00:00Z'), null)
  assert.equal(sliceDailyPnl('[]', '30d', '2026-08-14T12:00:00Z'), null)
  assert.equal(sliceDailyPnl('null', '30d', '2026-08-14T12:00:00Z'), null)
  assert.equal(sliceDailyPnl(42, '30d', '2026-08-14T12:00:00Z'), null)
  assert.equal(sliceDailyPnl({}, '30d', null), null)
  assert.equal(sliceDailyPnl({}, '30d', ''), null)
  assert.equal(sliceDailyPnl({}, '30d', 'not-a-date'), null)
  assert.equal(sliceDailyPnl({}, '30d', 123), null)
  assert.equal(sliceDailyPnl({}, '30d', true), null)
  assert.deepEqual(sliceDailyPnl({}, '30d', '2026-08-14T12:00:00Z'), [])
  assert.deepEqual(sliceDailyPnl({
    '2026-08-13': 0,
    '2026-02-30': 10,
    nope: 2,
    '2026-08-12': null,
    '2026-08-11': '4.5',
    '2026-08-10': 'not-a-number',
  }, '30d', '2026-08-14T12:00:00Z'), [
    { date: '2026-08-11', pnl: 4.5 },
    { date: '2026-08-13', pnl: 0 },
  ])
})

test('current evidence rejects a prior wallet result synchronously and normalizes matching addresses', () => {
  const cachedB = { address: '0xB', display_name: 'Cached B', pnl_30d: 2 }
  const fetchedA = { address: ' 0Xa ', data: { address: '0xA', display_name: 'Fetched A', pnl_30d: 1 } }
  const fetchedB = { address: ' 0XB ', data: { address: '0xB', display_name: 'Fetched B', pnl_30d: 3 } }

  assert.equal(selectCurrentEvidence('0xb', cachedB, fetchedA), cachedB)
  assert.equal(selectCurrentEvidence('0xb', cachedB, fetchedB), fetchedB.data)
  assert.deepEqual(selectCurrentEvidence('0xb', null, fetchedA), {})
})

test('daily PnL uses the backend inclusive UTC close-day cutoff for each selected period', () => {
  const raw = JSON.stringify({
    '2026-08-06': 6,
    '2026-08-07': 7,
    '2026-08-14': 14,
    '2026-08-15': 15,
    '2026-07-15': 30,
    '2026-07-14': 31,
    '2026-05-16': 90,
    '2026-05-15': 91,
  })

  assert.deepEqual(sliceDailyPnl(raw, '7d', '2026-08-14T23:59:59Z').map((d) => d.date), [
    '2026-08-07', '2026-08-14',
  ])
  assert.deepEqual(sliceDailyPnl(raw, '30d', '2026-08-14T00:00:00Z').map((d) => d.date), [
    '2026-07-15', '2026-08-06', '2026-08-07', '2026-08-14',
  ])
  assert.deepEqual(sliceDailyPnl(raw, '90d', '2026-08-14T12:00:00Z').map((d) => d.date), [
    '2026-05-16', '2026-07-14', '2026-07-15', '2026-08-06', '2026-08-07', '2026-08-14',
  ])
})

test('analysis metrics switch reactively by selected period and never turn missing values into zero', () => {
  const fetched = {
    pnl_7d: null,
    pnl_30d: 30,
    winrate_7d: 0,
    winrate_30d: null,
    volume_7d: 70,
    volume_30d: 300,
    history_days: 12.5,
    stats_refreshed_at: '2026-08-14T12:00:00Z',
  }

  assert.deepEqual(analysisMetrics(fetched, '7d'), {
    period: '7d', days: 7, realizedPnl: null, winRate: 0, grossVolume: 70,
    historyDays: 12.5, partial: false, refreshedAt: '2026-08-14T12:00:00Z',
  })
  assert.equal(analysisMetrics(fetched, '30d').realizedPnl, 30)
  assert.equal(analysisMetrics(fetched, '30d').winRate, null)
  assert.equal(analysisMetrics(fetched, '30d').grossVolume, 300)
  assert.equal(analysisMetrics(fetched, '30d').partial, true)
})

test('position preview puts live holdings before redeemable leftovers, sorts by value, and does not mutate payload', () => {
  const positions = [
    { asset: 'resolved-high', size: 3, redeemable: true, current_value: 100 },
    { asset: 'live-low', size: 1, redeemable: false, current_value: 2 },
    { asset: 'empty', size: 0, redeemable: false, current_value: 500 },
    { asset: 'live-high', size: 2, redeemable: false, current_value: 20 },
    { asset: 'resolved-low', size: 1, redeemable: true, current_value: 1 },
  ]
  const originalOrder = positions.map((p) => p.asset)
  const preview = positionPreview(positions, 3)

  assert.deepEqual(preview.items.map((p) => p.asset), ['live-high', 'live-low', 'resolved-high'])
  assert.equal(preview.total, 4)
  assert.equal(preview.liveCount, 2)
  assert.equal(preview.redeemableCount, 2)
  assert.equal(preview.label, 'SHOWING 3 OF 4 FETCHED HOLDINGS')
  assert.deepEqual(positions.map((p) => p.asset), originalOrder)
  assert.equal(positionPreview(positions, 8).label, '4 FETCHED HOLDINGS')
  assert.deepEqual(positionPreview(null, 8), {
    items: [], total: null, liveCount: null, redeemableCount: null,
    label: 'FETCHED HOLDINGS UNAVAILABLE',
  })
})

test('TraderAnalysis owns deep evidence, period-reactive history, provenance, and the fetched state', async () => {
  const source = await readSource('../src/components/TraderAnalysis.jsx')

  assert.match(source, /function TraderAnalysis|export default function TraderAnalysis/)
  assert.match(source, /address.*trader.*period|trader.*period/)
  assert.match(source, /analysisMetrics\([^,]+,\s*period\)/)
  assert.match(source, /sliceDailyPnl\([^,]+,\s*period,/)
  assert.match(source, /<Sparkline daily=\{daily\}/)
  assert.match(source, /STATS FETCHING|FETCHING LATEST STATS/)
  assert.match(source, /SELECTED PERIOD SUMMARY/)
  assert.match(source, /CURRENT POSITIONS/)
  assert.match(source, /RECENT ACTIVITY/)
  assert.match(source, /PNL AND WIN RATE ARE RECONSTRUCTED FROM FETCHED CLOSING EVENTS/)
  assert.match(source, /PNL AND WIN RATE ALSO USE SEPARATELY BOUNDED REDEEM AND POSITIONS CLOSING EVIDENCE/)
  assert.match(source, /TRADE, REDEEM, AND POSITIONS SOURCES CAN EACH BE PARTIAL/)
  assert.match(source, /GROSS VOLUME USES FETCHED TRADE ROWS/)
  assert.match(source, /POSITIONS MAY TRUNCATE AT 500 ROWS/)
  assert.match(source, /FETCHED TRADE HISTORY/)
  assert.doesNotMatch(source, /FETCHED-HISTORY SCOPE/)
  assert.doesNotMatch(source, /Selected-period metrics use only/)
  assert.match(source, /setFetched\(\{\s*address,\s*data:\s*result\s*\}\)/)
  assert.match(source, /selectCurrentEvidence\(address,\s*trader,\s*fetched\)/)
  assert.doesNotMatch(source, /const evidence = fetched \|\| trader/)
  assert.doesNotMatch(source, /recommend|ranking|concentration/i)
})

test('TraderCard keeps the concise C1 surface and passes global period into deep analysis beside copy settings', async () => {
  const source = await readSource('../src/components/TraderCard.jsx')
  const primary = source.split('{expanded &&')[0]

  assert.doesNotMatch(primary, /Sparkline|CURRENT POSITIONS|RECENT ACTIVITY/)
  assert.match(source, /<TraderAnalysis[\s\S]*trader=\{t\}[\s\S]*period=\{period\}/)
  assert.match(source, /\{expanded &&[\s\S]*<TraderAnalysis[\s\S]*>COPY SETTINGS</)
  assert.doesNotMatch(source, /TraderProfile/)
})

test('deep analysis has a mobile single-column layout and wrapping rows', async () => {
  const css = await readSource('../src/styles/brutalism.css')
  const mobile = css.slice(css.indexOf('@media (max-width: 760px)'))

  assert.match(mobile, /\.ta-summary\s*\{[^}]*grid-template-columns:\s*1fr/)
  assert.match(mobile, /\.ta-row\s*\{[^}]*align-items:\s*flex-start/)
})
