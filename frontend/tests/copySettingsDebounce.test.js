import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

// Exercise the component's actual event handler without a DOM or live API.
const source = readFileSync(new URL('../src/components/WalletRiskCard.jsx', import.meta.url), 'utf8')
const handler = source.slice(source.indexOf('  function set(k, v) {'), source.indexOf('  async function toggleEnabled'))
function harness() {
  let serial = 0
  const pending = new Map(), saved = []
  const set = new Function('setS', 'timers', 'clearTimeout', 'setTimeout', 'persist', 'ZERO_IS_NULL',
    `${handler}; return set`)(() => {}, { current: {} }, id => pending.delete(id),
    fn => { pending.set(++serial, fn); return serial }, patch => saved.push(patch), new Set())
  return { set, saved, flush: () => { for (const fn of pending.values()) fn(); pending.clear() } }
}
for (const invalid of ['', NaN, Infinity]) {
  test(`invalid edit ${String(invalid)} cancels the previous unsent setting`, () => {
    const h = harness()
    h.set('min_price', 0.3)
    h.set('min_price', invalid)
    h.flush()
    assert.deepEqual(h.saved, [])
  })
}
test('latest valid edit is the only submitted setting', () => {
  const h = harness()
  h.set('min_price', 0.3)
  h.set('min_price', 0.4)
  h.flush()
  assert.deepEqual(h.saved, [{ min_price: 0.4 }])
})
