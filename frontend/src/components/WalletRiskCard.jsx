import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { CAPITAL_SHARE, MULTIPLIER_STOPS, POSITION_SHARE } from '../copySizing'
import Folder from './Folder'
import SettingSlider from './SettingSlider'
import CopyText from './CopyText'
import Icon from './Icon'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')

// Per-wallet copy settings. Nulls from the DB fall back to these so every
// slider always shows a concrete value; MAX OPEN 0 = unlimited (stored null).
// Must match the backend config fallbacks (backend/config.py + copy_engine
// _follow_risk) so a slider a user never touched shows exactly what the engine
// enforces. The three "no-limit" fields (max_open / max_exposure / daily_loss)
// show 0 when unset = unlimited/none, which is how the engine reads NULL.
const DEFAULTS = {
  sizing_mode: POSITION_SHARE,
  copy_ratio_pct: 1.0,
  capital_multiplier: 1,
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
  ['copy_ratio_pct', 'Copy ratio', 0, 20, 0.1, '%', 'Your copy = their position × this %.'],
  ['capital_multiplier', 'Multiplier', 0.1, 100, 0.1, '×', '1× takes the same share of your cash. 1% of $150 is $1.50, under a $2 minimum copy.'],
  ['max_position_usd', 'Max per position', 1, 500, 1, '$', 'Hard cap for the whole copied market position.'],
  ['min_leader_usd', 'Min leader position', 0, 10000, 50, '$', 'Skip if their position is smaller than this.'],
  ['ignore_below_usd', 'Min copy size', 0.5, 50, 0.5, '$', 'Skip any copy or top-up smaller than this.'],
  ['max_open_positions', 'Max open positions', 0, 50, 1, '', '0 = unlimited.'],
  ['max_total_exposure_usd', 'Max total exposure', 0, 5000, 5, '$', 'Cap on everything open from this wallet. 0 = none.'],
  ['min_price', 'Min price', 0, 1, 0.01, '¢', 'Skip cheaper longshots. 0.10 = 10¢.'],
  ['max_price', 'Max price', 0, 1, 0.01, '¢', 'Skip near-resolved markets. 0.98 = 98¢.'],
  ['max_slippage_pct', 'Max slippage', 0, 10, 0.5, '%', 'Versus the leader’s fill price.'],
  ['daily_loss_limit_usd', 'Daily loss limit', 0, 1000, 1, '$', '0 = none.'],
]
const GROUPS = [
  ['Position sizing', ['copy_ratio_pct', 'max_position_usd', 'ignore_below_usd', 'min_leader_usd']],
  ['Market filters', ['min_price', 'max_price', 'max_slippage_pct']],
  ['Risk limits', ['max_open_positions', 'max_total_exposure_usd', 'daily_loss_limit_usd']],
]
// values where 0 means "no limit" and should persist as NULL, not 0
const ZERO_IS_NULL = new Set(['max_open_positions', 'max_total_exposure_usd', 'daily_loss_limit_usd'])
const STOPS = { capital_multiplier: MULTIPLIER_STOPS }
// How copies are sized (followed_traders.sizing_mode). Capital share puts the
// multiplier where the copy ratio sits; every other limit applies to both.
const MODES = [
  [POSITION_SHARE, 'Their position', 'Your copy is a percentage of their position in that market.'],
  [CAPITAL_SHARE, 'Their cash share', 'A bet that takes 5% of their free cash takes 5% of yours, times the multiplier. Their cash is read on Polygon when they trade; when it can’t be read, that copy is skipped.'],
]
const sizingKeys = (keys, mode) => keys.map((k) => (
  k === 'copy_ratio_pct' && mode === CAPITAL_SHARE ? 'capital_multiplier' : k))

function withDefaults(w) {
  const out = { ...w }
  for (const k of Object.keys(DEFAULTS)) out[k] = w[k] ?? DEFAULTS[k]
  return out
}

