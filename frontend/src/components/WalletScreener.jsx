import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import FilterSlider from './FilterSlider'
import Folder from './Folder'
import TraderCard from './TraderCard'

const PERIODS = ['7d', '30d', '90d']
const SORTS = [
  ['consistency', 'CONSISTENCY'],
  ['pnl', 'PNL'],
  ['winrate', 'WINRATE'],
  ['volume', 'VOLUME'],
  ['pnl_quality', 'PNL QUALITY'],
]

const EMPTY = {
  winrateMin: '', pnlMin: '', volumeMin: '', consistencyMin: '',
  fillExitMin: '', fillExitMax: '', pnlQualityMin: '',
}

// Wallet parser / screener — every filter below combines with the others via
// AND (see backend GET /api/traders/leaderboard), all scoped to the selected
// period (7d/30d/90d) except PNL QUALITY, which is a point-in-time snapshot
// (realized pnl minus current unrealized pnl — see TraderCard tooltip).
export default function WalletScreener({ onFollowed, balance }) {
  const [period, setPeriod] = useState('30d')
  const [sort, setSort] = useState('consistency')
  const [f, setF] = useState(EMPTY)
  const [search, setSearch] = useState('')
  const [traders, setTraders] = useState([])
  const [loading, setLoading] = useState(true)

  const clearField = (k) => setF((p) => ({ ...p, [k]: '' }))
  const clearAll = () => setF(EMPTY)

  const params = useMemo(() => {
    const p = { sort, limit: 50 }
    if (search.trim() !== '') p.search = search.trim()
    if (f.winrateMin !== '') p[`winrate_${period}_min`] = Number(f.winrateMin) / 100
    if (f.pnlMin !== '') p[`pnl_${period}_min`] = Number(f.pnlMin)
    if (f.volumeMin !== '') p[`volume_${period}_min`] = Number(f.volumeMin)
    if (f.consistencyMin !== '') p[`consistency_ratio_${period}_min`] = Number(f.consistencyMin) / 100
    if (f.fillExitMin !== '') p[`fill_exit_ratio_${period}_min`] = Number(f.fillExitMin)
    if (f.fillExitMax !== '') p[`fill_exit_ratio_${period}_max`] = Number(f.fillExitMax)
    if (f.pnlQualityMin !== '') p.pnl_quality_min = Number(f.pnlQualityMin)
    return p
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [f, period, sort, search])

  useEffect(() => {
    let alive = true
    setLoading(true)
    const t = setTimeout(() => {
      api
        .leaderboard(params)
        .then((r) => alive && setTraders(r))
        .catch(() => alive && setTraders([]))
        .finally(() => alive && setLoading(false))
    }, 300) // debounced auto-apply — no explicit "search" button
    return () => {
      alive = false
      clearTimeout(t)
    }
  }, [params])

  const chips = useMemo(() => {
    const P = period.toUpperCase()
    const out = []
    if (f.winrateMin !== '') out.push(['winrateMin', `WINRATE ${P} ≥ ${f.winrateMin}%`])
    if (f.pnlMin !== '') out.push(['pnlMin', `PNL ${P} ≥ $${f.pnlMin}`])
    if (f.volumeMin !== '') out.push(['volumeMin', `VOL ${P} ≥ $${f.volumeMin}`])
    if (f.consistencyMin !== '') out.push(['consistencyMin', `GREEN DAYS ${P} ≥ ${f.consistencyMin}%`])
    if (f.fillExitMin !== '') out.push(['fillExitMin', `EXIT/FILL ${P} ≥ ${f.fillExitMin}%`])
    if (f.fillExitMax !== '') out.push(['fillExitMax', `EXIT/FILL ${P} ≤ ${f.fillExitMax}%`])
    if (f.pnlQualityMin !== '') out.push(['pnlQualityMin', `PNL QUALITY ≥ $${f.pnlQualityMin}`])
    return out
  }, [f, period])

  return (
    <div>
      <input
        className="search-box"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="&gt; search wallet address / name / x handle…"
      />

      <Folder id="screener-filters" title="FILTERS" open>
        <div className="sort-row">
          {PERIODS.map((p) => (
            <button key={p} className={`chip ${period === p ? 'active' : ''}`} onClick={() => setPeriod(p)}>
              {p.toUpperCase()}
            </button>
          ))}
        </div>

        <div className="filter-grid">
          <FilterSlider
            label={`WIN RATE ${period.toUpperCase()} ≥ %`}
            value={f.winrateMin} onChange={(v) => setF((p) => ({ ...p, winrateMin: v }))}
            min={0} max={100} step={5} placeholder="off"
          />
          <FilterSlider
            label={`PNL ${period.toUpperCase()} ≥ $`}
            value={f.pnlMin} onChange={(v) => setF((p) => ({ ...p, pnlMin: v }))}
            min={0} max={25000} step={250} placeholder="off"
          />
          <FilterSlider
            label={`VOLUME ${period.toUpperCase()} ≥ $`}
            value={f.volumeMin} onChange={(v) => setF((p) => ({ ...p, volumeMin: v }))}
            min={0} max={250000} step={2500} placeholder="off"
          />
          <FilterSlider
            label={`GREEN DAYS ${period.toUpperCase()} ≥ %`}
            value={f.consistencyMin} onChange={(v) => setF((p) => ({ ...p, consistencyMin: v }))}
            min={0} max={100} step={5} placeholder="off"
          />
          <FilterSlider
            label={`EXIT/FILL ${period.toUpperCase()} ≥ %`}
            value={f.fillExitMin} onChange={(v) => setF((p) => ({ ...p, fillExitMin: v }))}
            min={0} max={100} step={5} placeholder="off"
          />
          <FilterSlider
            label={`EXIT/FILL ${period.toUpperCase()} ≤ %`}
            value={f.fillExitMax} onChange={(v) => setF((p) => ({ ...p, fillExitMax: v }))}
            min={0} max={200} step={5} off="max" placeholder="off"
          />
          <FilterSlider
            label="PNL QUALITY (RPNL − UPNL) ≥ $"
            value={f.pnlQualityMin} onChange={(v) => setF((p) => ({ ...p, pnlQualityMin: v }))}
            min={-25000} max={25000} step={250} placeholder="off"
          />
        </div>

        {chips.length > 0 && (
          <div className="sort-row">
            {chips.map(([k, label]) => (
              <button key={k} className="chip active" onClick={() => clearField(k)} title="click to clear">
                {label} ×
              </button>
            ))}
            <button className="chip" onClick={clearAll}>CLEAR ALL</button>
          </div>
        )}
      </Folder>

      <div className="sort-row">
        {SORTS.map(([k, l]) => (
          <button key={k} className={`chip ${sort === k ? 'active' : ''}`} onClick={() => setSort(k)}>
            {l}
          </button>
        ))}
      </div>

      {loading ? (
        <>
          <div className="card skeleton" />
          <div className="card skeleton" />
          <div className="card skeleton" />
        </>
      ) : traders.length === 0 ? (
        <div className="muted">
          {search.trim() ? `no cached wallet matches "${search.trim()}"` : 'no wallets match these filters'}
        </div>
      ) : (
        traders.map((t) => (
          <TraderCard key={t.address} t={t} period={period} onFollowed={onFollowed} balance={balance} />
        ))
      )}
    </div>
  )
}
