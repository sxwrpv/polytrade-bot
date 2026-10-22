// Pure rules for the Withdraw panel (components/Withdraw.jsx). The server
// checks every one of them again; these keep the form honest before it asks.

export const ADDRESS_SHAPE = /^0x[0-9a-fA-F]{40}$/
// The server accepts Telegram launch data up to 300 seconds old for a
// withdrawal; the panel asks for a relaunch a minute before that runs out.
export const STEP_UP_WARN_AGE = 240
export const DRAFT_TTL_MS = 30 * 60 * 1000
const DRAFT_KEY = 'withdraw-draft'
// Statuses that stop changing on their own (attention waits for an operator).
const SETTLED = new Set(['confirmed', 'failed', 'attention', 'refunded'])
const SENDING = new Set(['requested', 'submitting', 'submitted'])

/** "12.5" -> "12.50". Anything but a positive amount in whole cents -> null. */
export function normalizeAmount(text) {
  const t = String(text ?? '').trim().replace(/^\$/, '')
  if (!/^\d+(\.\d{1,2})?$/.test(t)) return null
  const value = Number(t)
  return value > 0 ? value.toFixed(2) : null
}

export const isAddressShape = (text) => ADDRESS_SHAPE.test(String(text ?? '').trim())

export function lastFourMatches(address, typed) {
  const want = String(address || '').slice(-4).toLowerCase()
  return want.length === 4 && String(typed || '').trim().toLowerCase() === want
}

/** 0x and groups of four, so two addresses can be compared by eye. */
export function groupAddress(address) {
  const a = String(address || '')
  if (!ADDRESS_SHAPE.test(a)) return [a]
  return [a.slice(0, 2), ...a.slice(2).match(/.{4}/g)]
}

export const shortAddress = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')

/** Seconds since Telegram signed this launch's initData; null outside Telegram. */
export function launchAge(webApp, nowMs = Date.now()) {
  const signedAt = Number(webApp?.initDataUnsafe?.auth_date)
  if (!webApp?.initData || !Number.isFinite(signedAt) || signedAt <= 0) return null
  return Math.max(0, Math.floor(nowMs / 1000) - signedAt)
}

/** One key per confirm screen: resending from that screen repeats the same withdrawal. */
export function newIdempotencyKey(cryptoImpl = globalThis.crypto) {
  if (cryptoImpl?.randomUUID) return cryptoImpl.randomUUID()
  const bytes = new Uint8Array(16)
  cryptoImpl.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

// The address and amount survive a relaunch from Telegram (which a fresh
// verification needs), for half an hour. Neither is a secret.
export function saveDraft(storage, draft, nowMs = Date.now()) {
  try {
    storage?.setItem(DRAFT_KEY, JSON.stringify({ ...draft, at: nowMs }))
  } catch {
    /* storage unavailable: the draft is a convenience */
  }
}

export function loadDraft(storage, nowMs = Date.now()) {
  try {
    const d = JSON.parse(storage?.getItem(DRAFT_KEY) || 'null')
    if (!d || !(nowMs - Number(d.at) <= DRAFT_TTL_MS)) return null
    return { destination: String(d.destination || ''), amount: String(d.amount || '') }
  } catch {
    return null
  }
}

export function clearDraft(storage) {
  try {
    storage?.removeItem(DRAFT_KEY)
  } catch {
    /* nothing to clear */
  }
}

export const isSettled = (status) => SETTLED.has(status)

const clock = (iso) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** How a withdrawal reads: the pill's label and tone, which of the three
    stages it is in (1 sending, 2 converting, 3 delivered), and a sentence. */
export function describeStatus(w) {
  const amount = `$${w?.amount ?? ''}`
  if (SENDING.has(w?.status)) {
    if (w.error && w.held_until) {
      return { step: 1, tone: 'warn', label: 'Not confirmed',
        text: `This transfer has not gone through. ${amount} stays held until ${clock(w.held_until)} while PolyTrade makes sure, then it is released.` }
    }
    return { step: 1, tone: 'processing', label: 'Sending',
      text: `Sending ${amount} from your PolyTrade wallet.` }
  }
  switch (w?.status) {
    case 'bridging':
      return { step: 2, tone: 'processing', label: 'Converting',
        text: `${amount} left your wallet. The bridge is converting it to USDC.` }
    case 'confirmed':
      return { step: 3, tone: 'success', label: 'Delivered',
        text: `${w.delivered ?? w.amount} USDC arrived at ${shortAddress(w.destination)} on Polygon.` }
    case 'attention':
      return { step: 2, tone: 'warn', label: 'Needs a check',
        text: `${amount} left your wallet, and the bridge has not confirmed delivery yet. PolyTrade is following up and will message you in Telegram.` }
    case 'refunded':
      return { step: 0, tone: 'warn', label: 'Returned',
        text: 'The bridge provider returned this withdrawal.' }
    default:
      return { step: 0, tone: 'error', label: 'Not sent',
        text: `${amount} stayed in your PolyTrade wallet.${w?.error ? ` ${w.error}` : ''}` }
  }
}

/** One stage's look in the three-step list, from describeStatus. */
export function stageState(stage, info) {
  if (info.tone === 'error' || info.step === 0) return stage === 1 ? 'error' : 'todo'
  if (stage < info.step) return 'done'
  if (stage > info.step) return 'todo'
  if (info.tone === 'success') return 'done'
  return info.tone === 'warn' ? 'warn' : 'active'
}
