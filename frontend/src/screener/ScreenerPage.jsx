import { useCallback, useEffect, useMemo, useState } from 'react'
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

const short = (address) => `${address.slice(0, 6)}…${address.slice(-4)}`

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

  useEffect(() => subscribeSaved(() => setSavedTick((n) => n + 1)), [])

  // replaceState, not push: filtering is not navigation, and a history entry
  // per slider drag would make Back useless.
  useEffect(() => {
    const q = encodeScreenerState({ period, sort, search, filters, completeHistoryOnly })
    window.history.replaceState(null, '', q ? `?${q}` : window.location.pathname)
  }, [period, sort, search, filters, completeHistoryOnly])

  const queryState = useMemo(() => {
    try {
      return {
        query: buildPublicQuery({
          period, sort, search, filters, completeHistoryOnly, offset,
        }),
        validationError: '',
      }
    } catch (problem) {
      return { query: null, validationError: String(problem.message || problem) }
    }
  }, [completeHistoryOnly, filters, offset, period, search, sort])
  const query = queryState.query

  useEffect(() => {
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
  }, [query, queryState.validationError])

  const rows = useMemo(() => walletRows(payload), [payload])
  const chips = useMemo(
    () => activeFilterChips({ filters, period, completeHistoryOnly }),
    [completeHistoryOnly, filters, period],
  )
  const resetPage = useCallback(() => {
    setOffset(0)
    setSelected(null)
  }, [])
  const changePeriod = (value) => { resetPage(); setPeriod(value) }
  const changeSort = (value) => { resetPage(); setSort(value) }
  const changeSearch = (value) => { resetPage(); setSearch(value) }
  const changeCompleteHistory = (value) => { resetPage(); setCompleteHistoryOnly(value) }
  const setFilter = (key, value) => {
    resetPage()
    setFilters((current) => ({ ...current, [key]: value }))
  }
  const clearChip = (key) => {
    if (key === 'completeHistoryOnly') changeCompleteHistory(false)
    else setFilter(key, '')
  }
  const clearAll = useCallback(() => {
    setOffset(0)
    setSelected(null)
    setFilters({ ...DEFAULT_FILTERS })
    setCompleteHistoryOnly(false)
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
          />
        </aside>

        <main className="screener-main">
          <div className="results-toolbar">
            <span className="coverage">
              {payload?.total != null ? `${payload.total.toLocaleString()} wallets` : ''}
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
                onClick={() => downloadCsv(rows, period)}
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
                    {offset > 0 && payload?.total > 0
                      ? 'No wallets on this page. Use Previous to return to available results.'
                      : exactLookup
                        ? 'That wallet is not in the public screener cache yet.'
                        : 'No cached wallet matches these filters.'}
                  </p>
                ) : (
                  <ResultTable
                    rows={rows} period={period}
                    selected={selected} onSelect={setSelected}
                    sort={sort} onSort={(next) => { setSort(next); setOffset(0) }}
                  />
                )}
                <ResultPagination
                  payload={payload} offset={offset} pageSize={pageSize} onPage={goToPage}
                />
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
            <li>Metrics cover the selected {period.toUpperCase()} window only.</li>
            <li>“—” means unavailable, not zero.</li>
            <li>“Partial” describes fetched trade history, nothing else.</li>
          </ul>
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
}) {
  const label = period.toUpperCase()
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

      <div className="control" role="group" aria-label="Sort by">
        <span className="control-label">SORT BY</span>
        <div className="segmented">
          {SORTS.map(([key, text]) => (
            <button
              key={key} type="button"
              className={key === sort ? 'active' : ''}
              aria-pressed={key === sort}
              onClick={() => onSort(key)}
            >{text}</button>
          ))}
        </div>
      </div>

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

      {chips.length > 0 && (
        <div className="chip-row" aria-label="Active filters">
          {chips.map(([key, text]) => (
            <button key={key} type="button" className="chip" onClick={() => onClearChip(key)}>
              {text} ×
            </button>
          ))}
          <button type="button" className="chip chip-clear" onClick={onClearAll}>
            Clear all
          </button>
        </div>
      )}
    </>
  )
}

function downloadCsv(rows, period) {
  const blob = new Blob([walletsToCsv(rows, period)], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `polytrade-screener-${period}-${new Date().toISOString().slice(0, 10)}.csv`
  document.body.append(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function SortableHeader({ label, column, sort, onSort }) {
  const key = COLUMN_SORT[column]
  const active = sort === key
  return (
    <th scope="col" className="num" aria-sort={active ? 'descending' : 'none'}>
      <button type="button" className={`th-sort${active ? ' is-active' : ''}`} onClick={() => onSort(key)}>
        <span>{label}</span>
        {/* The API only ranks descending, so this marks the active column
            rather than promising a direction toggle it cannot honour. */}
        <span className="th-arrow" aria-hidden="true">{active ? '\u25be' : '\u2195'}</span>
      </button>
    </th>
  )
}

function ResultTable({ rows, period, selected, onSelect, sort, onSort }) {
  const label = period.toUpperCase()
  return (
    <div className="table-scroll">
      <table className="screener-table">
        <caption className="visually-hidden">
          Polymarket wallets ranked over the selected {label} window. Metrics without a value
          are unavailable, not zero.
        </caption>
        <thead>
          <tr>
            <th scope="col"><span className="visually-hidden">Saved</span></th>
            <th scope="col">Wallet</th>
            <SortableHeader label={`PnL ${label}`} column="pnl" sort={sort} onSort={onSort} />
            <SortableHeader label={`Win rate ${label}`} column="winRate" sort={sort} onSort={onSort} />
            <SortableHeader label={`Volume ${label}`} column="volume" sort={sort} onSort={onSort} />
            <th scope="col" className="num">Active positions</th>
            <th scope="col">Fetched history</th>
            <th scope="col"><span className="visually-hidden">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.address} className={row.address === selected ? 'selected' : ''}>
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
                >{isSaved(row.address) ? '\u2665' : '\u2661'}</button>
              </td>
              <th scope="row">
                <span className="wallet-name">{row.displayName || 'Unnamed wallet'}</span>
                <span className="wallet-address" title={row.address}>{short(row.address)}</span>
              </th>
              <td className={`num ${row.pnl == null ? '' : row.pnl >= 0 ? 'pos' : 'neg'}`}>
                {formatMetric(row.pnl, 'money')}
              </td>
              <td className="num">{formatMetric(row.winRate, 'percent')}</td>
              <td className="num">{formatMetric(row.volume, 'money')}</td>
              <td className="num">{formatMetric(row.activePositions, 'count')}</td>
              <td className="coverage">{row.coverage}</td>
              <td>
                <button
                  type="button" className="btn btn-analyze"
                  aria-pressed={row.address === selected}
                  onClick={() => onSelect(row.address === selected ? null : row.address)}
                >ANALYZE</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
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
