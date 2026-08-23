import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import { publicApi } from './publicApi'
import RangeFilter from './RangeFilter'
import SeriesChart from '../components/SeriesChart'
import { isSaved, toggleSaved, savedList, savedCount, subscribeSaved, clearSaved } from './savedWallets'
import SiteSwitcher from '../components/SiteSwitcher'
import {
  DEFAULT_FILTERS,
  DEFAULT_PERIOD,
  DEFAULT_SORT,
  PERIODS,
  POLYMARKET_PROFILE,
  SORTS,
  activeFilterChips,
  botDeepLink,
  buildPublicQuery,
  formatMetric,
  isAddress,
  paginationLabel,
  walletRows,
  CURVES,
  curveFrom,
  dayOutcomes,
  COLUMN_SORT,
  encodeScreenerState,
  decodeScreenerState,
  walletsToCsv,
} from './screenerModel'
import { loadCohort, withCopyScore, scoredCount, fromCohortRow, toCohortShape } from './cohort'
import { CopyChip, ScoreMove, MirrorChip, Sparkline, BandRow } from './CopyScore'
import {
  COHORT_FILTERS,
  COHORT_PERIOD_LABEL,
  CLASS_ORDER,
  RECOMMENDED,
  STALE_DAYS,
  bandRuns,
  buildCohortBoard,
  classDef,
  cohortAge,
  cohortFilterChips,
  cohortToCsv,
  money as cohortMoney,
  pnlIn,
  roiIn,
  signedMoney,
  signedPercent,
  staleHeldBack,
  toCohortPeriod,
  volumeIn,
} from './cohortModel'

const short = (address) => `${address.slice(0, 6)}…${address.slice(-4)}`

/* Orderings the live API cannot serve.
 *
 * `SORTS` above stays exactly what the public API ranks by — pnl, winrate,
 * volume — because buildPublicQuery must keep rejecting anything else. These
 * two are computed from the Copy Score cohort instead, so choosing one swaps
 * the board's source rather than adding a parameter to the request. The board
 * says which source it is reading; it never mixes them in one table. */
export const COHORT_SORTS = [
  ['copy', 'Copy Score'],
  ['roi', 'ROI'],
]
const isCohortSort = (sort) => COHORT_SORTS.some(([key]) => key === sort)

/** Categories the cohort slices wallets by, in the order the board shows. */
const CATEGORIES = ['all', 'Sports', 'Politics', 'Crypto', 'World', 'Economy', 'Tech', 'Culture', 'Weather']

