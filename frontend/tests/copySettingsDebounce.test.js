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

const setModeSource = source.slice(source.indexOf('  async function setMode(mode) {'),
  source.indexOf('  async function remove()'))
for (const ok of [true, false]) {
  test(`a sizing mode switch saves at once${ok ? '' : ' and is undone when the save fails'}`, async () => {
    let state = { sizing_mode: 'position_share' }
    const saved = []
    const setMode = new Function('s', 'setS', 'persist', `${setModeSource}; return setMode`)(
      state, (update) => { state = update(state) }, async (patch) => { saved.push(patch); return ok })
    await setMode('capital_share')
    assert.deepEqual(saved, [{ sizing_mode: 'capital_share' }])
    assert.equal(state.sizing_mode, ok ? 'capital_share' : 'position_share')
  })
}

test('choosing the mode already set sends nothing', async () => {
  const saved = []
  const setMode = new Function('s', 'setS', 'persist', `${setModeSource}; return setMode`)(
    { sizing_mode: 'capital_share' }, () => {}, async (patch) => { saved.push(patch); return true })
  await setMode('capital_share')
  assert.deepEqual(saved, [])
})
