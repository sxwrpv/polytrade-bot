// Links to a market on polymarket.com.
//
// /market/<market slug> resolves to the market's own page inside its event,
// for single- and multi-outcome events alike. /event/ takes the EVENT slug, so
// a market slug there opened an empty page for every market that belongs to a
// multi-outcome event (elections, Fed rates, sports; seen 2026-10-22).
export function marketUrl(slug) {
  return `https://polymarket.com/market/${encodeURIComponent(slug)}`
}

// Inside Telegram, external pages open through WebApp.openLink, Telegram's
// documented call for leaving a Mini App. Everywhere else the link keeps its
// own target="_blank" behaviour.
export function openMarketLink(event) {
  const tg = window.Telegram?.WebApp
  if (!tg?.initData || typeof tg.openLink !== 'function') return
  event.preventDefault()
  tg.openLink(event.currentTarget.href)
}
