import { useState } from 'react'
import { api } from '../api'

/* Start copying a wallet by pasting its address.
 *
 * The address is the only input. Ratio and per-trade cap fall back to the
 * server's defaults, and both are editable afterwards from the wallet's own
 * settings — asking for three numbers before anything happens is what stops
 * people finishing.
 *
 * Copying moves real money, so the button never fires straight from the field:
 * a valid address arms a confirm step that states, in words, what is about to
 * start. This is the one control on Home that spends.
 */

const ADDRESS = /^0x[0-9a-fA-F]{40}$/

/** Accept a bare address or anything ending in one — a Polymarket profile URL,
 *  a PolyTrade screener link, a pasted row. */
function parseAddress(input) {
  const raw = String(input ?? '').trim()
  if (ADDRESS.test(raw)) return raw.toLowerCase()
  const m = /(0x[0-9a-fA-F]{40})(?:[/?#]|$)/.exec(raw)
  return m ? m[1].toLowerCase() : null
}

export default function CopyWalletCard({ onAdded }) {
  const [value, setValue] = useState('')
  const [state, setState] = useState('idle')   // idle | confirm | working | done
  const [error, setError] = useState('')

  const address = parseAddress(value)
  const touched = value.trim().length > 0
  // Only complain once there is enough typed to be wrong about.
  const malformed = touched && !address && value.trim().length >= 42

  async function start() {
    if (!address) return
    setState('working')
    setError('')
    try {
      await api.follow(address)
      setState('done')
      setValue('')
      onAdded?.(address)
      setTimeout(() => setState('idle'), 2600)
    } catch (e) {
      setState('idle')
      setError(String(e.message || e))
    }
  }

  return (
    <div className="card copy-wallet">
      <div className="section-header">COPY A WALLET</div>

      <label className="fld">
        WALLET ADDRESS
        <input
          value={value}
          onChange={(e) => { setValue(e.target.value); setState('idle'); setError('') }}
          onKeyDown={(e) => { if (e.key === 'Enter' && address) setState('confirm') }}
          placeholder="0x… or paste a profile link"
          spellCheck="false"
          autoComplete="off"
          aria-invalid={malformed || undefined}
          aria-describedby="copy-wallet-note"
        />
      </label>

      {malformed && (
        <p className="copy-wallet-err">That is not a wallet address. It should be 0x followed by 40 hex characters.</p>
      )}
      {error && <p className="copy-wallet-err">{error}</p>}

      {state === 'done' ? (
        <p className="copy-wallet-ok">
          Copying started. Adjust the ratio and per-trade cap under Copied Wallets.
        </p>
      ) : state === 'confirm' ? (
        <div className="copy-wallet-confirm">
          <p>
            Start copying <b>{address.slice(0, 10)}…{address.slice(-6)}</b> with your default
            ratio and per-trade cap? This spends real money on their next qualifying trade.
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
          disabled={!address || state === 'working'}
          onClick={() => setState('confirm')}
        >{state === 'working' ? 'STARTING…' : 'COPY THIS WALLET'}</button>
      )}

      <p className="copy-wallet-note" id="copy-wallet-note">
        Copying starts from their next qualifying trade — open positions are not back-filled.
      </p>
    </div>
  )
}
