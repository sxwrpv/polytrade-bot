import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  discoveryMetrics,
  formatActivePositions,
  formatMoney,
} from '../src/components/traderCardModel.js'

const readSource = (path) => readFile(new URL(path, import.meta.url), 'utf8')

test('money formatting preserves small-value precision and the original sign', () => {
  assert.equal(formatMoney(null), '—')
  assert.equal(formatMoney(0), '$0')
  assert.equal(formatMoney(0, { signed: true }), '+$0')
  assert.equal(formatMoney(-0.01, { signed: true }), '-$0.01')
  assert.equal(formatMoney(0.01, { signed: true }), '+$0.01')
  assert.equal(formatMoney(-12.3), '-$12.30')
  assert.equal(formatMoney(12.3), '$12.30')
  assert.equal(formatMoney(1234.49), '$1,234')
  assert.equal(formatMoney(-1234.49, { signed: true }), '-$1,234')
})

test('active-position formatting reports only the fetched count and preserves null versus zero', () => {
  assert.equal(formatActivePositions(null), '—')
  assert.equal(formatActivePositions(0), '0')
  assert.equal(formatActivePositions(499), '499')
  assert.equal(formatActivePositions(500), '500')
  assert.equal(formatActivePositions(501), '501')
  assert.doesNotMatch(formatActivePositions(500), /\+/)
})

test('discovery metrics map every globally selected period to its matching fields', () => {
  const trader = {
    pnl_7d: 7,
    pnl_30d: 30,
    pnl_90d: 90,
    winrate_7d: 0.17,
    winrate_30d: 0.3,
    winrate_90d: 0.9,
    volume_7d: 700,
    volume_30d: 3000,
    volume_90d: 9000,
    open_positions: 4,
    history_days: 45,
    stats_refreshed_at: '2026-08-14T12:30:00Z',
  }

  assert.deepEqual(discoveryMetrics(trader, '7d'), {
    period: '7d',
    days: 7,
    realizedPnl: 7,
    winRate: 0.17,
    grossVolume: 700,
    activePositions: 4,
    historyDays: 45,
    partial: false,
    refreshedAt: '2026-08-14T12:30:00Z',
  })
  assert.equal(discoveryMetrics(trader, '30d').realizedPnl, 30)
  assert.equal(discoveryMetrics(trader, '30d').winRate, 0.3)
  assert.equal(discoveryMetrics(trader, '30d').grossVolume, 3000)
  assert.equal(discoveryMetrics(trader, '90d').realizedPnl, 90)
  assert.equal(discoveryMetrics(trader, '90d').winRate, 0.9)
  assert.equal(discoveryMetrics(trader, '90d').grossVolume, 9000)
  assert.equal(discoveryMetrics(trader, '90d').partial, true)
})

test('fetched TRADE history coverage uses a strict selected-period boundary', () => {
  assert.equal(discoveryMetrics({ history_days: 29.6 }, '30d').partial, true)
  assert.equal(discoveryMetrics({ history_days: 29.999 }, '30d').partial, true)
  assert.equal(discoveryMetrics({ history_days: 30 }, '30d').partial, false)
  assert.equal(discoveryMetrics({ history_days: 30.1 }, '30d').partial, false)
  assert.equal(discoveryMetrics({ history_days: null }, '30d').partial, false)
})

test('active positions stay unavailable before enrichment, including a legacy zero', () => {
  const metrics = discoveryMetrics({ open_positions: 0, history_days: null }, '30d')

  assert.equal(metrics.realizedPnl, null)
  assert.equal(metrics.winRate, null)
  assert.equal(metrics.grossVolume, null)
  assert.equal(metrics.activePositions, null)
  assert.equal(metrics.partial, false)
  assert.equal(metrics.refreshedAt, null)

  assert.equal(discoveryMetrics({ open_positions: null }, '30d').activePositions, null)
  assert.equal(discoveryMetrics({ open_positions: 500 }, '30d').activePositions, null)
})

