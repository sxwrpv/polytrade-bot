/* Saved wallets for the public screener.
 *
 * localStorage on this browser only — the screener is anonymous by design, so
 * there is no account to hang a list off and nothing is ever sent anywhere.
 * The UI says so rather than letting the list read as synced.
 *
 * Each record snapshots the wallet's name and PnL at save time. The cache
 * behind the board rotates, and a wallet saved last week may not be on the
 * current page at all — without the snapshot the list would show a bare
 * address. Where the wallet IS on screen the live figure wins; where it is not,
 * the snapshot shows, labelled.
 */
const KEY = 'polytrade.screener.saved'
export const MAX_SAVED = 200
const ADDRESS = /^0x[0-9a-fA-F]{40}$/

const listeners = new Set()
let cache = null

function read() {
  if (cache) return cache
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '[]')
    cache = Array.isArray(raw) ? raw.filter((r) => r && ADDRESS.test(r.w)) : []
  } catch {
    cache = []
  }
  return cache
}

function write(rows) {
  cache = rows
  try {
    localStorage.setItem(KEY, JSON.stringify(rows))
  } catch {
    /* quota or a locked-down browser: the list still works for this session */
  }
  listeners.forEach((fn) => fn(rows))
}

/* Newest first. Records are stored newest-first too, so equal timestamps —
   two saves inside the same millisecond, which is easy to do — keep their
   insertion order under a stable sort instead of silently inverting. */
export const savedList = () => [...read()].sort((a, b) => (b.savedAt ?? 0) - (a.savedAt ?? 0))
export const savedCount = () => read().length
export const isSaved = (address) => ADDRESS.test(address || '') && read().some((r) => r.w === address.toLowerCase())

export function toggleSaved(address, meta = {}) {
  if (!ADDRESS.test(address || '')) return false
  const w = address.toLowerCase()
  const rows = read()
  if (rows.some((r) => r.w === w)) {
    write(rows.filter((r) => r.w !== w))
    return false
  }
  if (rows.length >= MAX_SAVED) return false
  write([{
    w,
    name: meta.name ?? null,
    pnl: Number.isFinite(meta.pnl) ? meta.pnl : null,
    period: meta.period ?? null,
    savedAt: Date.now(),
  }, ...rows])
  return true
}

export function clearSaved() { write([]) }

export function subscribeSaved(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}
