import { useState } from 'react'
import { api, CURRENT_FUNDING_ACK_VERSION } from '../api'
import { acceptFundingAndLoadAddresses } from '../fundingAcknowledgement'
import ChainIcon from './ChainIcon'
import CopyText from './CopyText'
import { Notice } from './UiState'

const SHORT_LABEL = { evm: 'EVM', svm: 'Solana', tron: 'Tron', btc: 'Bitcoin' }

// The disclosure is deliberately inside the reusable funding component: every
// route starts closed, and the backend independently enforces the durable ack.
export function FundingAccess({ gasless }) {
  const [acknowledged, setAcknowledged] = useState(false)
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState(null)

  async function reveal() {
    if (!acknowledged || busy) return
    setBusy(true)
    setErr('')
    try {
      const result = await acceptFundingAndLoadAddresses({
        api,
        version: CURRENT_FUNDING_ACK_VERSION,
      })
      setRows(result.addresses)
      setSelected(result.addresses[0]?.chain ?? null)
    } catch (error) {
      setErr(String(error.message || error))
    } finally {
      setBusy(false)
    }
  }

  if (!rows) {
    return (
      <div className="security-check card">
        <h2>Before you fund</h2>
        <p>Only send supported assets using the exact chain and address shown. Start small and verify the transfer. PolyTrade does not provide in-app withdrawals or automatic redemption; moving or redeeming funds may require external tools and network fees.</p>
        <label className="ack-row">
          <span className="ck">
            <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
            <span className="ck-box" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5" pathLength="1" /></svg></span>
          </span>
          <span>I understand the security and funding instructions and want to reveal deposit addresses.</span>
        </label>
        {err && <div className="warn-box" role="alert">{err}</div>}
        <button type="button" className="btn" disabled={!acknowledged || busy} onClick={reveal}>
          {busy ? 'Recording acknowledgement…' : 'Acknowledge and show addresses'}
        </button>
      </div>
    )
  }

  const current = rows.find((row) => row.chain === selected)
  const index = Math.max(0, rows.findIndex((row) => row.chain === selected))
  return (
    <div className="card">
      <p className="funding-instructions">
        Choose only a network listed below and send a supported asset to its matching address.
        The bridge converts eligible deposits to pUSD. Confirm asset and network support before sending.
      </p>
      <div className="notice-box funding-disclosure">
        PolyTrade does not offer in-app withdrawals or automatic redemption. Moving funds may require
        external tools, a supported network, and network fees.
      </div>
      <div className="seg" role="group" aria-label="Deposit network" style={{ '--n': rows.length, '--i': index }}>
        {rows.map((row) => (
          <button key={row.chain} type="button" aria-pressed={selected === row.chain} onClick={() => setSelected(row.chain)}>
            <ChainIcon chain={row.chain} />
            {SHORT_LABEL[row.chain] || row.chain.toUpperCase()}
          </button>
        ))}
      </div>
      {current && (
        <div className="funding-address">
          <span className="account-label">{current.label}</span>
          <CopyText value={current.address} label={`Copy ${current.label} deposit address`} />
        </div>
      )}
      {gasless === false && (
        <div className="list-notice">
          <Notice>
            Separately, the first trade needs a one-time on-chain allowance approval, which does
            require a small amount of MATIC on Polygon in this wallet.
          </Notice>
        </div>
      )}
    </div>
  )
}

export default FundingAccess
