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
      <div className="logo"><span>P</span> PolyTrade</div>
      <div className="section-header">RECOVER EXISTING WALLET</div>
      <h1>Link Telegram without creating another wallet.</h1>
      <p className="muted">An authenticated legacy wallet already exists in this browser. Link this Telegram account to preserve that wallet and its funds.</p>
      <div className="card"><div className="muted small">EXISTING WALLET</div><div className="addr">{address}</div></div>
      <label className="ack-row card">
        <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
        <span>I want to link this Telegram identity to the existing wallet. Do not create a second wallet.</span>
      </label>
      {err && <div className="warn-box" role="alert">{err}</div>}
      <button className="btn" disabled={!confirmed || busy} onClick={link}>
        {busy ? 'LINKING…' : 'LINK TELEGRAM & PRESERVE WALLET'}
      </button>
    </div>
  )
}
