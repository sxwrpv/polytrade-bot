import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const settle = () => new Promise((resolve) => setImmediate(resolve))

// Runs the copied-wallets list's own load() against a following() request the
// test answers by hand, in whatever order it likes.
const list = read('../src/components/CopiedWallets.jsx')
const loader = list.slice(list.indexOf('  const load = useCallback(() => {'), list.indexOf('  // Unmounting moves the number on'))

function copiedWallets() {
  const state = { rows: [], error: null, loading: true, count: null }
  const requests = []
  const seq = { current: 0 }
  const api = {
    following: () => new Promise((resolve, reject) => { requests.push({ resolve, reject }) }),
  }
  const load = new Function(
    'useCallback', 'seq', 'api', 'setRows', 'setError', 'setLoading', 'onCount', 'onChange',
    `${loader}; return load`,
  )(
    (fn) => fn, seq, api,
    (v) => { state.rows = v },
    (v) => { state.error = v },
    (v) => { state.loading = v },
    (n) => { state.count = n },
    undefined,
  )
  return { state, requests, seq, load }
}

const OLD = [{ trader_address: '0xold', paused: 0 }]
const NEW = [{ trader_address: '0xold', paused: 1 }, { trader_address: '0xnew', paused: 0 }]

test('an older reload that answers last leaves the newer rows in place', async () => {
  const w = copiedWallets()
  w.load()
  w.load()
  w.requests[1].resolve(NEW)
  await settle()
  assert.deepEqual(w.state, { rows: NEW, error: null, loading: false, count: 2 })
  w.requests[0].resolve(OLD)
  await settle()
  assert.deepEqual(w.state, { rows: NEW, error: null, loading: false, count: 2 })
})

test('reloads that answer in order apply the last one', async () => {
  const w = copiedWallets()
  w.load()
  w.load()
  w.requests[0].resolve(OLD)
  await settle()
  assert.equal(w.state.loading, true)   // the newest read is still out
  assert.deepEqual(w.state.rows, [])
  w.requests[1].resolve(NEW)
  await settle()
  assert.deepEqual(w.state, { rows: NEW, error: null, loading: false, count: 2 })
})

test('an older reload that fails last raises no error over the newer rows', async () => {
  const w = copiedWallets()
  w.load()
  w.load()
  w.requests[1].resolve(NEW)
  await settle()
  w.requests[0].reject(new Error('timeout'))
  await settle()
  assert.deepEqual(w.state, { rows: NEW, error: null, loading: false, count: 2 })
})

test('the newest reload still reports its own failure', async () => {
  const w = copiedWallets()
  w.load()
  const failure = new Error('timeout')
  w.requests[0].reject(failure)
  await settle()
  assert.deepEqual(w.state, { rows: [], error: failure, loading: false, count: null })
})

test('a read still in flight when the list unmounts reports nothing', async () => {
  assert.match(list, /useEffect\(\(\) => \{ load\(\); return \(\) => \{ seq\.current \+= 1 \} \}, \[load\]\)/)
  const w = copiedWallets()
  w.load()
  w.seq.current += 1                     // what the effect's cleanup does
  w.requests[0].resolve(OLD)
  await settle()
  assert.deepEqual(w.state, { rows: [], error: null, loading: true, count: null })
})

// The Account page's breakdown read, cut out of its effect.
const user = read('../src/pages/User.jsx')
const breakdown = user.slice(user.indexOf('    let live = true'), user.indexOf('  }, [reload])'))

function breakdownRead() {
  const state = { byWallet: null, error: null }
  const requests = []
  const api = {
    pnlByWallet: () => new Promise((resolve, reject) => { requests.push({ resolve, reject }) }),
  }
  const run = new Function('api', 'setByWallet', 'setByWalletError', breakdown)
  const start = () => run(api, (v) => { state.byWallet = v }, (v) => { state.error = v })
  return { state, requests, start }
}

test('a failed breakdown read is reported as a failure and leaves the list unset', async () => {
  const b = breakdownRead()
  b.start()
  const failure = new Error('timeout')
  b.requests[0].reject(failure)
  await settle()
  assert.deepEqual(b.state, { byWallet: null, error: failure })
})

test('a retry clears the failure and a good answer fills the breakdown', async () => {
  const b = breakdownRead()
  const stop = b.start()
  b.requests[0].reject(new Error('timeout'))
  await settle()
  stop()
  b.start()
  assert.equal(b.state.error, null)
  b.requests[1].resolve([{ trader_address: 'manual', closed_trades: 1, win_rate: 1, realized_pnl: 2 }])
  await settle()
  assert.equal(b.state.error, null)
  assert.equal(b.state.byWallet.length, 1)
})

test('the answer to a superseded breakdown read is dropped', async () => {
  const b = breakdownRead()
  const stop = b.start()
  stop()
  b.start()
  b.requests[1].resolve([])
  await settle()
  b.requests[0].reject(new Error('timeout'))
  await settle()
  assert.deepEqual(b.state, { byWallet: [], error: null })
})

test('the breakdown shows the shared error state with a retry ahead of the empty state', () => {
  assert.doesNotMatch(user, /setByWallet\(\[\]\)/)
  const folder = user.slice(user.indexOf('<Folder id="user-breakdown"'))
  const error = folder.indexOf('byWalletError ? (')
  const empty = folder.indexOf('No closed positions yet')
  assert.ok(error > 0 && error < empty)
  assert.match(folder, /<ErrorState\s+title="Couldn’t load the breakdown"\s+error=\{byWalletError\}\s+onRetry=\{\(\) => setReload\(\(n\) => n \+ 1\)\}/)
  assert.match(user, /import \{ ErrorState, Notice, StateBlock, describeError \} from '\.\.\/components\/UiState'/)
})
