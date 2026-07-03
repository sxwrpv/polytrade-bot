import { useState } from 'react'
import { api, haptic, saveSession } from '../api'
import CopyText from '../components/CopyText'
import DepositAddresses from '../components/DepositAddresses'

export default function Onboarding({ onDone }) {
  const [result, setResult] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function create() {
    setBusy(true)
    setErr('')
    try {
      // Inside Telegram, initData links the account — a returning Telegram
      // user gets their existing wallet back instead of a duplicate. The
      // referral code rides in via the t.me deep link's start_param (or ?ref=
      // on the web).
      const initData = window.Telegram?.WebApp?.initData || null
      const referredBy =
        window.Telegram?.WebApp?.initDataUnsafe?.start_param ||
        new URLSearchParams(window.location.search).get('ref') ||
        null
      const r = await api.createWallet({ init_data: initData, referred_by: referredBy })
      saveSession({ address: r.address, token: r.api_token })
      haptic('success')
      setResult(r)
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="onboard">
        <div className="section-header">WALLET READY</div>
        <p className="muted">Your wallet address (click to copy):</p>
        <CopyText value={result.address} />

        <div className="section-header">FUND YOUR WALLET</div>
        {result.gasless && (
          <p className="pos small" style={{ marginBottom: 8 }}>
            &gt; GASLESS WALLET — trading approvals are already set, no MATIC ever needed.
          </p>
        )}
        <DepositAddresses gasless={result.gasless} />

        <div className="warn-box">
          BACK UP YOUR PRIVATE KEY VIA USER &gt; EXPORT — IF YOU LOSE IT, YOUR FUNDS ARE GONE.
        </div>
        <button className="btn" onClick={() => onDone(result.address)}>ENTER</button>
      </div>
    )
  }

  return (
    <div className="onboard">
      <div className="logo">&gt; POLYMARKET COPYBOT</div>
      <div className="tagline">&gt; FREE. OPEN SOURCE. REAL TRADES.</div>
      {err && <div className="warn-box">{err}</div>}
      <button className="btn" disabled={busy} onClick={create}>
        {busy ? 'CREATING…' : 'CREATE WALLET'}
      </button>
    </div>
  )
}
