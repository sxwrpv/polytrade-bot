import { useState } from 'react'
import { api, CURRENT_TERMS_VERSION, haptic, saveSession } from '../api'
import { useRedeemStatus } from '../redeemStatus'
import CopyText from '../components/CopyText'
import Icon from '../components/Icon'
import { FundingAccess } from '../components/DepositAddresses'
import { Notice } from '../components/UiState'

function Progress({ step }) {
  return (
    <div className="onboard-progress">
      <span className="onboard-progress-label">Step {step} of 2</span>
      <span
        className="onboard-progress-bar"
        role="progressbar"
        aria-label="Wallet setup"
        aria-valuemin={1}
        aria-valuemax={2}
        aria-valuenow={step}
        style={{ '--progress': step === 1 ? '50%' : '100%' }}
      ><i /></span>
    </div>
  )
}

export default function Onboarding({ onDone }) {
  const redeem = useRedeemStatus()
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
        <Progress step={2} />
        <div className="eyebrow">{result.created ? 'WALLET CREATED' : 'WALLET FOUND'}</div>
        <h1>{result.created
          ? 'Your custodial wallet was created.'
          : 'Your existing custodial wallet is already linked to this Telegram account.'}</h1>
        <p className="muted">Your wallet address:</p>
        <CopyText value={result.address} label="Copy your wallet address" />
        {result.gasless && <p className="muted small">Wallet setup was attempted through the relayer. Trading readiness and approvals may still be incomplete; some external wallet or network actions may also require MATIC or other fees.</p>}

        <div className="section-header">Fund your wallet</div>
        <FundingAccess gasless={result.gasless} />
        <Notice icon="shield">Keep Telegram access secure. You can review key export options under Account, but exposing a private key can put all wallet funds at risk.</Notice>
        <button type="button" className="btn btn-block" onClick={() => onDone(result.address)}>Enter PolyTrade</button>
      </div>
    )
  }

  return (
    <div className="onboard onboard-flow">
      <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
      <Progress step={1} />
      <div className="eyebrow">Before wallet creation</div>
      <h1>Understand what you’re opening.</h1>
      <p className="muted">PolyTrade uses a custodial wallet to place real-money prediction-market trades based on wallets you choose to follow.</p>
      <div className="onboard-terms">
        <article><Icon name="shield" /><b>Custody &amp; security</b><p>PolyTrade creates and operates the wallet. Protect your Telegram account and understand that custody and software failures can put funds at risk.</p></article>
        <article><Icon name="alert" /><b>Real money, real risk</b><p>Trades can lose some or all committed funds. Copy execution may differ, be delayed, or fail; past activity is not a promise of results.</p></article>
        <article><Icon name="globe" /><b>Eligibility &amp; access</b><p>You are responsible for ensuring that Polymarket and this service are permitted where you live and that you meet all applicable eligibility requirements.</p></article>
        <article><Icon name="transfer" /><b>Moving funds</b><p>{redeem.auto
          ? 'There is no in-app withdrawal. Resolved positions are redeemed automatically. External tools, supported networks, and fees may be required to move funds out.'
          : 'There is no in-app withdrawal or automatic redemption. External tools, supported networks, and fees may be required.'}</p></article>
      </div>
      <label className="ack-row card">
        <span className="ck">
          <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
          <span className="ck-box" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5" pathLength="1" /></svg></span>
        </span>
        <span>I acknowledge the custodial model, real-money risk, eligibility responsibility, and funding limitations.</span>
      </label>
      {err && <div className="warn-box" role="alert">{err}</div>}
      <button type="button" className="btn btn-block" disabled={busy || !acknowledged} onClick={create}>
        {busy ? 'Creating secure wallet…' : 'Acknowledge and create wallet'}
      </button>
      <p className="muted small center">Only available from a verified Telegram launch.</p>
    </div>
  )
}
