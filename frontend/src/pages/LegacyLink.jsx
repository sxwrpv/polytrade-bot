import { useState } from 'react'
import { api, haptic, saveSession } from '../api'

export default function LegacyLink({ initData, address, onDone }) {
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  async function link() {
    if (!confirmed || busy) return
    setBusy(true)
    setErr('')
    try {
      const result = await api.linkTelegram(initData)
      saveSession({ address: result.address })
      haptic('success')
      onDone(result.address)
    } catch (error) {
      setErr(String(error.message || error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="onboard onboard-flow">
      <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
      <div className="eyebrow">Recover existing wallet</div>
      <h1>Link Telegram without creating another wallet.</h1>
      <p className="muted">An authenticated legacy wallet already exists in this browser. Link this Telegram account to preserve that wallet and its funds.</p>
      <div className="card account-row">
        <span className="account-label">Existing wallet</span>
        <div className="addr">{address}</div>
      </div>
      <label className="ack-row card">
        <span className="ck">
          <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
          <span className="ck-box" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5" pathLength="1" /></svg></span>
        </span>
        <span>I want to link this Telegram identity to the existing wallet. Do not create a second wallet.</span>
      </label>
      {err && <div className="warn-box" role="alert">{err}</div>}
      <button type="button" className="btn btn-block" disabled={!confirmed || busy} onClick={link}>
        {busy ? 'Linking…' : 'Link Telegram and keep this wallet'}
      </button>
    </div>
  )
}
