import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { draftText, parseDraft } from '../src/settingDraft.js'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')

// The exact-value field as React runs it: the parent keeps whatever onChange
// hands it (WalletRiskCard.set and CopyWalletCard.setRatio both do), and the
// input shows draftText(draft, value) on the next render.
function field(value) {
  let draft = null
  const sent = []
  return {
    type(text) { draft = text; value = parseDraft(text); sent.push(value) },
    blur() { draft = null },
    outside(next) { value = next },
    get shown() { return draftText(draft, value) },
    get value() { return value },
    sent,
  }
}

test('typing 1.5 keeps the decimal point on screen at every keystroke', () => {
  const f = field(7)
  for (const [text, value] of [['1', 1], ['1.', 1], ['1.5', 1.5]]) {
    f.type(text)
    assert.equal(f.shown, text)
    assert.equal(f.value, value)
  }
  assert.deepEqual(f.sent, [1, 1, 1.5])
})

test('a comma keypad types the same decimal', () => {
  const f = field(7)
  for (const [text, value] of [['1', 1], ['1,', 1], ['1,5', 1.5]]) {
    f.type(text)
    assert.equal(f.shown, text)
    assert.equal(f.value, value)
  }
  f.blur()
  assert.equal(f.shown, '1.5')
})

test('a leading zero survives on the way to 0.05', () => {
  const f = field(0.1)
  for (const [text, value] of [['0', 0], ['0.', 0], ['0.0', 0], ['0.05', 0.05]]) {
    f.type(text)
    assert.equal(f.shown, text)
    assert.equal(f.value, value)
  }
})

test('text that is no number reaches the parent as the empty value', () => {
  for (const text of ['', '   ', '.', ',', '-', 'abc', '1,000,000', '1.000,5', '1,5.2', 'Infinity', 'NaN']) {
    assert.equal(parseDraft(text), '', JSON.stringify(text))
  }
  const f = field(15)
  f.type('abc')
  assert.equal(f.shown, 'abc')
  assert.equal(f.value, '')
  f.blur()
  assert.equal(f.shown, '')
})

test('finite numbers pass through as numbers', () => {
  assert.equal(parseDraft('15'), 15)
  assert.equal(parseDraft(' 2.50 '), 2.5)
  assert.equal(parseDraft(',5'), 0.5)
  assert.equal(parseDraft('.5'), 0.5)
  assert.equal(parseDraft('0'), 0)
})

test('the field shows the value again on blur and after a change from outside', () => {
  const f = field(15)
  f.type('2.50')
  assert.equal(f.shown, '2.50')
  f.blur()
  assert.equal(f.shown, '2.5')

  f.type('1.')
  f.outside(3)            // a slider drag, or a reload from the server
  assert.equal(f.shown, '3')
  f.outside(null)
  assert.equal(f.shown, '')
})

test('the field shows the value the parent holds once it differs from the draft', () => {
  assert.equal(draftText('600', 500), '500')
  assert.equal(draftText('1.5', ''), '')
  assert.equal(draftText(null, 0.98), '0.98')
  assert.equal(draftText('', ''), '')
})

test('SettingSlider wires the draft into its exact-value field', () => {
  const source = read('../src/components/SettingSlider.jsx')
  const input = source.slice(source.indexOf('className="slider-num"'))
  assert.match(input, /value=\{draftText\(draft, value\)\}/)
  assert.match(input, /onChange=\{\(e\) => \{ setDraft\(e\.target\.value\); onChange\(parseDraft\(e\.target\.value\)\) \}\}/)
  assert.match(input, /onBlur=\{\(\) => setDraft\(null\)\}/)
  assert.match(input, /inputMode="decimal"/)
  // A drag hands the field back to the value.
  const range = source.slice(source.indexOf('type="range"'), source.indexOf('className="slider-num"'))
  assert.match(range, /setDraft\(null\)/)
})

// The wallet card's own handler, fed what the field sends for each keystroke.
const card = read('../src/components/WalletRiskCard.jsx')
const handler = card.slice(card.indexOf('  function set(k, v) {'), card.indexOf('  async function toggleEnabled'))
function wallet() {
  let serial = 0
  const pending = new Map(), saved = []
  const set = new Function('setS', 'timers', 'clearTimeout', 'setTimeout', 'persist', 'ZERO_IS_NULL',
    `${handler}; return set`)(() => {}, { current: {} }, (id) => pending.delete(id),
    (fn) => { pending.set(++serial, fn); return serial }, (patch) => saved.push(patch), new Set())
  return { set, saved, flush: () => { for (const fn of pending.values()) fn(); pending.clear() } }
}

test('typing 1.5 into a wallet setting saves 1.5', () => {
  const w = wallet()
  for (const text of ['1', '1.', '1.5']) w.set('copy_ratio_pct', parseDraft(text))
  w.flush()
  assert.deepEqual(w.saved, [{ copy_ratio_pct: 1.5 }])
})

test('clearing the field cancels the unsent save and sends nothing', () => {
  const w = wallet()
  for (const text of ['1', '']) w.set('max_position_usd', parseDraft(text))
  w.flush()
  assert.deepEqual(w.saved, [])
})
