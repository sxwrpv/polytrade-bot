import { useCallback, useEffect, useMemo, useState } from 'react'
import { publicApi } from './publicApi'
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
  walletRows,
} from './screenerModel'

const short = (address) => `${address.slice(0, 6)}…${address.slice(-4)}`

const refreshed = (value) => {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`
}

export default function ScreenerPage() {
  const [period, setPeriod] = useState(DEFAULT_PERIOD)
  const [sort, setSort] = useState(DEFAULT_SORT)
  const [search, setSearch] = useState('')
  const [filters, setFilters] = useState(() => ({ ...DEFAULT_FILTERS }))
  const [completeHistoryOnly, setCompleteHistoryOnly] = useState(false)
  const [payload, setPayload] = useState(null)
  const [state, setState] = useState('loading')
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(null)

  const query = useMemo(
    () => buildPublicQuery({ period, sort, search, filters, completeHistoryOnly }),
    [completeHistoryOnly, filters, period, search, sort],
  )

  useEffect(() => {
    let alive = true
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
  }, [query])

  const rows = useMemo(() => walletRows(payload), [payload])
  const chips = useMemo(
    () => activeFilterChips({ filters, period, completeHistoryOnly }),
    [completeHistoryOnly, filters, period],
  )
  const setFilter = (key, value) => setFilters((current) => ({ ...current, [key]: value }))
  const clearChip = (key) => {
    if (key === 'completeHistoryOnly') setCompleteHistoryOnly(false)
    else setFilter(key, '')
  }
  const clearAll = useCallback(() => {
    setFilters({ ...DEFAULT_FILTERS })
    setCompleteHistoryOnly(false)
  }, [])

  const exactLookup = isAddress(search) ? search.trim().toLowerCase() : null

  return (
    <div className="screener">
      <header className="screener-nav">
        <a className="screener-brand" href="/" aria-label="PolyTrade home">
          <img className="brand-logo" src="/brand/polytrade-mark.png" alt="" />
          <span>PolyTrade</span>
          <span className="screener-brand-divider" />
          <span className="screener-brand-label">Wallet Screener</span>
        </a>
        <nav className="screener-nav-links" aria-label="Site">
          <a href="/docs">Documentation</a>
          <a href="/docs/system-design">System design</a>
          <a className="btn screener-nav-cta" href={botDeepLink(null)}>Open Telegram ↗</a>
        </nav>
      </header>

      <main className="screener-main">
        <section className="screener-intro">
          <p className="screener-eyebrow">PUBLIC WALLET RESEARCH</p>
          <h1>Wallet Screener</h1>
          <p className="screener-lede">
            Search Polymarket wallets on windowed statistics rebuilt from public trading
            history. Every figure states the period it covers and how much history backs it.
            Nothing here is a recommendation.
          </p>
        </section>

        <section className="screener-controls" aria-label="Filters">
          <div className="control-row">
            <div className="control" role="group" aria-label="Metric period">
              <span className="control-label">PERIOD</span>
              <div className="segmented">
                {PERIODS.map((value) => (
                  <button
                    key={value} type="button"
                    className={value === period ? 'active' : ''}
                    aria-pressed={value === period}
                    onClick={() => setPeriod(value)}
                  >{value.toUpperCase()}</button>
                ))}
              </div>
            </div>

            <div className="control" role="group" aria-label="Sort by">
              <span className="control-label">SORT BY</span>
              <div className="segmented">
                {SORTS.map(([key, label]) => (
                  <button
                    key={key} type="button"
                    className={key === sort ? 'active' : ''}
                    aria-pressed={key === sort}
                    onClick={() => setSort(key)}
                  >{label}</button>
                ))}
              </div>
            </div>

            <div className="control control-search">
              <label className="control-label" htmlFor="screener-search">
                SEARCH NAME, X HANDLE OR 0x ADDRESS
              </label>
              <input
                id="screener-search" type="search" value={search}
                placeholder="0x… or a display name"
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
          </div>

          <details className="screener-advanced">
            <summary>Filters</summary>
            <div className="filter-row">
              <NumberFilter
                id="f-pnl" label={`PnL ${period.toUpperCase()} ≥ $`}
                value={filters.pnlMin} onChange={(v) => setFilter('pnlMin', v)}
              />
              <NumberFilter
                id="f-winrate" label={`Win rate ${period.toUpperCase()} ≥ %`}
                value={filters.winrateMin} onChange={(v) => setFilter('winrateMin', v)}
              />
              <NumberFilter
                id="f-volume" label={`Volume ${period.toUpperCase()} ≥ $`}
                value={filters.volumeMin} onChange={(v) => setFilter('volumeMin', v)}
              />
              <label className="filter-check">
                <input
                  type="checkbox" checked={completeHistoryOnly}
                  onChange={(event) => setCompleteHistoryOnly(event.target.checked)}
                />
                <span>
                  <strong>Complete fetched history only</strong>
                  <small>
                    Hides wallets whose fetched trade history does not reach back across the
                    whole period. This describes that source only.
                  </small>
                </span>
              </label>
            </div>
          </details>

          {chips.length > 0 && (
            <div className="chip-row" aria-label="Active filters">
              {chips.map(([key, label]) => (
                <button key={key} type="button" className="chip" onClick={() => clearChip(key)}>
                  {label} ×
                </button>
              ))}
              <button type="button" className="chip chip-clear" onClick={clearAll}>Clear all</button>
            </div>
          )}
        </section>

        <section className="screener-results" aria-label="Results" aria-busy={state === 'loading'}>
          {state === 'throttled' ? (
            <p className="screener-state" role="status">
              The public screener is rate limited and this client has hit the limit. Wait a
              minute and try again.
            </p>
          ) : state === 'error' ? (
            <p className="screener-state" role="alert">Could not load wallets: {error}</p>
          ) : state === 'loading' ? (
            <p className="screener-state" role="status">Loading wallets…</p>
          ) : rows.length === 0 ? (
            <p className="screener-state">
              {exactLookup
                ? 'That wallet is not in the public screener cache yet.'
                : 'No cached wallet matches these filters.'}
            </p>
          ) : (
            <ResultTable
              rows={rows} period={period}
              selected={selected} onSelect={setSelected}
            />
          )}
        </section>

        {selected && (
          <WalletAnalysis
            row={rows.find((row) => row.address === selected) || null}
            period={period}
            onClose={() => setSelected(null)}
          />
        )}

        <Provenance provenance={payload?.provenance} />
      </main>

      <footer className="screener-footer">
        <span>Public wallet data from Polymarket. Not financial advice.</span>
        <span>
          <a href="/docs/risk-and-security">Risk &amp; security</a>
          {' · '}
          <a href="/docs">Documentation</a>
        </span>
      </footer>
    </div>
  )
}

function NumberFilter({ id, label, value, onChange }) {
  return (
    <div className="filter-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id} type="number" inputMode="decimal" value={value}
        placeholder="off" onChange={(event) => onChange(event.target.value)}
      />
    </div>
  )
}

function ResultTable({ rows, period, selected, onSelect }) {
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
            <th scope="col">Wallet</th>
            <th scope="col" className="num">PnL {label}</th>
            <th scope="col" className="num">Win rate {label}</th>
            <th scope="col" className="num">Volume {label}</th>
            <th scope="col" className="num">Active positions</th>
            <th scope="col">Fetched history</th>
            <th scope="col"><span className="visually-hidden">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.address} className={row.address === selected ? 'selected' : ''}>
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

function WalletAnalysis({ row, period, onClose }) {
  if (!row) return null
  const label = period.toUpperCase()
  const stamp = refreshed(row.refreshedAt)
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

      <dl className="analysis-metrics">
        <div><dt>Realised PnL · {label}</dt><dd>{formatMetric(row.pnl, 'money')}</dd></div>
        <div><dt>Win rate · {label}</dt><dd>{formatMetric(row.winRate, 'percent')}</dd></div>
        <div><dt>Gross volume · {label}</dt><dd>{formatMetric(row.volume, 'money')}</dd></div>
        <div><dt>Active positions</dt><dd>{formatMetric(row.activePositions, 'count')}</dd></div>
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
      </ul>

      <div className="analysis-actions">
        <a
          className="btn" href={botDeepLink(row.address)}
          target="_blank" rel="noreferrer noopener"
        >Open the Telegram Mini App ↗</a>
        <CopyAddress address={row.address} />
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

/* The address has to travel by hand, so make that one click rather than a
   fiddly selection of a 42-character string. Falls back to a visible, fully
   selectable address if the clipboard API is unavailable or refused. */
function CopyAddress({ address }) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return undefined
    const timer = setTimeout(() => setCopied(false), 2000)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(address)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  return (
    <button type="button" className="btn btn-ghost" onClick={copy}>
      {copied ? 'ADDRESS COPIED' : 'COPY ADDRESS'}
    </button>
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
