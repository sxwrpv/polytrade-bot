/* Loader for the Copy Score overlay.
 *
 * The overlay is ~3MB of JSON (well under a megabyte over the wire once Caddy
 * compresses it), so it is NOT on the critical path. The live board renders
 * first from PolyTrade's API; this loads alongside and hydrates the Copy Score
 * column when it arrives. A board that works without the overlay is the point:
 * if this fetch fails, every money column is still correct and the score column
 * simply reads "not scored".
 *
 * It is a static asset rather than an API route on purpose. PolyTrade's public
 * screener API promises it publishes no composite score, and that promise is
 * documented and tested. This number is Polycopy's, not PolyTrade's, so it
 * travels separately and carries its own provenance.
 */

const ASSET = '/screener-cohort.json'

let pending = null

/** Load once per page. Repeat callers share the same promise. */
export function loadCohort() {
  if (!pending) {
    pending = fetch(ASSET, { credentials: 'omit', headers: { Accept: 'application/json' } })
      .then((response) => {
        if (!response.ok) throw new Error(`cohort ${response.status}`)
        return response.json()
      })
      .then((raw) => index(raw))
      .catch((problem) => {
        // Let a later mount retry rather than caching the failure forever, and
        // resolve to null so the board treats it as "no score available".
        pending = null
        if (import.meta.env?.DEV) console.warn('Copy Score overlay unavailable:', problem)
        return null
      })
  }
  return pending
}

/** Address → row, plus the metadata every score display has to state. */
function index(raw) {
  const byAddress = new Map()
  for (const trader of raw.traders ?? []) {
    byAddress.set(String(trader.w).toLowerCase(), trader)
  }
  return {
    meta: raw.meta ?? {},
    traders: raw.traders ?? [],
    byAddress,
    spark: raw.spark ?? {},
    wow: raw.wow ?? {},
    wowAnchor: raw.wowAnchor ?? null,
    copyDelta: raw.copyDelta ?? {},
    angles: raw.angles ?? [],
    groups: raw.groups ?? [],
    structures: raw.structures ?? [],
  }
}

/** The cohort row for one address, or null when the cohort does not cover it.
 *  Null here means "not scored", which is not the same as a middling score. */
export function cohortRow(cohort, address) {
  if (!cohort || !address) return null
  return cohort.byAddress.get(String(address).toLowerCase()) ?? null
}

/**
 * Attach the overlay to live rows.
 *
 * The live figures always win: PnL, win rate and volume on screen are
 * PolyTrade's own, refreshed on its own schedule. Only the score, the
 * classifiers and the sparkline — none of which PolyTrade computes — come from
 * the cohort, and a wallet the cohort does not cover keeps a null score rather
 * than a zero.
 */
export function withCopyScore(rows, cohort) {
  if (!cohort) return rows.map((row) => ({ ...row, copyClass: null, copyNet: null, scored: false }))
  return rows.map((row) => {
    const match = cohortRow(cohort, row.address)
    if (!match) return { ...row, copyClass: null, copyNet: null, scored: false }
    return {
      ...row,
      copyClass: match.copyClass ?? null,
      copyNet: match.copyNet ?? null,
      mm: match.mm ?? null,
      arb: match.arb ?? null,
      freq: match.freq ?? null,
      cats: match.cats ?? [],
      avgSize: match.avgSize ?? null,
      niche: match.niche ?? null,
      spark: cohort.spark[String(row.address).toLowerCase()] ?? null,
      scoreMove: cohort.copyDelta?.[String(row.address).toLowerCase()] ?? null,
      scored: true,
    }
  })
}

/** How many of the rows on screen the cohort actually covers, so the board can
 *  say "score for 12 of 50" instead of leaving the gaps unexplained. */
export const scoredCount = (rows) => rows.filter((row) => row.scored).length

/**
 * Put a cohort row into the same shape the live rows use.
 *
 * One table, one row shape: the alternative is two renderers drifting apart.
 * Fields the cohort does not carry stay null rather than being filled with a
 * plausible zero — `activePositions` is genuinely unknown here, and coverage
 * describes the cohort's own active-day count, not PolyTrade's fetched
 * history, so it is labelled differently on purpose.
 */
export function fromCohortRow(trader, period, spark = null, scoreMove = null) {
  const windowed = (d7, d30, all) => (period === 'd7' ? d7 : period === 'd30' ? d30 : all)
  const pnl = windowed(trader.d7, trader.d30, trader.pnl)
  const volume = windowed(trader.d7Vol, trader.d30Vol, trader.vol)
  const days = period === 'd7' ? 7 : period === 'd30' ? 30 : null
  return {
    address: trader.w,
    displayName: trader.name || null,
    xUsername: null,
    verified: false,
    pnl: pnl ?? null,
    winRate: trader.winRate ?? null,
    volume: volume ?? null,
    openValue: trader.openVal ?? null,
    // PolyTrade's cache holds this; the cohort does not. Absent, not zero.
    activePositions: null,
    consistencyRatio: null,
    fillExitRatio: null,
    historyDays: trader.activeDays ?? null,
    historyPartial: days != null && trader.activeDays != null && trader.activeDays < days,
    coverage: trader.activeDays == null
      ? 'UNAVAILABLE'
      : days == null
        ? `${Number(trader.activeDays).toLocaleString('en-US')}D ACTIVE`
        : trader.activeDays < days
          ? `PARTIAL · ${trader.activeDays}D OF ${days}D`
          : `~${days}D OBSERVED`,
    refreshedAt: null,
    periodDays: days,
    // The cohort carries weekly points, not the daily series the live analysis
    // panel draws, so the panel says it has none rather than drawing weeks
    // under a daily heading.
    dailyPnl: null,
    lastTradeDay: trader.lastTradeDay ?? null,
    copyClass: trader.copyClass ?? null,
    copyNet: trader.copyNet ?? null,
    mm: trader.mm ?? null,
    arb: trader.arb ?? null,
    freq: trader.freq ?? null,
    cats: trader.cats ?? [],
    avgSize: trader.avgSize ?? null,
    niche: trader.niche ?? null,
    maxDD: trader.maxDD ?? null,
    maker: trader.maker ?? null,
    spark,
    scoreMove,
    scored: true,
  }
}

/**
 * Put a displayed row back into cohort shape for the CSV writer.
 *
 * The row on screen already resolved PnL and volume for the selected window,
 * so all three window slots carry that same figure: the export states its
 * period in the filename and the `period` column, and there is no second
 * window in the file to confuse it with.
 */
export function toCohortShape(row) {
  return {
    w: row.address,
    name: row.displayName,
    copyClass: row.copyClass ?? null,
    copyNet: row.copyNet ?? null,
    pnl: row.pnl ?? null,
    d7: row.pnl ?? null,
    d30: row.pnl ?? null,
    vol: row.volume ?? null,
    d7Vol: row.volume ?? null,
    d30Vol: row.volume ?? null,
    openVal: row.openValue ?? null,
    winRate: row.winRate ?? null,
    activeDays: row.historyDays ?? null,
    avgSize: row.avgSize ?? null,
    maxDD: row.maxDD ?? null,
    maker: row.maker ?? null,
    niche: row.niche ?? null,
    cats: row.cats ?? [],
    lastTradeDay: row.lastTradeDay ?? null,
    mm: row.mm ?? null,
    arb: row.arb ?? null,
    freq: row.freq ?? null,
  }
}
