import { useState } from 'react'
import { api, CURRENT_FUNDING_ACK_VERSION } from '../api'
import { acceptFundingAndLoadAddresses } from '../fundingAcknowledgement'
import ChainIcon from './ChainIcon'
import CopyText from './CopyText'

const SHORT_LABEL = { evm: 'EVM', svm: 'SOLANA', tron: 'TRON', btc: 'BITCOIN' }

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
          <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
          <span>I understand the security and funding instructions and want to reveal deposit addresses.</span>
        </label>
        {err && <div className="warn-box" role="alert">{err}</div>}
        <button className="btn" disabled={!acknowledged || busy} onClick={reveal}>
          {busy ? 'RECORDING ACKNOWLEDGEMENT…' : 'ACKNOWLEDGE & REVEAL ADDRESSES'}
        </button>
      </div>
    )
  }

  const current = rows.find((row) => row.chain === selected)
  return (
    <div>
      <p className="muted funding-instructions" style={{ marginBottom: 10 }}>
        Choose only a network listed below and send a supported asset to its matching address.
        The bridge converts eligible deposits to pUSD. Confirm asset and network support before sending.
      </p>
      <div className="notice-box funding-disclosure">
        PolyTrade does not offer in-app withdrawals or automatic redemption. Moving funds may require
        external tools, a supported network, and network fees.
      </div>
      <div className="sort-row">
        {rows.map((row) => (
          <button key={row.chain} className={`chip chip-icon ${selected === row.chain ? 'active' : ''}`} onClick={() => setSelected(row.chain)}>
            <ChainIcon chain={row.chain} />
            {SHORT_LABEL[row.chain] || row.chain.toUpperCase()}
          </button>
        ))}
      </div>
      {current && (
        <div>
          <div className="muted small">{current.label} (click address to copy)</div>
          <CopyText value={current.address} />
        </div>
      )}
      {gasless === false && (
        <div className="warn-box" style={{ marginTop: 12 }}>
          SEPARATELY, THE FIRST TRADE NEEDS A ONE-TIME ON-CHAIN ALLOWANCE APPROVAL, WHICH DOES
          REQUIRE A SMALL AMOUNT OF MATIC ON POLYGON IN THIS WALLET.
        </div>
      )}
    </div>
  )
}

export default FundingAccess
