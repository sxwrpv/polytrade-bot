import { useState } from 'react'
import { api, CURRENT_TERMS_VERSION, haptic, saveSession } from '../api'
import CopyText from '../components/CopyText'
import { FundingAccess } from '../components/DepositAddresses'

export default function Onboarding({ onDone }) {
  const [acknowledged, setAcknowledged] = useState(false)

  const [result, setResult] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function create() {
    if (!acknowledged) return
    setBusy(true)
    setErr('')
    try {
      const initData = window.Telegram?.WebApp?.initData || null
      const response = await api.createWallet({
        init_data: initData,
        terms_accepted: true,
        terms_version: CURRENT_TERMS_VERSION,
      })
      saveSession({ address: response.address })
      haptic('success')
      setResult(response)
    } catch (error) {
      setErr(String(error.message || error))
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="onboard onboard-flow">
        <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
        <div className="onboard-progress"><span className="done">1</span><i /><span className="active">2</span></div>
        <div className="section-header">{result.created ? 'WALLET CREATED' : 'WALLET FOUND'}</div>
        <h1>{result.created
          ? 'Your custodial wallet was created.'
          : 'Your existing custodial wallet is already linked to this Telegram account.'}</h1>
        <p className="muted">Your wallet address (tap to copy):</p>
        <CopyText value={result.address} />
        {result.gasless && <p className="muted small">Wallet setup was attempted through the relayer. Trading readiness and approvals may still be incomplete; some external wallet or network actions may also require MATIC or other fees.</p>}

        <div className="section-header">FUND YOUR WALLET</div>
        <FundingAccess gasless={result.gasless} />
        <div className="notice-box">Keep Telegram access secure. You can review key export options under USER, but exposing a private key can put all wallet funds at risk.</div>
        <button className="btn" onClick={() => onDone(result.address)}>ENTER POLYTRADE</button>
      </div>
    )
  }

  return (
    <div className="onboard onboard-flow">
      <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
      <div className="onboard-progress"><span className="active">1</span><i /><span>2</span></div>
      <div className="eyebrow">BEFORE WALLET CREATION</div>
      <h1>Understand what you’re opening.</h1>
      <p className="muted">PolyTrade uses a custodial wallet to place real-money prediction-market trades based on wallets you choose to follow.</p>
      <div className="onboard-terms">
        <article><b>Custody &amp; security</b><p>PolyTrade creates and operates the wallet. Protect your Telegram account and understand that custody and software failures can put funds at risk.</p></article>
        <article><b>Real money, real risk</b><p>Trades can lose some or all committed funds. Copy execution may differ, be delayed, or fail; past activity is not a promise of results.</p></article>
        <article><b>Eligibility &amp; access</b><p>You are responsible for ensuring that Polymarket and this service are permitted where you live and that you meet all applicable eligibility requirements.</p></article>
        <article><b>Moving funds</b><p>There is no in-app withdrawal or automatic redemption. External tools, supported networks, and fees may be required.</p></article>
      </div>
      <label className="ack-row card">
        <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
        <span>I acknowledge the custodial model, real-money risk, eligibility responsibility, and funding limitations.</span>
      </label>
      {err && <div className="warn-box">{err}</div>}
      <button className="btn" disabled={busy || !acknowledged} onClick={create}>{busy ? 'CREATING SECURE WALLET…' : 'ACKNOWLEDGE & CREATE WALLET'}</button>
      <p className="muted small center">Only available from a verified Telegram launch.</p>
    </div>
  )
}
