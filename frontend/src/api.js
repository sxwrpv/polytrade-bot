// fetch() wrappers for the backend.
//
// Auth is an HttpOnly session cookie set by the server — this file deliberately
// holds NO credential. Script running on the page (an XSS, a bad dependency)
// cannot read the session, which is exactly why the old localStorage Bearer
// token was removed. Only the public wallet address is cached locally, purely
// so the UI can render before /me returns; it grants nothing on its own.
const KEY = 'session' // JSON {address} — public data only, never a secret

function load() {
  try {
    return JSON.parse(localStorage.getItem(KEY) || 'null')
  } catch {
    return null
  }
}
export const getSession = load
export const getWallet = () => load()?.address || null
export const saveSession = (s) =>
  localStorage.setItem(KEY, JSON.stringify({ address: s?.address ?? null }))
export const clearWallet = () => localStorage.removeItem(KEY)

async function req(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  // same-origin: the SPA is served by the same FastAPI app, so the session
  // cookie rides along automatically and no token is ever handled in JS.
  const r = await fetch(`/api${path}`, { ...opts, headers, credentials: 'same-origin' })
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.detail || r.statusText)
  }
  return r.json()
}

// Telegram haptic feedback — no-op outside Telegram or on old clients.
export function haptic(type = 'success') {
  try {
    window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred(type)
  } catch {
    /* unsupported client */
  }
}

export const api = {
  // auth
  logout: () => req('/auth/logout', { method: 'POST' }),
  telegramAuth: (initData) =>
    req('/auth/telegram', { method: 'POST', body: JSON.stringify({ init_data: initData }) }),
  // user
  createWallet: (body) => req('/user/create-wallet', { method: 'POST', body: JSON.stringify(body) }),
  me: (balance = false) => req(`/user/me${balance ? '?balance=true' : ''}`),
  pnl: (period = '30d') => req(`/user/pnl?period=${period}`),
  equitySeries: (period = '7d') => req(`/user/equity-series?period=${period}`),
  pnlByWallet: () => req('/user/pnl/by-wallet'),
  getSettings: () => req('/user/settings'),
  activity: (limit = 30) => req(`/user/activity?limit=${limit}`),
  depositAddress: () => req('/user/deposit-address'),
  settings: (body) => req('/user/settings', { method: 'POST', body: JSON.stringify(body) }),
  // Key export needs a fresh Telegram step-up proof, not just the session —
  // pass the CURRENT initData so the backend can re-verify the human.
  exportKey: (initData) =>
    req('/user/export-key', { method: 'POST', body: JSON.stringify({ init_data: initData }) }),
  // traders — leaderboard doubles as the wallet screener: pass sort/limit/offset
  // plus any number of `<column>_min` / `<column>_max` filter keys (see
  // backend/core/trader_stats.py _FILTERABLE_COLUMNS); they all combine with AND.
  leaderboard: (params = {}) => {
    const q = new URLSearchParams()
    Object.entries({ sort: 'consistency', limit: 50, ...params }).forEach(([k, v]) => {
      if (v !== '' && v != null) q.set(k, v)
    })
    return req(`/traders/leaderboard?${q.toString()}`)
  },
  following: () => req('/traders/following'),
  trader: (addr) => req(`/traders/${addr}`),
  follow: (addr, body) => req(`/traders/${addr}/follow`, { method: 'POST', body: JSON.stringify(body) }),
  followSettings: (addr, body) => req(`/traders/${addr}/settings`, { method: 'POST', body: JSON.stringify(body) }),
  unfollow: (addr) => req(`/traders/${addr}/follow`, { method: 'DELETE' }),
  // positions
  openPositions: () => req('/positions/open'),
  closedPositions: () => req('/positions/closed'),
  closePosition: (id, acceptableSlippagePct) => req(`/positions/${id}/close`, {
    method: 'POST',
    body: JSON.stringify({ acceptable_slippage_pct: acceptableSlippagePct }),
  }),
  // Sell a live wallet holding that has no active bot tracking row.
  closeExternal: (tokenId, acceptableSlippagePct) =>
    req('/positions/close-external', {
      method: 'POST',
      body: JSON.stringify({
        token_id: tokenId,
        acceptable_slippage_pct: acceptableSlippagePct,
      }),
    }),
}