test('active positions preserve a true zero and fetched count after enrichment', () => {
  const enriched = { stats_refreshed_at: '2026-08-14T12:30:00Z' }

  assert.equal(discoveryMetrics({ ...enriched, open_positions: 0 }, '30d').activePositions, 0)
  assert.equal(discoveryMetrics({ ...enriched, open_positions: 500 }, '30d').activePositions, 500)
  assert.equal(discoveryMetrics({ ...enriched, open_positions: null }, '30d').activePositions, null)
})

test('TraderCard discloses fetched-position lower-bound risk without inventing truncation', async () => {
  const source = await readSource('../src/components/TraderCard.jsx')

  assert.match(source, /ACTIVE POSITIONS\*/)
  assert.match(source, /\* fetched positions snapshot; count can be a lower bound if source reaches its 500-row cap\./)
  assert.doesNotMatch(source, /500\+/)
})

test('TraderCard presents fetched history as approximate, including complete coverage', async () => {
  const source = await readSource('../src/components/TraderCard.jsx')

  assert.doesNotMatch(source, /AT LEAST/)
  assert.match(source, /FETCHED TRADE HISTORY · ~\{metrics\.days\}D OBSERVED/)
  assert.match(source, /~\{metrics\.historyDays\}D OF \{metrics\.days\}D/)
})

test('TraderCard primary surface stays focused and ANALYZE reveals evidence plus copy settings', async () => {
  const source = await readSource('../src/components/TraderCard.jsx')
  const primary = source.split('{expanded &&')[0]

  assert.match(primary, /discoveryMetrics\s*\(t,\s*period\)/)
  assert.match(primary, /REALIZED PNL/)
  assert.match(primary, /WIN RATE/)
  assert.match(primary, /GROSS VOLUME/)
  assert.match(primary, /ACTIVE POSITIONS/)
  assert.match(primary, /formatMoney\(metrics\.realizedPnl, \{ signed: true \}\)/)
  assert.match(primary, /formatActivePositions\(metrics\.activePositions\)/)
  assert.match(primary, /FETCHED TRADE HISTORY/)
  assert.doesNotMatch(primary, /(?<!TRADE HISTORY )COVERAGE/)
  assert.doesNotMatch(primary, /tier-badge|LIFETIME|ALL-TIME|CONS(?:ISTENCY)?|PNL QUALITY|QUAL |EXIT\/FILL|GREEN DAYS|DAYS|Sparkline|COPY TRADER|click to copy/)
  assert.doesNotMatch(primary, /PERIODS\.map|setChartPeriod/)

  assert.match(source, /setExpanded\(\(current\) => !current\)/)
  assert.match(source, /aria-expanded=\{expanded\}/)
  assert.match(source, /aria-controls=\{analysisRegionId\}/)
  assert.match(source, /\{expanded \? 'HIDE ANALYSIS' : 'ANALYZE'\}/)
  assert.match(source, /useId\(\)\.replace\(\/\[\^a-zA-Z0-9_-\]\/g, ''\)/)
  assert.doesNotMatch(source, /analysisRegionId\s*=.*t\.address/)
  assert.match(source, /\{expanded &&[\s\S]*id=\{analysisRegionId\}[\s\S]*role="region"[\s\S]*aria-label="Trader analysis"[\s\S]*<TraderProfile address=\{t\.address\}/)
  assert.match(source, /\{expanded &&[\s\S]*>COPY SETTINGS</)
  assert.match(source, /RECONSTRUCTED FROM FETCHED CLOSING EVENTS/)
  assert.match(source, /TRADE, REDEEM, AND POSITIONS SOURCES CAN EACH BE PARTIAL/)
  assert.match(source, /GROSS VOLUME USES FETCHED TRADE ROWS/)
})

test('mobile CSS stacks the card heading and permits the period label to wrap', async () => {
  const css = await readSource('../src/styles/brutalism.css')
  const mobile = css.slice(css.indexOf('@media (max-width: 760px)'))

  assert.match(mobile, /\.trader-card \.tc-top\s*\{[^}]*flex-direction:\s*column/)
  assert.match(mobile, /\.tc-period-label\s*\{[^}]*white-space:\s*normal/)
})
