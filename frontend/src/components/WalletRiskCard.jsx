import { useRef, useState } from 'react'
import { api } from '../api'
import Folder from './Folder'
import SettingSlider from './SettingSlider'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')

// Per-wallet copy settings. Nulls from the DB fall back to these so every
// slider always shows a concrete value; MAX OPEN 0 = unlimited (stored null).
// Must match the backend config fallbacks (backend/config.py + copy_engine
// _follow_risk) so a slider a user never touched shows exactly what the engine
// enforces. The three "no-limit" fields (max_open / max_exposure / daily_loss)
// show 0 when unset = unlimited/none, which is how the engine reads NULL.
const DEFAULTS = {
  copy_ratio_pct: 1.0,
  max_position_usd: 15,
  min_leader_usd: 0,
  ignore_below_usd: 2,
  max_open_positions: 0,
  max_total_exposure_usd: 0,
  min_price: 0.1,
  max_price: 0.98,
  max_slippage_pct: 2,
  daily_loss_limit_usd: 0,
}

// [key, label, min, max, step, unit, hint]
const SLIDERS = [
  ['copy_ratio_pct', 'RATIO %', 0, 20, 0.1, '%', 'copy = leader position × this %'],
  ['max_position_usd', 'MAX / TRADE', 1, 500, 1, '$', 'hard cap per copied position'],
  ['min_leader_usd', 'MIN LEADER', 0, 10000, 50, '$', "skip if leader's position is smaller"],
  ['ignore_below_usd', 'IGNORE POSITIONS <', 0, 50, 0.5, '$', 'skip if our copy would be this small'],
  ['max_open_positions', 'MAX OPEN', 0, 50, 1, '', '0 = unlimited'],
  ['max_total_exposure_usd', 'MAX EXPOSURE', 0, 5000, 50, '$', 'cap total open on this wallet (0 = none)'],
  ['min_price', 'MIN PRICE', 0, 1, 0.01, '', 'skip cheaper longshots'],
  ['max_price', 'MAX PRICE', 0, 1, 0.01, '', 'skip near-resolved markets'],
  ['max_slippage_pct', 'MAX SLIPPAGE %', 0, 10, 0.5, '%', 'vs leader fill price'],
  ['daily_loss_limit_usd', 'DAILY LOSS LIMIT', 0, 1000, 10, '$', '0 = none'],
]
// values where 0 means "no limit" and should persist as NULL, not 0
const ZERO_IS_NULL = new Set(['max_open_positions', 'max_total_exposure_usd', 'daily_loss_limit_usd'])

function withDefaults(w) {
  const out = { ...w }
  for (const k of Object.keys(DEFAULTS)) out[k] = w[k] ?? DEFAULTS[k]
  return out
}

export default function WalletRiskCard({ w, onChange }) {
  const [s, setS] = useState(() => withDefaults(w))
  const [saved, setSaved] = useState('')
  const timers = useRef({})

  async function persist(patch) {
    try {
      await api.followSettings(w.trader_address, patch)
      setSaved('SAVED ✓')
      setTimeout(() => setSaved(''), 900)
    } catch (e) {
      setSaved(String(e.message || e))
    }
  }

  // live UI update; debounce the network save per-field so dragging a slider
  // doesn't fire a request on every tick
  function set(k, v) {
    setS((p) => ({ ...p, [k]: v }))
    if (v === '' || Number.isNaN(Number(v))) return
    clearTimeout(timers.current[k])
    timers.current[k] = setTimeout(() => {
      let num = Number(v)
      const val = ZERO_IS_NULL.has(k) && num === 0 ? null : num
      persist({ [k]: val })
    }, 400)
  }

  async function toggleEnabled() {
    const nextPaused = s.paused ? 0 : 1
    setS((p) => ({ ...p, paused: nextPaused }))
    await persist({ paused: !!nextPaused })
    onChange?.()
  }

  async function remove() {
    await api.unfollow(w.trader_address).catch(() => {})
    onChange?.()
  }

  const enabled = !s.paused
  const title = (
    <span>
      {w.display_name || short(w.trader_address)}{' '}
      <span className={`tier-badge tier-${w.tier || 'bronze'}`}>{(w.tier || 'bronze').toUpperCase()}</span>
      {enabled ? null : <span className="neg"> · OFF</span>}
    </span>
  )

  return (
    <Folder id={`wallet-${w.trader_address}`} title={title}>
      <div className="card">
        <div className="muted small">{w.trader_address}</div>

        <label className="fld fld-slider" style={{ marginTop: 8 }}>
          <span className="setting-head">
            <span>ENABLED</span>
            <button
              className={`chip ${enabled ? 'active' : ''}`}
              onClick={toggleEnabled}
            >
              {enabled ? 'ON' : 'OFF'}
            </button>
          </span>
          <span className="setting-hint muted small">
            off = no new buys; open positions still exit &amp; resolve
          </span>
        </label>

        <div className="settings-grid">
          {SLIDERS.map(([k, label, min, max, step, unit, hint]) => (
            <SettingSlider
              key={k}
              label={label}
              value={s[k]}
              onChange={(v) => set(k, v)}
              min={min}
              max={max}
              step={step}
              unit={unit}
              hint={hint}
            />
          ))}
        </div>

        <div className="pc-row" style={{ marginTop: 8 }}>
          <button className="btn btn-danger" onClick={remove}>UNFOLLOW</button>
          {saved && <span className="muted">{saved}</span>}
        </div>
      </div>
    </Folder>
  )
}