export default function WalletRiskCard({ w, onChange }) {
  const [s, setS] = useState(() => withDefaults(w))
  const [saved, setSaved] = useState(null)        // { ok, text } — the last save's outcome
  const [confirmStop, setConfirmStop] = useState(false)
  const [stopping, setStopping] = useState(false)
  const timers = useRef({})
  const savedTimer = useRef(null)

  // Parent reloads are authoritative. Without this, a failed optimistic pause
  // could leave the card showing OFF even though the DB still said ON.
  useEffect(() => setS(withDefaults(w)), [w])
  useEffect(() => () => clearTimeout(savedTimer.current), [])

  async function persist(patch) {
    clearTimeout(savedTimer.current)
    try {
      await api.followSettings(w.trader_address, patch)
      setSaved({ ok: true, text: 'Saved' })
      savedTimer.current = setTimeout(() => setSaved(null), 1400)
      return true
    } catch (e) {
      setSaved({ ok: false, text: `Not saved: ${String(e.message || e)}` })
      onChange?.()
      return false
    }
  }

  // live UI update; debounce the network save per-field so dragging a slider
  // doesn't fire a request on every tick
  function set(k, v) {
    setS((p) => ({ ...p, [k]: v }))
    clearTimeout(timers.current[k])
    if (v === '' || !Number.isFinite(Number(v))) return
    timers.current[k] = setTimeout(() => {
      let num = Number(v)
      const val = ZERO_IS_NULL.has(k) && num === 0 ? null : num
      persist({ [k]: val })
    }, 400)
  }

  async function toggleEnabled() {
    const previous = s.paused
    const nextPaused = previous ? 0 : 1
    setS((p) => ({ ...p, paused: nextPaused }))
    const ok = await persist({ paused: !!nextPaused })
    if (!ok) setS((p) => ({ ...p, paused: previous }))
    else onChange?.()
  }

  // Saved at once, like the pause switch, so the sizing rule applies whole.
  async function setMode(mode) {
    const previous = s.sizing_mode
    if (mode === previous) return
    setS((p) => ({ ...p, sizing_mode: mode }))
    const ok = await persist({ sizing_mode: mode })
    if (!ok) setS((p) => ({ ...p, sizing_mode: previous }))
  }

  async function remove() {
    setStopping(true)
    try {
      await api.unfollow(w.trader_address)
      onChange?.()
    } catch (e) {
      setSaved({ ok: false, text: `Could not stop copying: ${String(e.message || e)}` })
      setConfirmStop(false)
    } finally {
      setStopping(false)
    }
  }

  const enabled = !s.paused
  const name = w.display_name || short(w.trader_address)
  const title = (
    <>
      <span className="wallet-name">{name}</span>
      <span className={`tier-badge tier-${w.tier || 'bronze'}`}>{w.tier || 'bronze'}</span>
      <span className={`wallet-status ${enabled ? 'is-on' : 'is-off'}`}>{enabled ? 'Copying' : 'Paused'}</span>
    </>
  )
  const byKey = Object.fromEntries(SLIDERS.map((row) => [row[0], row]))
  const noExposure = !Number(s.max_total_exposure_usd)
  const noDailyLoss = !Number(s.daily_loss_limit_usd)
  const modeIndex = Math.max(0, MODES.findIndex(([mode]) => mode === s.sizing_mode))

  return (
    <Folder id={`wallet-${w.trader_address}`} title={title}>
      <div className="card wallet-card">
        <CopyText value={w.trader_address} display={`${w.trader_address.slice(0, 10)}…${w.trader_address.slice(-8)}`} label="Copy wallet address" />

        <div className="switch-row">
          <span className="switch-copy">
            <b>{enabled ? 'Copying new trades' : 'Copying paused'}</b>
            <span>Paused stops new buys. Open positions still exit and resolve.</span>
          </span>
          <span className="switch">
            <input
              type="checkbox"
              role="switch"
              checked={enabled}
              onChange={toggleEnabled}
              aria-label={`Copy new trades from ${name}`}
            />
            <span className="switch-track" aria-hidden="true"><span className="switch-thumb" /></span>
          </span>
        </div>

        {GROUPS.map(([group, keys]) => (
          <div className="settings-group" key={group}>
            <div className="settings-group-title">{group}</div>
            {group === 'Position sizing' && (
              <div className="sizing-mode">
                <span className="setting-head" id={`sizing-${w.trader_address}`}>Size copies by</span>
                <div
                  className="seg seg-sm"
                  role="group"
                  aria-labelledby={`sizing-${w.trader_address}`}
                  style={{ '--n': MODES.length, '--i': modeIndex }}
                >
                  {MODES.map(([mode, text]) => (
                    <button
                      key={mode}
                      type="button"
                      aria-pressed={MODES[modeIndex][0] === mode}
                      onClick={() => setMode(mode)}
                    >{text}</button>
                  ))}
                </div>
                <span className="setting-hint">{MODES[modeIndex][2]}</span>
              </div>
            )}
            <div className="settings-grid">
              {sizingKeys(keys, MODES[modeIndex][0]).map((k) => {
                const [, label, min, max, step, unit, hint] = byKey[k]
                return (
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
                    stops={STOPS[k]}
                  />
                )
              })}
            </div>
            {group === 'Risk limits' && (noExposure || noDailyLoss) && (
              <div className="notice" role="note">
                <Icon name="alert" />
                <div>
                  <b>Risk warning:</b>{' '}
                  {noExposure && noDailyLoss
                    ? 'no total exposure or daily loss limit is active.'
                    : noExposure
                      ? 'no total exposure limit is active.'
                      : 'no daily loss limit is active.'}
                </div>
              </div>
            )}
          </div>
        ))}

        <div className="wallet-actions">
          {confirmStop ? (
            <div className="confirm-inline" role="alertdialog" aria-label={`Stop copying ${name}`}>
              <p>
                Stop copying <b>{name}</b>? New buys from this wallet stop. Nothing is sold: open
                positions still follow its reductions, and you can close them or let them resolve.
              </p>
              <div className="confirm-inline-actions">
                <button type="button" className="btn btn-ghost" onClick={() => setConfirmStop(false)} disabled={stopping}>Keep copying</button>
                <button type="button" className="btn btn-danger is-strong" onClick={remove} disabled={stopping}>
                  {stopping ? 'Stopping…' : 'Stop copying'}
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="btn btn-danger" onClick={() => setConfirmStop(true)}>Stop copying</button>
          )}
          {saved && !confirmStop && (
            <span className={`fld-status ${saved.ok ? 'is-ok' : 'is-error'}`} role="status">
              {saved.ok && <Icon name="check" className="icon icon-sm" />}
              {saved.text}
            </span>
          )}
        </div>
      </div>
    </Folder>
  )
}
