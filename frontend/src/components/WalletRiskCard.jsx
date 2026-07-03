import { useState } from 'react'
import { api } from '../api'
import Folder from './Folder'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')
const NULLABLE = new Set(['max_slippage_pct', 'max_total_exposure_usd', 'daily_loss_limit_usd'])
const FIELDS = [
  ['allocation_pct', 'ALLOCATION %'],
  ['max_position_usd', 'MAX / POSITION pUSD'],
  ['max_slippage_pct', 'MAX SLIPPAGE % (blank = default)'],
  ['max_total_exposure_usd', 'MAX EXPOSURE pUSD (blank = none)'],
  ['daily_loss_limit_usd', 'DAILY LOSS LIMIT pUSD (blank = none)'],
]

export default function WalletRiskCard({ w, onChange }) {
  const [s, setS] = useState(w)
  const [saved, setSaved] = useState('')
  const set = (k, v) => setS((p) => ({ ...p, [k]: v }))

  async function save(patch) {
    try {
      await api.followSettings(w.trader_address, patch)
      setSaved('SAVED ✓')
      setTimeout(() => setSaved(''), 1000)
    } catch (e) {
      setSaved(String(e.message || e))
    }
  }
  function saveField(k) {
    const raw = s[k]
    const num = raw === '' || raw == null ? null : Number(raw)
    if (num != null && Number.isNaN(num)) return
    if (num == null && !NULLABLE.has(k)) return
    save({ [k]: num })
  }
  async function togglePause() {
    const next = s.paused ? 0 : 1
    set('paused', next)
    await save({ paused: !!next })
    onChange?.()
  }
  async function remove() {
    await api.unfollow(w.trader_address).catch(() => {})
    onChange?.()
  }

  const title = (
    <span>
      {w.display_name || short(w.trader_address)}{' '}
      <span className={`tier-badge tier-${w.tier || 'bronze'}`}>{(w.tier || 'bronze').toUpperCase()}</span>
      {s.paused ? <span className="neg"> · PAUSED</span> : null}
    </span>
  )

  return (
    <Folder id={`wallet-${w.trader_address}`} title={title}>
      <div className="card">
        <div className="muted">{w.trader_address}</div>
        <div className="tc-stats" style={{ marginTop: 8 }}>
          <span>ALLOC {s.allocation_pct}%</span>
          <span>MAX ${s.max_position_usd}</span>
          <span>OPEN {w.open_positions || 0}</span>
        </div>
        {FIELDS.map(([k, label]) => (
          <label className="fld" key={k}>
            {label}
            <input value={s[k] ?? ''} onChange={(e) => set(k, e.target.value)} onBlur={() => saveField(k)} />
          </label>
        ))}
        <div className="pc-row" style={{ marginTop: 4 }}>
          <button className={`btn ${s.paused ? 'btn-danger' : ''}`} onClick={togglePause}>
            {s.paused ? 'RESUME' : 'PAUSE'}
          </button>
          <button className="btn btn-danger" onClick={remove}>UNFOLLOW</button>
        </div>
        {saved && <div className="muted">{saved}</div>}
      </div>
    </Folder>
  )
}
