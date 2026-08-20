import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { trackTelemetry } from '../telemetry'
import FilterSlider from './FilterSlider'
import Folder from './Folder'
import TraderCard from './TraderCard'
import {
  DEFAULT_FILTERS,
  DEFAULT_PERIOD,
  DEFAULT_SORT,
  PERIODS,
  SORTS,
  activeFilterChips,
  buildScreenerQuery,
} from './walletScreenerModel'

export default function WalletScreener({ onFollowed, balance }) {
  const [period, setPeriod] = useState(DEFAULT_PERIOD)
  const [sort, setSort] = useState(DEFAULT_SORT)
  const [filters, setFilters] = useState(() => ({ ...DEFAULT_FILTERS }))
  const [includePartialHistory, setIncludePartialHistory] = useState(true)
  const [search, setSearch] = useState('')
  const [traders, setTraders] = useState([])
  const [loading, setLoading] = useState(true)
  // A full 0x address that is not cached is still fetched live and shown as a card.
  const [checked, setChecked] = useState(null)
  const [checking, setChecking] = useState(false)
  const [checkErr, setCheckErr] = useState(null)
  const lastTrackedSearchRef = useRef('')
  const searchAddr = /^0x[0-9a-fA-F]{40}$/.test(search.trim()) ? search.trim().toLowerCase() : null

  const selectPeriod = (value) => {
    if (value === period) return
    setPeriod(value)
    trackTelemetry('period_changed', { period: value, source: 'screener' })
  }
  const setFilter = (key, value) => setFilters((current) => ({ ...current, [key]: value }))
  const clearChip = (key) => {
    if (key === 'includePartialHistory') setIncludePartialHistory(true)
    else setFilter(key, '')
  }
  const clearAll = () => {
    setFilters({ ...DEFAULT_FILTERS })
    setIncludePartialHistory(true)
  }

  const params = useMemo(
    () => buildScreenerQuery({ period, sort, search, includePartialHistory, filters }),
    [filters, includePartialHistory, period, search, sort],
  )

  useEffect(() => {
    let alive = true
    setLoading(true)
    const timer = setTimeout(() => {
      api
        .leaderboard(params)
        .then((result) => alive && setTraders(result))
        .catch(() => alive && setTraders([]))
        .finally(() => alive && setLoading(false))
    }, 300)
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [params])

  useEffect(() => {
    const term = search.trim()
    if (!term) {
      lastTrackedSearchRef.current = ''
      return undefined
    }
    const timer = setTimeout(() => {
      if (lastTrackedSearchRef.current === term) return
      lastTrackedSearchRef.current = term
      trackTelemetry('screener_search_submitted', {
        query_kind: /^0x[0-9a-fA-F]{40}$/.test(term) ? 'address' : 'text',
        period,
        active_filters: activeFilterChips({ period, includePartialHistory, filters }).length > 0,
      })
    }, 600)
    return () => clearTimeout(timer)
  }, [filters, includePartialHistory, period, search])

  // Live wallet check fires once the debounced cache search settles empty.
  useEffect(() => {
    // Clear synchronously before every early-return path, including when a full
    // address is replaced with ordinary search text while a request is active.
    setChecking(false)
    setChecked(null)
    setCheckErr(null)
    if (!searchAddr || loading) return
    if (traders.some((trader) => trader.address?.toLowerCase() === searchAddr)) return
    let alive = true
    setChecking(true)
    api
      .trader(searchAddr)
      .then((trader) => alive && setChecked({ requestedAddress: searchAddr, trader }))
      .catch((error) => alive && setCheckErr({
        requestedAddress: searchAddr,
        message: String(error.message || error),
      }))
      .finally(() => alive && setChecking(false))
    return () => {
      alive = false
    }
  }, [searchAddr, loading, traders])

  const currentChecked = checked?.requestedAddress === searchAddr ? checked.trader : null
  const currentCheckErr = checkErr?.requestedAddress === searchAddr ? checkErr.message : ''

  const chips = useMemo(
    () => activeFilterChips({ period, includePartialHistory, filters }),
    [filters, includePartialHistory, period],
  )

  return (
    <div>
      <input
        className="search-box"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder="> search name / x handle, or paste any 0x address to check it…"
      />

      <Folder title="FILTERS" open>
        <div className="screener-control-group" aria-label="Metric period">
          <span className="screener-control-label">PERIOD</span>
          <div className="sort-row">
            {PERIODS.map((value) => (
              <button
                key={value}
                className={`chip ${period === value ? 'active' : ''}`}
                onClick={() => selectPeriod(value)}
                aria-pressed={period === value}
              >
                {value.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        <div className="screener-control-group" aria-label="Sort wallets">
          <span className="screener-control-label">SORT BY</span>
          <div className="sort-row">
            {SORTS.map(([key, label]) => (
              <button
                key={key}
                className={`chip ${sort === key ? 'active' : ''}`}
                onClick={() => setSort(key)}
                aria-pressed={sort === key}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        <div className="filter-grid basic-filter-grid">
          <FilterSlider
            label={`PNL ${period.toUpperCase()} ≥ $`}
            value={filters.pnlMin} onChange={(value) => setFilter('pnlMin', value)}
            min={-25000} max={25000} step={250} placeholder="off"
          />
          <FilterSlider
            label={`WIN RATE ${period.toUpperCase()} ≥ %`}
            value={filters.winrateMin} onChange={(value) => setFilter('winrateMin', value)}
            min={0} max={100} step={5} placeholder="off"
          />
          <FilterSlider
            label={`VOLUME ${period.toUpperCase()} ≥ $`}
            value={filters.volumeMin} onChange={(value) => setFilter('volumeMin', value)}
            min={0} max={250000} step={2500} placeholder="off"
          />
        </div>

        <label className="coverage-control">
          <input
            type="checkbox"
            checked={includePartialHistory}
            onChange={(event) => setIncludePartialHistory(event.target.checked)}
          />
          <span>
            <strong>INCLUDE PARTIAL TRADE HISTORY</strong>
            <small>Includes wallets with less fetched TRADE history than the selected period; this does not describe other source coverage.</small>
          </span>
        </label>

        <details
          className="advanced-filters"
          onToggle={(event) => {
            if (event.currentTarget.open) {
              trackTelemetry('advanced_filters_opened', { period })
            }
          }}
        >
          <summary>ADVANCED FILTERS</summary>
          <div className="filter-grid">
            <div>
              <FilterSlider
                label={`POSITIVE CLOSE-DAY RATIO ${period.toUpperCase()} ≥ %`}
                value={filters.consistencyRatioMin}
                onChange={(value) => setFilter('consistencyRatioMin', value)}
                min={0} max={100} step={5} placeholder="off"
              />
              <p className="filter-description">Positive / (positive + negative) realized close days; flat/no-close days omitted.</p>
            </div>
            <div>
              <FilterSlider
                label={`SELL / BUY EVENT COUNT ${period.toUpperCase()} ≥ %`}
                value={filters.fillExitMin}
                onChange={(value) => setFilter('fillExitMin', value)}
                min={0} max={400} step={5} placeholder="off"
              />
              <p className="filter-description">SELL activity row count / BUY activity row count × 100; not capital, shares, or position close rate.</p>
            </div>
            <div>
              <FilterSlider
                label={`SELL / BUY EVENT COUNT ${period.toUpperCase()} ≤ %`}
                value={filters.fillExitMax}
                onChange={(value) => setFilter('fillExitMax', value)}
                min={0} max={400} step={5} off="max" placeholder="off"
              />
              <p className="filter-description">SELL activity row count / BUY activity row count × 100; not capital, shares, or position close rate.</p>
            </div>
          </div>
        </details>

        {chips.length > 0 && (
          <div className="active-filter-row" aria-label="Active filters">
            {chips.map(([key, label]) => (
              <button key={key} className="chip active" onClick={() => clearChip(key)} title="Click to clear">
                {label} ×
              </button>
            ))}
            <button className="chip" onClick={clearAll}>CLEAR ALL</button>
          </div>
        )}
      </Folder>

      {loading ? (
        <>
          <div className="card skeleton" />
          <div className="card skeleton" />
          <div className="card skeleton" />
        </>
      ) : traders.length === 0 ? (
        searchAddr && (checking || currentChecked || currentCheckErr) ? (
          <section className="direct-wallet-lookup" aria-label="Direct wallet lookup">
            <div className="warn-box">DIRECT WALLET LOOKUP · ACTIVE SCREENER FILTERS DO NOT APPLY</div>
            {checking ? (
              <>
                <div className="muted">checking wallet stats live (computing selected-period metrics)…</div>
                <div className="card skeleton" />
              </>
            ) : currentChecked ? (
              <TraderCard t={currentChecked} period={period} onFollowed={onFollowed} balance={balance} />
            ) : (
              <div className="warn-box">wallet check failed: {currentCheckErr}</div>
            )}
          </section>
        ) : (
          <div className="muted">
            {search.trim()
              ? `no cached wallet matches "${search.trim()}" — paste a full 0x address to check any wallet live`
              : 'no wallets match these filters'}
          </div>
        )
      ) : (
        traders.map((trader) => (
          <TraderCard key={trader.address} t={trader} period={period} onFollowed={onFollowed} balance={balance} />
        ))
      )}
    </div>
  )
}
