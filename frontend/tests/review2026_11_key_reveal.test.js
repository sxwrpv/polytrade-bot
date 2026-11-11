import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

// Runs the Account page's own reveal and close handlers against stand-in
// state setters and a key request the test settles by hand.
const source = readFileSync(new URL('../src/pages/User.jsx', import.meta.url), 'utf8')
const handlers = source.slice(source.indexOf('  async function reveal() {'), source.indexOf('  async function logOut() {'))

function dialog({ initData = 'signed-telegram' } = {}) {
  const state = { exp: true, key: '', expErr: '', revealing: false }
  const requests = []
  const api = {
    exportKey: (proof) => new Promise((resolve, reject) => { requests.push({ proof, resolve, reject }) }),
  }
  const { reveal, closeExport } = new Function(
    'api', 'window', 'revealSeq', 'setExp', 'setKey', 'setExpErr', 'setRevealing',
    `${handlers}; return { reveal, closeExport }`,
  )(
    api,
    { Telegram: { WebApp: { initData } } },
    { current: 0 },
    (v) => { state.exp = v },
    (v) => { state.key = v },
    (v) => { state.expErr = v },
    (v) => { state.revealing = v },
  )
  return { state, requests, reveal, closeExport }
}

test('a reveal that answers while the dialog is open shows the key', async () => {
  const d = dialog()
  const done = d.reveal()
  assert.equal(d.state.revealing, true)
  assert.equal(d.requests[0].proof, 'signed-telegram')
  d.requests[0].resolve({ private_key: 'test-key' })
  await done
  assert.deepEqual(d.state, { exp: true, key: 'test-key', expErr: '', revealing: false })
})

test('a key that arrives after the dialog closed stays out of state', async () => {
  const d = dialog()
  const done = d.reveal()
  d.closeExport()
  assert.deepEqual(d.state, { exp: false, key: '', expErr: '', revealing: false })
  d.requests[0].resolve({ private_key: 'test-key' })
  await done
  assert.deepEqual(d.state, { exp: false, key: '', expErr: '', revealing: false })
})

test('a failure that arrives after the dialog closed leaves no message behind', async () => {
  const d = dialog()
  const done = d.reveal()
  d.closeExport()
  d.requests[0].reject(new Error('Telegram proof expired'))
  await done
  assert.deepEqual(d.state, { exp: false, key: '', expErr: '', revealing: false })
})

test('closing clears a key that was on screen', async () => {
  const d = dialog()
  const done = d.reveal()
  d.requests[0].resolve({ private_key: 'test-key' })
  await done
  d.closeExport()
  assert.equal(d.state.key, '')
  assert.equal(d.state.exp, false)
})

test('an abandoned reveal cannot unlock or fill a later one', async () => {
  const d = dialog()
  const first = d.reveal()
  d.closeExport()
  d.state.exp = true                    // the dialog is opened again
  const second = d.reveal()
  d.requests[0].resolve({ private_key: 'stale-key' })
  await first
  assert.equal(d.state.key, '')
  assert.equal(d.state.revealing, true)  // the second request is still out
  d.requests[1].resolve({ private_key: 'fresh-key' })
  await second
  assert.equal(d.state.key, 'fresh-key')
  assert.equal(d.state.revealing, false)
})

test('a failed reveal in the open dialog still reports why', async () => {
  const d = dialog()
  const done = d.reveal()
  d.requests[0].reject(new Error('Telegram proof expired'))
  await done
  assert.deepEqual(d.state, { exp: true, key: '', expErr: 'Telegram proof expired', revealing: false })
})

test('outside Telegram no key request is sent', async () => {
  const d = dialog({ initData: '' })
  await d.reveal()
  assert.equal(d.requests.length, 0)
  assert.match(d.state.expErr, /Telegram bot/)
})

test('the dialog closes through closeExport and the page drops the key when it unmounts', () => {
  assert.match(source, /onClose=\{closeExport\}/)
  assert.match(source, /useEffect\(\(\) => \(\) => \{ revealSeq\.current \+= 1; setKey\(''\) \}, \[\]\)/)
})
