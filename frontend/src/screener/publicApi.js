/* Client for the anonymous, read-only screener API.
 *
 * Two deliberate properties:
 *
 *   credentials: 'omit' — no session cookie is ever sent. The public screener
 *   is anonymous, so it has nothing to authenticate with, and keeping it that
 *   way is what lets the same bundle run on screener.polytradebot.live without
 *   relaxing SameSite on the real session cookie.
 *
 *   VITE_API_BASE — the API origin is configurable at build time. Same-origin
 *   ('/api', the default) today; an absolute https://polytradebot.live/api on
 *   the subdomain, with no code change.
 */
const API_BASE = import.meta.env?.VITE_API_BASE || '/api'

async function get(path, params = {}) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === '' || value == null) continue
    query.set(key, String(value))
  }
  const suffix = query.toString()
  const response = await fetch(`${API_BASE}${path}${suffix ? `?${suffix}` : ''}`, {
    credentials: 'omit',
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const error = new Error(body.detail || response.statusText)
    error.status = response.status
    throw error
  }
  return response.json()
}

export const publicApi = {
  wallets: (params) => get('/public/screener/wallets', params),
  wallet: (address, params) => get(`/public/screener/wallets/${address}`, params),
  provenance: () => get('/public/screener/provenance'),
}
