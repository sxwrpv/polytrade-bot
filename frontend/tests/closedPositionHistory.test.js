import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const positionsSource = readFileSync(
  new URL('../src/pages/Positions.jsx', import.meta.url),
  'utf8',
)

test('closed positions explains the 12-hour window and links to Polymarket history', () => {
  assert.match(positionsSource, /Showing position history from the last 12 hours\./)
  assert.match(positionsSource, />\s*More position history on Polymarket\s*</)
  assert.match(positionsSource, /href="https:\/\/polymarket\.com\/portfolio"/)
})
