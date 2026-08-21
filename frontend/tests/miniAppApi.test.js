/* The authenticated Mini App API wrapper.
 *
 * This test moved here from walletScreenerModel.test.js: it covers src/api.js,
 * which is live, so it had to outlive the retired in-app screener's suite.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const readSource = (path) => readFile(new URL(path, import.meta.url), 'utf8')

test('API leaderboard wrapper defaults to pnl_30d', async () => {
  const source = await readSource('../src/api.js')
  assert.match(source, /Object\.entries\(\{ sort: 'pnl_30d', limit: 50, \.\.\.params \}\)/)
  assert.doesNotMatch(source, /Object\.entries\(\{ sort: 'consistency'/)
})