const refreshed = (value) => {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`
}

export default function ScreenerPage() {
  // A shared link must reproduce the board it was copied from, so the controls
  // hydrate from the query string before the first request goes out.
  const initial = useMemo(() => decodeScreenerState(window.location.search), [])
  const [period, setPeriod] = useState(initial.period)
  const [sort, setSort] = useState(initial.sort)
  const [search, setSearch] = useState(initial.search)
  const [filters, setFilters] = useState(() => ({ ...initial.filters }))
  const [completeHistoryOnly, setCompleteHistoryOnly] = useState(initial.completeHistoryOnly)
  const [savedTick, setSavedTick] = useState(0)
  const [showSaved, setShowSaved] = useState(false)
  const [payload, setPayload] = useState(null)
  const [state, setState] = useState('loading')
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(null)
  const [offset, setOffset] = useState(0)
  // The Copy Score overlay. Null until it arrives, and null forever if it
  // fails — the board is fully usable either way, with the score column
  // reading "not scored" instead of a number nobody can stand behind.
  const [cohort, setCohort] = useState(null)
  const [bands, setBands] = useState(initial.bands)
  const [category, setCategory] = useState(initial.category)
  const [direction, setDirection] = useState(initial.direction)
  const [visible, setVisible] = useState(50)

  useEffect(() => subscribeSaved(() => setSavedTick((n) => n + 1)), [])

  // Off the critical path on purpose: the live board renders first and this
  // hydrates the score column when it lands.
  useEffect(() => {
    let alive = true
    loadCohort().then((loaded) => { if (alive) setCohort(loaded) })
    return () => { alive = false }
  }, [])

  const cohortMode = isCohortSort(sort)
  const cohortPeriod = toCohortPeriod(period)
  // `all` is a lifetime total, so the heading says ALL rather than mislabelling
  // it with the live board's 90D.
  const windowLabel = cohortMode ? COHORT_PERIOD_LABEL[cohortPeriod] : period.toUpperCase()

  // replaceState, not push: filtering is not navigation, and a history entry
  // per slider drag would make Back useless.
  useEffect(() => {
    const q = encodeScreenerState({
      period, sort, search, filters, completeHistoryOnly, bands, category, direction,
    })
    window.history.replaceState(null, '', q ? `?${q}` : window.location.pathname)
  }, [period, sort, search, filters, completeHistoryOnly, bands, category, direction])

  const queryState = useMemo(() => {
    // A cohort ordering is not a query the API can answer; the board reads the
    // overlay directly instead of asking for a sort the route would reject.
    if (isCohortSort(sort)) return { query: null, validationError: '', cohortOrdered: true }
    try {
      return {
        query: buildPublicQuery({
          period, sort, search, filters, completeHistoryOnly, offset,
        }),
        validationError: '',
        cohortOrdered: false,
      }
    } catch (problem) {
      return {
        query: null,
        validationError: String(problem.message || problem),
        cohortOrdered: false,
      }
    }
  }, [completeHistoryOnly, filters, offset, period, search, sort])
  const query = queryState.query

  useEffect(() => {
    if (queryState.cohortOrdered) {
      // Nothing to fetch: the cohort effect below drives this board.
      setError('')
      setState(cohort ? 'ready' : 'loading')
      return undefined
    }
    if (!query) {
      setError(queryState.validationError)
      setState('invalid')
      return undefined
    }
    let alive = true
    setError('')
    setState('loading')
    const timer = setTimeout(() => {
      publicApi
        .wallets(query)
        .then((result) => {
          if (!alive) return
          setPayload(result)
          setState('ready')
        })
        .catch((problem) => {
          if (!alive) return
          setError(String(problem.message || problem))
          setState(problem.status === 429 ? 'throttled' : 'error')
        })
    }, 300)
    return () => { alive = false; clearTimeout(timer) }
  }, [query, queryState.validationError, queryState.cohortOrdered, cohort])

  /* Two boards, one table.
   *
   * Money orderings read PolyTrade's live cache and wear the Copy Score as an
   * overlay where the cohort covers the wallet. Copy Score and ROI orderings
   * read the cohort itself, because ranking by a figure the API does not hold
   * cannot be done a page at a time. Which one is on screen is stated above the
   * table — the two are never blended into one set of rows. */
  const liveRows = useMemo(
    () => withCopyScore(walletRows(payload), cohort),
    [payload, cohort],
  )
  const cohortRows = useMemo(() => {
    if (!cohortMode || !cohort) return []
    const term = String(search ?? '').trim().toLowerCase()
    const board = buildCohortBoard(cohort.traders, {
      metric: sort,
      period: cohortPeriod,
      category,
      asOf: cohort.meta.windowAnchor ?? cohort.meta.generatedAt?.slice(0, 10) ?? null,
      bands,
      filters,
      direction,
    })
    const matched = term
      ? board.filter((t) => (
        t.w.includes(term) || String(t.name ?? '').toLowerCase().includes(term)
      ))
      : board
    const key = (address) => String(address).toLowerCase()
    return matched.map((t) => fromCohortRow(
      t, cohortPeriod, cohort.spark[key(t.w)] ?? null, cohort.copyDelta?.[key(t.w)] ?? null,
    ))
  }, [cohort, cohortMode, sort, cohortPeriod, category, bands, filters, direction, search])

  const rows = cohortMode ? cohortRows : liveRows
  const chips = useMemo(
    () => (cohortMode
      ? cohortFilterChips({ metric: sort, period: cohortPeriod, category, bands, filters })
        .map((chip) => [chip.key, chip.text, chip.clearable])
      : activeFilterChips({ filters, period, completeHistoryOnly })
        .map(([key, text]) => [key, text, true])),
    [cohortMode, sort, cohortPeriod, category, bands, filters, completeHistoryOnly, period],
  )
  const age = useMemo(() => cohortAge(cohort?.meta?.generatedAt), [cohort])

  // The cohort board is client-side, so it grows in place rather than paging:
  // there is no request behind "show more".
  const shown = cohortMode ? rows.slice(0, visible) : rows
  const heldBack = useMemo(() => (
    cohortMode && cohort
      ? staleHeldBack(cohort.traders, {
        category,
        period: cohortPeriod,
        asOf: cohort.meta.windowAnchor ?? cohort.meta.generatedAt?.slice(0, 10) ?? null,
      })
      : 0
  ), [cohortMode, cohort, category, cohortPeriod])

  /* Clicking the active column flips direction; clicking another switches to
     it, starting descending because that is what a leaderboard means by "top".
     Only the two orderings this surface computes can flip — the API ranks
     descending only. */
  const changeColumnSort = (next) => {
    if (next === sort && isCohortSort(next)) {
      resetPage()
      setDirection((current) => (current === 'desc' ? 'asc' : 'desc'))
      return
    }
    changeSort(next)
  }
  const resetPage = useCallback(() => {
    setOffset(0)
    setSelected(null)
    setVisible(50)
  }, [])
  const changePeriod = (value) => { resetPage(); setPeriod(value) }
  const changeSort = (value) => {
    resetPage()
    // Direction only means something on a board this surface orders itself.
    // The API ranks descending only, so switching back to a money ordering
    // resets it rather than showing an arrow the request cannot honour.
    if (!isCohortSort(value)) setDirection('desc')
    setSort(value)
  }
  const changeSearch = (value) => { resetPage(); setSearch(value) }
  const changeCompleteHistory = (value) => { resetPage(); setCompleteHistoryOnly(value) }
  const changeCategory = (value) => { resetPage(); setCategory(value) }
  const setFilter = (key, value) => {
    resetPage()
    setFilters((current) => ({ ...current, [key]: value }))
  }
  const toggleBand = (band) => {
    resetPage()
    setBands((current) => {
      const next = new Set(current)
      if (next.has(band)) next.delete(band)
      else next.add(band)
      // An empty band set would render an empty board with no way back, so the
      // last band cannot be switched off.
      return next.size ? next : current
    })
  }
  const clearChip = (key) => {
    if (key === 'completeHistoryOnly') changeCompleteHistory(false)
    else if (key === 'category') changeCategory('all')
    else if (key === 'excludeHardToMirror') setFilter('excludeHardToMirror', false)
    else setFilter(key, '')
  }
  const clearAll = useCallback(() => {
    setOffset(0)
    setSelected(null)
    setVisible(50)
    setFilters({ ...DEFAULT_FILTERS, ...COHORT_FILTERS })
    setCompleteHistoryOnly(false)
    setCategory('all')
    setBands(new Set(RECOMMENDED))
  }, [])
  const pageSize = payload?.limit || query?.limit || 50
  const goToPage = (nextOffset) => {
    setSelected(null)
    setOffset(Math.max(0, nextOffset))
  }

  const exactLookup = isAddress(search) ? search.trim().toLowerCase() : null

  return (
    <div className="screener">
      <header className="screener-nav">
        <div className="screener-nav-primary">
          <a className="screener-brand" href="/" aria-label="PolyTrade home">
            <img className="brand-logo" src="/brand/polytrade-mark.png" alt="" />
            <span>PolyTrade</span>
            <span className="screener-brand-divider" />
            <span className="screener-brand-label">Wallet Screener</span>
          </a>
          <div className="screener-search">
            <label className="visually-hidden" htmlFor="screener-search">
              Search name, X handle or 0x address
            </label>
            <input
              id="screener-search" type="search" value={search}
              placeholder="Search a name, X handle, or 0x address…"
              onChange={(event) => changeSearch(event.target.value)}
            />
          </div>
        </div>

        <SiteSwitcher active="screener" />

        <nav className="screener-nav-links" aria-label="Product links">
          <a href="/docs/system-design">System design</a>
          <a className="btn screener-nav-cta" href={botDeepLink()}>Open Telegram ↗</a>
        </nav>
      </header>

      <div className="screener-shell">
        <section className="screener-intro">
          <p className="screener-eyebrow">PUBLIC WALLET RESEARCH</p>
          <h1>Wallet Screener</h1>
          <p className="screener-lede">
            Search Polymarket wallets on windowed statistics rebuilt from public trading
            history. Every figure states the period it covers and how much history backs
            it. Nothing here is a recommendation.
          </p>
        </section>

        <aside className="screener-sidebar" aria-label="Filters">
          <FilterRail
            period={period} onPeriod={changePeriod}
            sort={sort} onSort={changeSort}
            filters={filters} setFilter={setFilter}
            completeHistoryOnly={completeHistoryOnly}
            onCompleteHistoryOnly={changeCompleteHistory}
            chips={chips} onClearChip={clearChip} onClearAll={clearAll}
            cohortMode={cohortMode} hasCohort={Boolean(cohort)}
            bands={bands} onToggleBand={toggleBand}
            category={category} onCategory={changeCategory}
            windowLabel={windowLabel}
          />
        </aside>

        <main className="screener-main">
          {cohort && (
            <CohortNotice
              meta={cohort.meta} age={age} cohortMode={cohortMode}
              scored={scoredCount(rows)} total={rows.length}
            />
          )}

          <div className="results-toolbar">
            <span className="coverage">
              {cohortMode
                ? `${rows.length.toLocaleString()} wallets`
                : payload?.total != null ? `${payload.total.toLocaleString()} wallets` : ''}
            </span>
            <div className="results-actions">
              <button
                type="button"
                className={`btn btn-analyze${showSaved ? ' is-on' : ''}`}
                aria-pressed={showSaved}
                onClick={() => setShowSaved((v) => !v)}
              >{'\u2665'} SAVED {savedCount() ? `(${savedCount()})` : ''}</button>
              <button
                type="button" className="btn btn-analyze"
                disabled={rows.length === 0}
                title="Download this page exactly as filtered and ordered"
                onClick={() => downloadCsv(rows, period, { cohortMode, cohortPeriod, cohort })}
              >CSV</button>
              <button
                type="button" className="btn btn-analyze"
                title="Copy a link that reproduces this exact view"
                onClick={(e) => {
                  navigator.clipboard?.writeText(window.location.href).catch(() => {})
                  const b = e.currentTarget
                  b.textContent = 'COPIED'
                  setTimeout(() => { b.textContent = 'SHARE' }, 1400)
                }}
              >SHARE</button>
            </div>
          </div>

          <section
            className="screener-results" aria-label="Results"
            aria-busy={state === 'loading'}
          >
            {state === 'invalid' ? (
              <p className="screener-state" role="alert">Invalid filters: {error}</p>
            ) : state === 'throttled' ? (
              <p className="screener-state" role="status">
                The public screener is rate limited and this client has hit the limit. Wait a
                minute and try again.
              </p>
            ) : state === 'error' ? (
              <p className="screener-state" role="alert">Could not load wallets: {error}</p>
            ) : state === 'loading' ? (
              <p className="screener-state" role="status">Loading wallets…</p>
            ) : (
              <>
                {rows.length === 0 ? (
                  <p className="screener-state">
                    {cohortMode
                      ? emptyCohortMessage({ sort, bands, category })
                      : offset > 0 && payload?.total > 0
                        ? 'No wallets on this page. Use Previous to return to available results.'
                        : exactLookup
                          ? 'That wallet is not in the public screener cache yet.'
                          : 'No cached wallet matches these filters.'}
                  </p>
                ) : (
                  <ResultTable
                    rows={shown} period={period} windowLabel={windowLabel}
                    selected={selected} onSelect={setSelected}
                    sort={sort} direction={direction} onSort={changeColumnSort}
                    showScore={Boolean(cohort)} cohortMode={cohortMode}
                  />
                )}
                {cohortMode ? (
                  <CohortFooter
                    rows={rows} visible={visible} onMore={() => setVisible((n) => n + 20)}
                    heldBack={heldBack}
                  />
                ) : (
                  <ResultPagination
                    payload={payload} offset={offset} pageSize={pageSize} onPage={goToPage}
                  />
                )}
              </>
            )}
          </section>

          {showSaved && (
            <SavedPanel
              key={savedTick}
              rows={rows}
              onClose={() => setShowSaved(false)}
              onPick={(addr) => { setSelected(addr); setShowSaved(false) }}
            />
          )}

          {selected && (
            <WalletAnalysis
              row={rows.find((row) => row.address === selected) || null}
              period={period}
              onClose={() => setSelected(null)}
            />
          )}

          <Provenance provenance={payload?.provenance} />

          <footer className="screener-footer">
            <span>Public wallet data from Polymarket. Not financial advice.</span>
            <span>
              <a href="/docs/risk-and-security">Risk &amp; security</a>
              {' · '}
              <a href="/docs">Documentation</a>
            </span>
          </footer>
        </main>

        <aside className="screener-rail" aria-label="About these results">
          <div className="rail-title">Reading this table</div>
          <ul className="rail-notes">
            <li>Metrics cover the selected {windowLabel} window only.</li>
            <li>“—” means unavailable, not zero.</li>
            <li>“Partial” describes fetched trade history, nothing else.</li>
          </ul>

          {cohort && (
            <>
              <div className="rail-title">Copy Score bands</div>
              <ul className="rail-bands">
                {CLASS_ORDER.map((band) => (
                  <li key={band}>
                    <span className={`band-swatch band-${band}`} aria-hidden="true" />
                    <b>{classDef(band).chip}</b> — {classDef(band).line}
                  </li>
                ))}
              </ul>
              <p className="rail-hint">
                Wallets flagged <b>Market maker</b>, <b>Arbitrage</b> or <b>Very high
                frequency</b> are harder to mirror: their edge is the spread, holding both
                sides, or speed a copy cannot match.
              </p>
              <p className="rail-hint">
                Copy Score is {cohort.meta.scoreOwner || 'Polycopy'}&rsquo;s measurement over
                their own cohort, not PolyTrade&rsquo;s. PolyTrade publishes no composite
                score of its own — its inputs are partial by construction and one number
                would hide that.
              </p>
            </>
          )}
          <a className="rail-link" href="/docs/system-design">
            How the metrics are computed&nbsp;<span aria-hidden="true">↗</span>
          </a>
        </aside>
      </div>
    </div>
  )
}

/* Filter rail. Period and sort are pills; every numeric threshold has an
   explicit Apply control, so an untouched slider never narrows the result set
   and every numeric endpoint remains usable.

   On a narrow screen the rail stacks above the results, and left expanded it
   pushed the first wallet roughly 900px down the page. The numeric sliders
   therefore start collapsed below the breakpoint where the rail stops being a
   column — period and sort, the two controls people actually reach for first,
   stay visible either way. Evaluated once on mount: a viewport that changes
   mid-session is not worth reopening a disclosure the reader may have closed. */
const RAIL_IS_STACKED = '(max-width: 940px)'

function FilterRail({
  period, onPeriod, sort, onSort, filters, setFilter,
  completeHistoryOnly, onCompleteHistoryOnly, chips, onClearChip, onClearAll,
  cohortMode, hasCohort, bands, onToggleBand, category, onCategory, windowLabel,
}) {
  const label = windowLabel ?? period.toUpperCase()
  const [numericOpen] = useState(
    () => !(typeof window !== 'undefined' && window.matchMedia?.(RAIL_IS_STACKED).matches),
  )
  return (
    <>
      <div className="rail-heading">Filters</div>

      <div className="control" role="group" aria-label="Metric period">
        <span className="control-label">PERIOD</span>
        <div className="segmented">
          {PERIODS.map((value) => (
            <button
              key={value} type="button"
              className={value === period ? 'active' : ''}
              aria-pressed={value === period}
              onClick={() => onPeriod(value)}
            >{value.toUpperCase()}</button>
          ))}
        </div>
      </div>

      <div className="control" role="group" aria-label="Order by">
        <span className="control-label">ORDER BY</span>
        <div className="segmented">
          {SORTS.map(([key, text]) => (
            <button
              key={key} type="button"
              className={key === sort ? 'active' : ''}
              aria-pressed={key === sort}
              onClick={() => onSort(key)}
            >{text}</button>
          ))}
          {/* These two rank the Copy Score cohort rather than the live cache,
              so they are unavailable until that overlay has loaded — and the
              title says why rather than leaving a dead control. */}
          {COHORT_SORTS.map(([key, text]) => (
            <button
              key={key} type="button"
              className={key === sort ? 'active' : ''}
              aria-pressed={key === sort}
              disabled={!hasCohort}
              title={hasCohort
                ? 'Ranks the Copy Score cohort — a dated snapshot, not the live cache'
                : 'Loading the Copy Score cohort…'}
              onClick={() => onSort(key)}
            >{text}</button>
          ))}
        </div>
      </div>

      {cohortMode && (
        <>
          <div className="control" role="group" aria-label="Category">
            <span className="control-label">CATEGORY</span>
            <div className="segmented segmented-wrap">
              {CATEGORIES.map((value) => (
                <button
                  key={value} type="button"
                  className={value === category ? 'active' : ''}
                  aria-pressed={value === category}
                  onClick={() => onCategory(value)}
                >{value === 'all' ? 'All' : value}</button>
              ))}
            </div>
          </div>

          {sort === 'copy' && (
            <div className="control" role="group" aria-label="Copy Score bands">
              <span className="control-label">BANDS</span>
              <div className="band-picker">
                {CLASS_ORDER.filter((band) => band !== 'none').map((band) => (
                  <label key={band} className="band-check" title={classDef(band).line}>
                    <input
                      type="checkbox"
                      checked={bands.has(band)}
                      onChange={() => onToggleBand(band)}
                    />
                    <span className={`band-swatch band-${band}`} aria-hidden="true" />
                    {classDef(band).chip}
                  </label>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      <details className="screener-numeric" open={numericOpen}>
        <summary>Thresholds</summary>
        <RangeFilter
        id="f-pnl" label={`PnL ${label} ≥`} value={filters.pnlMin}
        onChange={(v) => setFilter('pnlMin', v)}
        min={-25000} max={25000} step={250}
        format={(n) => `$${n.toLocaleString('en-US')}`}
      />
      <RangeFilter
        id="f-winrate" label={`Win rate ${label} ≥`} value={filters.winrateMin}
        onChange={(v) => setFilter('winrateMin', v)}
        min={0} max={100} step={5} format={(n) => `${n}%`}
      />
      <RangeFilter
        id="f-volume" label={`Volume ${label} ≥`} value={filters.volumeMin}
        onChange={(v) => setFilter('volumeMin', v)}
        min={0} max={250000} step={2500}
        format={(n) => `$${n.toLocaleString('en-US')}`}
      />
      {cohortMode && (
        <>
          <RangeFilter
            id="f-roi" label={`ROI ${label} ≥`} value={filters.roiMin}
            onChange={(v) => setFilter('roiMin', v)}
            min={-50} max={100} step={5} format={(n) => `${n}%`}
          />
          <RangeFilter
            id="f-copynet" label="Copy Score ≥" value={filters.copyNetMin}
            onChange={(v) => setFilter('copyNetMin', v)}
            min={-50} max={50} step={1} format={(n) => (n > 0 ? `+${n}` : String(n))}
          />
          <RangeFilter
            id="f-activedays" label="Active days ≥" value={filters.activeDaysMin}
            onChange={(v) => setFilter('activeDaysMin', v)}
            min={0} max={365} step={5} format={(n) => `${n}D`}
          />
          {/* A ceiling, not a floor: a wallet whose average fill is $220K is
              one you cannot mirror at a retail budget however good its score. */}
          <RangeFilter
            id="f-avgsize" label="Average fill ≤" value={filters.avgSizeMax}
            onChange={(v) => setFilter('avgSizeMax', v)}
            min={10} max={5000} step={10} off="max"
            format={(n) => `$${n.toLocaleString('en-US')}`}
          />
        </>
      )}
      </details>

      <details className="screener-advanced">
        <summary>Advanced</summary>
        <RangeFilter
          id="f-consistency" label={`Positive close-day ratio ${label} ≥`}
          value={filters.consistencyRatioMin}
          onChange={(v) => setFilter('consistencyRatioMin', v)}
          min={0} max={100} step={5} format={(n) => `${n}%`}
        />
        <p className="rail-hint">
          Positive / (positive + negative) realized close days. Flat days and days with no
          closings are excluded.
        </p>
        <RangeFilter
          id="f-exit-min" label={`Sell / buy event count ${label} ≥`}
          value={filters.fillExitMin}
          onChange={(v) => setFilter('fillExitMin', v)}
          min={0} max={400} step={5} format={(n) => `${n}%`}
        />
        <RangeFilter
          id="f-exit-max" label={`Sell / buy event count ${label} ≤`}
          value={filters.fillExitMax}
          onChange={(v) => setFilter('fillExitMax', v)}
          min={0} max={400} step={5} off="max" format={(n) => `${n}%`}
        />
        <p className="rail-hint">
          SELL activity rows / BUY activity rows × 100. An activity-frequency ratio — not
          capital, shares, or a position close rate.
        </p>
      </details>

      <label className="filter-check">
        <input
          type="checkbox" checked={completeHistoryOnly}
          onChange={(event) => onCompleteHistoryOnly(event.target.checked)}
        />
        <span>
          <strong>Complete fetched history only</strong>
          <small>
            Hides wallets whose fetched trade history does not reach back across the whole
            period. This describes that source only.
          </small>
        </span>
      </label>

      {cohortMode && (
        <label className="filter-check">
          <input
            type="checkbox" checked={Boolean(filters.excludeHardToMirror)}
            onChange={(event) => setFilter('excludeHardToMirror', event.target.checked)}
          />
          <span>
            <strong>Drop wallets you cannot mirror</strong>
            <small>
              Hides market makers, arbitrage and very-high-frequency wallets. Their edge is
              the spread, both sides at once, or raw speed — none of which a copy can follow,
              whatever the score says.
            </small>
          </span>
        </label>
      )}

      {chips.length > 0 && (
        <div className="chip-row" aria-label="Active filters">
          {/* Structural chips — the volume floor an ordering imposes, the band
              filter, the freshness cut — are shown too, dashed and inert, so
              nothing narrows the board invisibly. */}
          {chips.map(([key, text, clearable]) => (clearable ? (
            <button key={key} type="button" className="chip" onClick={() => onClearChip(key)}>
              {text} ×
            </button>
          ) : (
            <span key={key} className="chip chip-fixed" title="Imposed by this ordering — not a filter you set">
              {text}
            </span>
          )))}
          <button type="button" className="chip chip-clear" onClick={onClearAll}>
            Clear all
          </button>
        </div>
      )}
    </>
  )
}

function downloadCsv(rows, period, { cohortMode = false, cohortPeriod = 'd30', cohort = null } = {}) {
  // The cohort export carries the score columns and is stamped with the
  // snapshot's own date, not today's — the file describes that snapshot.
  const body = cohortMode
    ? cohortToCsv(rows.map((row) => toCohortShape(row)), cohortPeriod)
    : walletsToCsv(rows, period)
  const stamp = cohortMode
    ? (cohort?.meta?.windowAnchor || cohort?.meta?.generatedAt || '').slice(0, 10)
    : new Date().toISOString().slice(0, 10)
  const blob = new Blob([body], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `polytrade-screener-${cohortMode ? 'copyscore-' : ''}${period}-${stamp}.csv`
  document.body.append(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function SortableHeader({ label, column, sort, direction, onSort, help }) {
  // Money columns map to the API's sort keys; Copy Score and ROI are the two
  // this surface orders itself.
  const key = COLUMN_SORT[column] ?? column
  const active = sort === key
  const cohortOrdered = isCohortSort(key)
  const ascending = active && direction === 'asc'
  return (
    <th
      scope="col" className="num" title={help}
      aria-sort={active ? (ascending ? 'ascending' : 'descending') : 'none'}
    >
      <button type="button" className={`th-sort${active ? ' is-active' : ''}`} onClick={() => onSort(key)}>
        <span>{label}</span>
        {/* Only a board this surface orders itself can honour a direction
            toggle. The API ranks descending only, so a live column marks the
            active sort rather than promising a flip it cannot deliver. */}
        <span className="th-arrow" aria-hidden="true">
          {active ? (cohortOrdered && ascending ? '▴' : '▾') : '↕'}
        </span>
      </button>
    </th>
  )
}

function ResultTable({
  rows, period, windowLabel, selected, onSelect, sort, direction, onSort,
  showScore, cohortMode,
}) {
  const label = windowLabel ?? period.toUpperCase()
  // One band header per run of equal bands, and only under Copy Score
  // ordering — under any other ordering the bands are interleaved, and a
  // header there would be a claim about rows that do not follow it.
  const runs = useMemo(() => new Map(
    bandRuns(rows, cohortMode && sort === 'copy' ? 'copy' : null).map((run) => [run.index, run]),
  ), [rows, sort, cohortMode])
  const columnCount = showScore ? 11 : 8
  return (
    <div className="table-scroll">
      <table className="screener-table">
        <caption className="visually-hidden">
          Polymarket wallets ranked over the selected {label} window. Metrics without a value
          are unavailable, not zero.
          {showScore && ' Copy Score is Polycopy’s figure over their own cohort, not PolyTrade’s.'}
        </caption>
        <thead>
          <tr>
            <th scope="col"><span className="visually-hidden">Saved</span></th>
            <th scope="col">Wallet</th>
            {showScore && (
              <SortableHeader
                label="Copy Score" column="copy" sort={sort} direction={direction} onSort={onSort}
                help="What is left of a wallet's edge after the spread and fees a copier pays. Polycopy's figure, over their cohort."
              />
            )}
            {showScore && (
              <SortableHeader
                label={`ROI ${label}`} column="roi" sort={sort} direction={direction} onSort={onSort}
                help="PnL as a share of volume over this window. Unavailable when volume is zero — a wallet that traded nothing is not a 0% wallet."
              />
            )}
            <SortableHeader label={`PnL ${label}`} column="pnl" sort={sort} direction={direction} onSort={onSort} />
            <SortableHeader label={`Win rate ${label}`} column="winRate" sort={sort} direction={direction} onSort={onSort} />
            <SortableHeader label={`Volume ${label}`} column="volume" sort={sort} direction={direction} onSort={onSort} />
            {showScore && <th scope="col">Trend</th>}
            <th scope="col" className="num">Active positions</th>
            <th scope="col">Fetched history</th>
            <th scope="col"><span className="visually-hidden">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const run = runs.get(index)
            // ROI is undefined without volume; a zero-volume wallet is not a
            // 0% wallet, so it stays unavailable.
            const roi = row.volume > 0 && row.pnl != null ? (row.pnl / row.volume) * 100 : null
            return (
              <Fragment key={row.address}>
                {run && <BandRow band={run.band} total={run.total} colSpan={columnCount} />}
                <tr className={row.address === selected ? 'selected' : ''}>
                  <td className="save-col">
                    <button
                      type="button"
                      className="save-btn"
                      aria-pressed={isSaved(row.address)}
                      aria-label={`${isSaved(row.address) ? 'Remove' : 'Save'} ${row.address}`}
                      title={isSaved(row.address) ? 'Saved — click to remove' : 'Save this wallet'}
                      onClick={() => toggleSaved(row.address, {
                        name: row.displayName, pnl: row.pnl, period,
                      })}
                    >{isSaved(row.address) ? '♥' : '♡'}</button>
                  </td>
                  <th scope="row">
                    <span className="wallet-name">{row.displayName || 'Unnamed wallet'}</span>
                    {/* The cohort labels unnamed wallets with their own short
                        address, so printing it again underneath is a second
                        copy of the same string, not a second fact. */}
                    {row.displayName !== short(row.address) && (
                      <span className="wallet-address" title={row.address}>{short(row.address)}</span>
                    )}
                    <MirrorChip row={row} />
                  </th>
                  {showScore && (
                    <td className="score-col">
                      <CopyChip copyClass={row.copyClass} copyNet={row.copyNet} />
                      <ScoreMove delta={row.scoreMove} />
                    </td>
                  )}
                  {showScore && (
                    <td className={`num ${roi == null ? '' : roi > 0 ? 'pos' : roi < 0 ? 'neg' : ''}`}>
                      {signedPercent(roi)}
                    </td>
                  )}
                  <td className={`num ${row.pnl == null ? '' : row.pnl >= 0 ? 'pos' : 'neg'}`}>
                    {formatMetric(row.pnl, 'money')}
                  </td>
                  <td className="num">{formatMetric(row.winRate, 'percent')}</td>
                  <td className="num">{formatMetric(row.volume, 'money')}</td>
                  {showScore && (
                    <td className="trend-col"><Sparkline values={row.spark} /></td>
                  )}
                  <td className="num">{formatMetric(row.activePositions, 'count')}</td>
                  <td
                    className="coverage"
                    title={row.lastTradeDay ? `Last trade ${row.lastTradeDay}` : undefined}
                  >{row.coverage}</td>
                  <td>
                    <button
                      type="button" className="btn btn-analyze"
                      aria-pressed={row.address === selected}
                      onClick={() => onSelect(row.address === selected ? null : row.address)}
                    >ANALYZE</button>
                  </td>
                </tr>
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/* Whose number the Copy Score is, and how old it is.
 *
 * Stated once above the table rather than repeated on fifty rows. Two things
 * have to reach the reader: the score is Polycopy's and not PolyTrade's, and
 * the cohort behind it is a dated snapshot — upstream regenerates daily, so
 * past two days these rankings describe a board that has already moved. */
function CohortNotice({ meta, age, cohortMode, scored, total }) {
  const generated = (meta.generatedAt || '').slice(0, 10)
  return (
    <div className={`cohort-notice${age.stale ? ' is-stale' : ''}`} role="note">
      <span className="cohort-notice-label">COPY SCORE</span>
      <p>
        {cohortMode ? (
          <>
            This board ranks the <strong>Copy Score cohort</strong> — {' '}
            a snapshot of {meta.scoreOwner || 'Polycopy'}&rsquo;s ranked wallets taken{' '}
            {generated} ({age.label}), not PolyTrade&rsquo;s live cache. Money columns are
            that snapshot&rsquo;s too.
          </>
        ) : (
          <>
            Rows are PolyTrade&rsquo;s live cache. Copy Score beside them is{' '}
            {meta.scoreOwner || 'Polycopy'}&rsquo;s figure from a snapshot taken {generated}{' '}
            ({age.label}), and it covers {scored.toLocaleString()} of the{' '}
            {total.toLocaleString()} wallets on this page — the rest are not scored, which is
            the absence of a score rather than a low one.
          </>
        )}
        {age.stale && ' That snapshot is regenerated daily upstream, so it is now describing a board that has already moved.'}
      </p>
    </div>
  )
}

/* The cohort board is already in the browser, so it grows in place. The count
   and the held-back note both say what is NOT on screen — a board that hides
   rows without saying so is the failure mode this surface exists to avoid. */
function CohortFooter({ rows, visible, onMore, heldBack }) {
  const remaining = rows.length - visible
  return (
    <nav className="screener-pagination" aria-label="Cohort board size">
      <span aria-live="polite">
        {rows.length
          ? `Showing ${Math.min(visible, rows.length).toLocaleString()} of ${rows.length.toLocaleString()} wallets`
          : 'No wallets'}
        {heldBack > 0 && (
          <>
            {' · '}
            <span title={`Lifetime totals never decay, so wallets that have not traded in ${STALE_DAYS} days are held back. They still rank on Polymarket's all-time board; this one answers who you could copy today.`}>
              {heldBack.toLocaleString()} held back as stale
            </span>
          </>
        )}
      </span>
      <div>
        {remaining > 0 && (
          <button type="button" className="btn btn-ghost" onClick={onMore}>
            Show {Math.min(20, remaining)} more
          </button>
        )}
      </div>
    </nav>
  )
}

