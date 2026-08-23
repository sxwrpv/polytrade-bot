// fetch() wrappers for the backend.
//
// Auth is an HttpOnly session cookie set by the server — this file deliberately
// holds NO credential. Script running on the page (an XSS, a bad dependency)
// cannot read the session, which is exactly why the old localStorage Bearer
// token was removed. Only the public wallet address is cached locally, purely
// so the UI can render before /me returns; it grants nothing on its own.
const KEY = 'session' // JSON {address} — public data only, never a secret
export const CURRENT_TERMS_VERSION = '2026-08-14'
export const CURRENT_FUNDING_ACK_VERSION = '2026-08-14'

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
    const error = new Error(body.detail || r.statusText)
    error.status = r.status
    throw error
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
  linkTelegram: (initData) =>
    req('/auth/link-telegram', { method: 'POST', body: JSON.stringify({ init_data: initData }) }),
  // user
  createWallet: (body) => req('/user/create-wallet', { method: 'POST', body: JSON.stringify(body) }),
  me: (balance = false) => req(`/user/me${balance ? '?balance=true' : ''}`),
  pnl: (period = '30d') => req(`/user/pnl?period=${period}`),
  equitySeries: (period = '7d') => req(`/user/equity-series?period=${period}`),
  pnlByWallet: () => req('/user/pnl/by-wallet'),
  getSettings: () => req('/user/settings'),
  activity: (limit = 30) => req(`/user/activity?limit=${limit}`),
  depositAddress: () => req('/user/deposit-address'),
  acknowledgeFunding: (body) => req('/user/funding-acknowledgement', {
    method: 'POST', body: JSON.stringify(body),
  }),
  settings: (body) => req('/user/settings', { method: 'POST', body: JSON.stringify(body) }),
  // Key export needs a fresh Telegram step-up proof, not just the session —
  // pass the CURRENT initData so the backend can re-verify the human.
  exportKey: (initData) =>
    req('/user/export-key', { method: 'POST', body: JSON.stringify({ init_data: initData }) }),
  // Already-copied wallets and their controls. Discovery lives on the public
  // read-only /screener surface rather than in the authenticated Mini App.
  following: () => req('/traders/following'),
  followSettings: (addr, body) => req(`/traders/${addr}/settings`, { method: 'POST', body: JSON.stringify(body) }),
  unfollow: (addr) => req(`/traders/${addr}/follow`, { method: 'DELETE' }),
  follow: (addr, body = {}) => req(`/traders/${addr}/follow`, { method: 'POST', body: JSON.stringify(body) }),
  telemetryEvent: (body) => req('/telemetry/events', {
    method: 'POST', body: JSON.stringify(body),
  }),
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
