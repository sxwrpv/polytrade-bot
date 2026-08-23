import { useState } from 'react'
import { api } from '../api'
import SettingSlider from './SettingSlider'

/* Start copying a wallet: paste, set limits, confirm.
 *
 * Progressive disclosure, in that order. The card asks for one thing at a
 * time — an empty form showing an address field plus two sliders plus a button
 * is three decisions before anything has happened, and it is where people stop.
 * The limits appear once there is a wallet to apply them to.
 *
 * Copying spends real money, so the last step is always an explicit confirm
 * that restates the wallet and both limits in words. This is the only control
 * on Home that spends.
 */

const ADDRESS = /^0x[0-9a-fA-F]{40}$/

/* Server-side defaults (backend/config.py). Mirrored so the sliders open where
   the backend would have put them; the values are still sent explicitly, so a
   drift between these and the server changes the starting position of a
   slider, never what silently gets applied. */
const DEFAULT_RATIO_PCT = 1.0
const DEFAULT_MAX_USD = 15

/** Accept a bare address or anything ending in one — a Polymarket profile URL,
 *  a PolyTrade screener link, a pasted row. */
export function parseAddress(input) {
  const raw = String(input ?? '').trim()
  if (ADDRESS.test(raw)) return raw.toLowerCase()
  const m = /(0x[0-9a-fA-F]{40})(?:[/?#]|$)/.exec(raw)
  return m ? m[1].toLowerCase() : null
}

export default function CopyWalletCard({ onAdded }) {
  const [value, setValue] = useState('')
  const [ratio, setRatio] = useState(DEFAULT_RATIO_PCT)
  const [maxUsd, setMaxUsd] = useState(DEFAULT_MAX_USD)
  const [state, setState] = useState('idle')   // idle | confirm | working | done
  const [error, setError] = useState('')

  const address = parseAddress(value)
  const touched = value.trim().length > 0
  // Only complain once there is enough typed to be wrong about.
  const malformed = touched && !address && value.trim().length >= 42

  // Clamped to the same bounds the API enforces, so the request cannot be
  // rejected for a value this form allowed.
  const ratioOk = Number.isFinite(Number(ratio)) && ratio > 0 && ratio <= 20
  const maxOk = Number.isFinite(Number(maxUsd)) && maxUsd >= 1 && maxUsd <= 500
  const ready = Boolean(address) && ratioOk && maxOk

  function reset() {
    setValue('')
    setRatio(DEFAULT_RATIO_PCT)
    setMaxUsd(DEFAULT_MAX_USD)
  }

  async function start() {
    if (!ready) return
    setState('working')
    setError('')
    try {
      await api.follow(address, {
        copy_ratio_pct: Number(ratio),
        max_position_usd: Number(maxUsd),
      })
      setState('done')
      reset()
      onAdded?.(address)
      setTimeout(() => setState('idle'), 3000)
    } catch (e) {
      setState('idle')
      setError(String(e.message || e))
    }
  }

  if (state === 'done') {
    return (
      <div className="card copy-wallet">
        <p className="copy-wallet-ok">
          Copying started. Change the ratio or the per-trade cap any time under
          Copied Wallets.
        </p>
      </div>
    )
  }

  return (
    <div className="card copy-wallet">
      <label className="fld">
        WALLET ADDRESS
        <input
          value={value}
          onChange={(e) => { setValue(e.target.value); setState('idle'); setError('') }}
          placeholder="0x… or paste a profile link"
          spellCheck="false"
          autoComplete="off"
          aria-invalid={malformed || undefined}
        />
      </label>

      {malformed && (
        <p className="copy-wallet-err">
          That is not a wallet address. It should be 0x followed by 40 hex characters.
        </p>
      )}
      {error && <p className="copy-wallet-err">{error}</p>}

      {/* Limits appear only once there is a wallet to apply them to. */}
      {address && (
        <div className="copy-wallet-settings">
          <div className="control-label">LIMITS FOR THIS WALLET</div>

          <SettingSlider
            label="COPY RATIO"
            value={ratio}
            onChange={setRatio}
            min={0.5}
            max={20}
            step={0.5}
            unit="%"
            hint="Your position = their position value × this. 1% of a $2,000 trade is $20."
          />
          <SettingSlider
            label="MAX PER TRADE"
            value={maxUsd}
            onChange={setMaxUsd}
            min={1}
            max={500}
            step={1}
            unit="$"
            hint="A hard ceiling. A trade the ratio sizes above this is capped, not skipped."
          />

          {!ratioOk && <p className="copy-wallet-err">Copy ratio must be between 0.5% and 20%.</p>}
          {!maxOk && <p className="copy-wallet-err">Max per trade must be between $1 and $500.</p>}
        </div>
      )}

      {state === 'confirm' ? (
        <div className="copy-wallet-confirm">
          <p>
            Start copying <b>{address.slice(0, 10)}…{address.slice(-6)}</b> at{' '}
            <b>{ratio}%</b> of their position size, capped at <b>${maxUsd}</b> per trade?
            This spends real money on their next qualifying trade.
          </p>
          <div className="copy-wallet-actions">
            <button type="button" className="btn" onClick={start}>Start copying</button>
            <button type="button" className="btn btn-ghost" onClick={() => setState('idle')}>Cancel</button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          className="btn"
          disabled={!ready || state === 'working'}
          onClick={() => setState('confirm')}
        >{state === 'working' ? 'STARTING…' : 'REVIEW AND COPY'}</button>
      )}

      <p className="copy-wallet-note">
        Copying starts from their next qualifying trade — open positions are not back-filled.
      </p>
    </div>
  )
}