/** Why a cohort board came back empty, in the terms the reader set it with. */
function emptyCohortMessage({ sort, bands, category }) {
  if (sort === 'copy') {
    const shown = [...(bands ?? RECOMMENDED)].map((band) => classDef(band).chip).join(' or ')
    return `No ${shown} wallet in ${category === 'all' ? 'this cohort' : category} traded `
      + 'enough in this window. Try a longer period, or widen the bands.'
  }
  return `No wallet in ${category === 'all' ? 'this cohort' : category} clears the volume `
    + 'floor this measure needs.'
}

function ResultPagination({ payload, offset, pageSize, onPage }) {
  return (
    <nav className="screener-pagination" aria-label="Wallet result pages">
      <span aria-live="polite">{paginationLabel(payload)}</span>
      <div>
        <button
          type="button" className="btn btn-ghost"
          disabled={offset <= 0}
          onClick={() => onPage(offset - pageSize)}
        >Previous</button>
        <button
          type="button" className="btn btn-ghost"
          disabled={!payload?.has_more}
          onClick={() => onPage(offset + pageSize)}
        >Next</button>
      </div>
    </nav>
  )
}

function WalletAnalysis({ row, period, onClose }) {
  const [curve, setCurve] = useState('cumulative')
  if (!row) return null
  const label = period.toUpperCase()
  const stamp = refreshed(row.refreshedAt)
  const shape = (CURVES.find(([k]) => k === curve) || CURVES[0])[2]
  const series = curveFrom(row.dailyPnl, curve)
  const outcomes = dayOutcomes(row.dailyPnl)
  const money = (v) => formatMetric(v, 'money')
  return (
    <section className="wallet-analysis" aria-label={`Analysis for ${row.address}`}>
      <div className="analysis-head">
        <div>
          <p className="screener-eyebrow">SELECTED WALLET · {label}</p>
          <h2>{row.displayName || 'Unnamed wallet'}</h2>
          <p className="analysis-address">{row.address}</p>
        </div>
        <button type="button" className="btn btn-ghost" onClick={onClose}>Close</button>
      </div>

      <div className="analysis-curve">
        <div className="analysis-curve-head">
          <span className="control-label">REALISED PNL · {label}</span>
          <div className="sort-row" style={{ margin: 0 }}>
            {CURVES.map(([key, text]) => (
              <button
                key={key}
                type="button"
                className={`chip ${curve === key ? 'active' : ''}`}
                aria-pressed={curve === key}
                onClick={() => setCurve(key)}
              >{text}</button>
            ))}
          </div>
        </div>
        {row.dailyPnl === null ? (
          <p className="chart-empty">
            This wallet&apos;s daily series has not been computed yet, so there is no curve to
            draw. That is missing data, not a flat month.
          </p>
        ) : (
          <SeriesChart
            points={series}
            shape={shape}
            format={money}
            label={`${curve} realised PnL`}
          />
        )}
        {outcomes && (
          <dl className="analysis-metrics analysis-outcomes">
            <div>
              <dt>Closing days · {label}</dt>
              <dd>{outcomes.positive} up · {outcomes.negative} down · {outcomes.flat} flat</dd>
            </div>
            <div><dt>Average moving day</dt><dd>{money(outcomes.avgMovingDay)}</dd></div>
            <div><dt>Best day</dt><dd>{money(outcomes.best)}</dd></div>
            <div><dt>Worst day</dt><dd>{money(outcomes.worst)}</dd></div>
          </dl>
        )}
      </div>

      <dl className="analysis-metrics">
        <div><dt>Realised PnL · {label}</dt><dd>{formatMetric(row.pnl, 'money')}</dd></div>
        <div><dt>Win rate · {label}</dt><dd>{formatMetric(row.winRate, 'percent')}</dd></div>
        <div><dt>Gross volume · {label}</dt><dd>{formatMetric(row.volume, 'money')}</dd></div>
        <div><dt>Active positions</dt><dd>{formatMetric(row.activePositions, 'count')}</dd></div>
        <div>
          <dt>Positive close-day ratio · {label}</dt>
          <dd>{formatMetric(row.consistencyRatio, 'percent')}</dd>
        </div>
        <div>
          <dt>Sell / buy event count · {label}</dt>
          <dd>{formatMetric(row.fillExitRatio, 'percentValue')}</dd>
        </div>
      </dl>

      <ul className="analysis-notes">
        <li>Fetched trade history: {row.coverage}.</li>
        {row.historyPartial && (
          <li>
            Part of this period is not covered by the fetched history, so these figures
            describe less than the full {label} window.
          </li>
        )}
        <li>
          {stamp
            ? `Statistics last recomputed ${stamp}.`
            : 'These statistics have not been recomputed yet, so period metrics are unavailable rather than zero.'}
        </li>
        <li>Win rate counts reconstructed closing events, not open positions.</li>
        <li>
          The curve is realised PnL per UTC day within this window. Days with no closings
          contribute zero, and drawdown is the distance below the running peak.
        </li>
        <li>
          The sell / buy event count is fetched SELL activity rows per 100 BUY rows. It
          is an activity-frequency ratio — not an order, position, share, or capital
          close rate.
        </li>
        <li>
          The positive close-day ratio excludes days that netted exactly zero realized
          PnL, and days with no closings at all.
        </li>
      </ul>

      <div className="analysis-actions">
        <a
          className="btn" href={botDeepLink()}
          target="_blank" rel="noreferrer noopener"
        >Open the Telegram Mini App ↗</a>
        <a className="analysis-link" href={POLYMARKET_PROFILE(row.address)}
           target="_blank" rel="noreferrer noopener">
          View on Polymarket ↗
        </a>
      </div>
      <p className="analysis-disclaimer">
        This page is read-only and cannot create an account, wallet or position. The Telegram Mini
        App currently shows only wallets already copied by the account. Adding a new copied wallet is not yet available from this public screener.
      </p>
    </section>
  )
}

