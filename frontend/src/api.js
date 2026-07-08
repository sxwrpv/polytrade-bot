// fetch() wrappers for the backend. Auth = secret session token (Bearer),
// issued once at wallet creation or re-issued by Telegram login — never the
// wallet address, which is public on-chain data.
const KEY = 'session' // JSON {address, token}

function load() {
  try {
    return JSON.parse(localStorage.getItem(KEY) || 'null')
  } catch {
    return null
  }
}
export const getSession = load
export const getWallet = () => load()?.address || null
export const saveSession = (s) => localStorage.setItem(KEY, JSON.stringify(s))
export const clearWallet = () => localStorage.removeItem(KEY)

async function req(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  const s = load()
  if (s?.token) headers['Authorization'] = `Bearer ${s.token}`
  const r = await fetch(`/api${path}`, { ...opts, headers })
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
  exportKey: () => req('/user/export-key', { method: 'POST' }),
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
  closePosition: (id) => req(`/positions/${id}/close`, { method: 'POST' }),
  // sell a wallet holding the bot didn't open (marked MANUAL on the card)
  closeExternal: (tokenId) =>
    req('/positions/close-external', { method: 'POST', body: JSON.stringify({ token_id: tokenId }) }),
}
