// Copy sizing as the engine does it (backend/core/copy_engine.py), for the
// Mini App's wallet settings and the homepage calculator. Two modes:
//   position_share  your copy = their position × your copy ratio
//   capital_share   your copy = the share of their free cash the bet took
//                   × your free cash × your multiplier
// Both then stop at your max per position and your free cash, and a copy
// under your minimum copy size is skipped.

export const POSITION_SHARE = 'position_share'
export const CAPITAL_SHARE = 'capital_share'

// Multiplier slider stops: fine steps at the low end, wide ones up to 100×.
// The ends match the API's bounds (routes_traders.py, 0.1 to 100).
export const MULTIPLIER_STOPS = [
  0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 75, 100,
]

/** Index of the stop closest to value, for a slider that moves over stops. */
export function nearestStop(stops, value) {
  const v = Number(value)
  let best = 0
  for (let i = 1; i < stops.length; i += 1) {
    if (Math.abs(stops[i] - v) < Math.abs(stops[best] - v)) best = i
  }
  return best
}

/** The share (0..1) of their free cash a bet took. The cash is what was left
 *  after the bet, so the cash before it is that plus the bet:
 *  $2,000 bet with $8,000 left = 20%. Null when either figure is unusable,
 *  the same as cash_share in the engine. */
export function cashShare(bet, cashLeft) {
  if (bet == null || cashLeft == null) return null
  const b = Number(bet)
  const c = Number(cashLeft)
  if (!Number.isFinite(b) || !Number.isFinite(c) || b <= 0 || c < 0) return null
  return b / (c + b)
}

/** Capital-share copy for a bet that took `share` (0..1) of their cash. */
export function capitalShareCopy({ share, yourCash, multiplier, cap, minCopy }) {
  const raw = share * yourCash * multiplier
  const limit = Math.min(cap, yourCash)
  const order = Math.min(raw, limit)
  if (!(order >= minCopy)) return { raw, order: 0, state: 'skipped' }
  return { raw, order, state: raw > limit ? 'capped' : 'copied' }
}