function Provenance({ provenance }) {
  return (
    <section className="screener-provenance" aria-label="Data provenance">
      <p className="screener-eyebrow">WHERE THESE NUMBERS COME FROM</p>
      <p>
        {provenance?.source
          || 'Public Polymarket leaderboard and activity data, recomputed by PolyTrade into 7-day, 30-day and 90-day windows.'}
      </p>
      <ul>
        {(provenance?.limitations || [
          'Metrics are reconstructed from fetched trade history; where that history does not reach back across the whole period the wallet is marked partial.',
          'A missing metric is shown as unavailable, never as zero.',
          'Figures are a cache, refreshed periodically.',
          'Past wallet activity does not predict future results.',
        ]).map((line) => <li key={line}>{line}</li>)}
      </ul>
    </section>
  )
}

/* Saved wallets, kept in this browser.
 *
 * The live row wins where the wallet is on the current page; otherwise the
 * snapshot taken at save time shows, labelled, because the cache behind the
 * board rotates and a saved wallet may not be on screen at all. */
function SavedPanel({ rows, onClose, onPick }) {
  const saved = savedList()
  const live = new Map(rows.map((r) => [r.address, r]))
  return (
    <section className="saved-panel" aria-label="Saved wallets">
      <div className="analysis-head">
        <div>
          <p className="screener-eyebrow">SAVED WALLETS</p>
          <h2>{saved.length} kept on this browser</h2>
        </div>
        <button type="button" className="btn btn-ghost" onClick={onClose}>Close</button>
      </div>

      {saved.length === 0 ? (
        <p className="screener-state">
          Nothing saved yet. Use the heart on any row to keep it here.
        </p>
      ) : (
        <ul className="saved-list">
          {saved.map((rec) => {
            const row = live.get(rec.w)
            return (
              <li key={rec.w}>
                <button
                  type="button" className="save-btn" aria-pressed="true"
                  aria-label={`Remove ${rec.w}`}
                  onClick={() => toggleSaved(rec.w)}
                >{'\u2665'}</button>
                <span className="saved-who">
                  <span className="wallet-name">{row?.displayName || rec.name || 'Unnamed wallet'}</span>
                  <span className="wallet-address">{short(rec.w)}</span>
                </span>
                <span className={`num ${(row?.pnl ?? rec.pnl) >= 0 ? 'pos' : 'neg'}`}>
                  {formatMetric(row?.pnl ?? rec.pnl, 'money')}
                  {!row && (rec.pnl != null) && <em className="as-saved"> as saved</em>}
                </span>
                {row && (
                  <button type="button" className="btn btn-analyze" onClick={() => onPick(rec.w)}>
                    ANALYZE
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {saved.length > 0 && (
        <div className="saved-foot">
          <button type="button" className="btn btn-ghost" onClick={() => {
            if (confirm(`Remove all ${saved.length} saved wallets? This cannot be undone.`)) clearSaved()
          }}>Clear all</button>
          <p className="analysis-disclaimer">
            Stored in this browser only. It is not an account, nothing is sent anywhere, and
            clearing site data clears it.
          </p>
        </div>
      )}
    </section>
  )
}
