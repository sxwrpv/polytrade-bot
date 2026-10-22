import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  CAPITAL_SHARE, MULTIPLIER_STOPS, POSITION_SHARE, capitalShareCopy, cashShare, nearestStop,
} from '../src/copySizing.js'

test('cash share reads the bet against the cash held before it, as the engine does', () => {
  assert.equal(cashShare(2000, 8000), 0.2)
  assert.equal(cashShare(500, 0), 1)
  for (const [bet, cash] of [[0, 10], [-1, 10], [10, -1], [NaN, 10], [10, Infinity], [10, null]]) {
    assert.equal(cashShare(bet, cash), null, `${bet} of ${cash}`)
  }
})

test('a capital-share copy is share × your cash × multiplier, then the caps and the minimum', () => {
  assert.deepEqual(
    capitalShareCopy({ share: 0.2, yourCash: 150, multiplier: 1, cap: 50, minCopy: 2 }),
    { raw: 30, order: 30, state: 'copied' })
  const capped = capitalShareCopy({ share: 0.2, yourCash: 150, multiplier: 2, cap: 50, minCopy: 2 })
  assert.deepEqual([capped.order, capped.state], [50, 'capped'])
  const allIn = capitalShareCopy({ share: 0.5, yourCash: 150, multiplier: 4, cap: 500, minCopy: 2 })
  assert.deepEqual([allIn.order, allIn.state], [150, 'capped'])
  const small = capitalShareCopy({ share: 0.005, yourCash: 150, multiplier: 1, cap: 50, minCopy: 2 })
  assert.deepEqual([small.order, small.state], [0, 'skipped'])
})

test('multiplier stops run in order across the API bounds, and a value finds its stop', () => {
  assert.equal(MULTIPLIER_STOPS[0], 0.1)
  assert.equal(MULTIPLIER_STOPS.at(-1), 100)
  assert.deepEqual([...MULTIPLIER_STOPS].sort((a, b) => a - b), MULTIPLIER_STOPS)
  assert.equal(MULTIPLIER_STOPS[nearestStop(MULTIPLIER_STOPS, 1)], 1)
  assert.equal(MULTIPLIER_STOPS[nearestStop(MULTIPLIER_STOPS, 11)], 10)
  assert.equal(MULTIPLIER_STOPS[nearestStop(MULTIPLIER_STOPS, 1000)], 100)
  assert.equal(MULTIPLIER_STOPS[nearestStop(MULTIPLIER_STOPS, 0)], 0.1)
})

test('the mode names are the ones the engine and the API store', () => {
  const engine = readFileSync(new URL('../../backend/core/copy_engine.py', import.meta.url), 'utf8')
  const api = readFileSync(new URL('../../backend/api/routes_traders.py', import.meta.url), 'utf8')
  assert.match(engine, new RegExp(`POSITION_SHARE = "${POSITION_SHARE}"`))
  assert.match(engine, new RegExp(`CAPITAL_SHARE = "${CAPITAL_SHARE}"`))
  assert.match(api, new RegExp(`Literal\\["${POSITION_SHARE}", "${CAPITAL_SHARE}"\\]`))
})
