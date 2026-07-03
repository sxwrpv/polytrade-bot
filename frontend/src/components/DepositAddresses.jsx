import { useEffect, useState } from 'react'
import { api } from '../api'
import ChainIcon from './ChainIcon'
import CopyText from './CopyText'

const SHORT_LABEL = { evm: 'EVM', svm: 'SOLANA', tron: 'TRON', btc: 'BITCOIN' }

// Polymarket's own bridge: send USDC/USDT (or other supported assets) from any
// of these chains to the matching address — it arrives as pUSD automatically.
// No gas needed for this step, ever. Whether the wallet ALSO needs a little
// MATIC for the one-time trading-allowance step depends on wallet type: a
// gasless Deposit Wallet's allowances were already set for free at creation
// time (via the relayer); a plain EOA fallback wallet still needs that one
// on-chain approval itself.
export default function DepositAddresses({ gasless }) {
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState('')
  const [selected, setSelected] = useState(null)

  useEffect(() => {
    api.depositAddress()
      .then((r) => {
        setRows(r.addresses)
        setSelected(r.addresses[0]?.chain ?? null)
      })
      .catch((e) => setErr(String(e.message || e)))
  }, [])

  if (err) return <div className="warn-box">{err}</div>
  if (!rows) return <div className="muted">loading deposit addresses…</div>

  const current = rows.find((r) => r.chain === selected)

  return (
    <div>
      <p className="muted small" style={{ marginBottom: 10 }}>
        Pick a chain, send USDC or USDT (or other supported assets) there — it converts to pUSD
        in your wallet automatically. No gas needed for this step.
      </p>

      <div className="sort-row">
        {rows.map((r) => (
          <button
            key={r.chain}
            className={`chip chip-icon ${selected === r.chain ? 'active' : ''}`}
            onClick={() => setSelected(r.chain)}
          >
            <ChainIcon chain={r.chain} />
            {SHORT_LABEL[r.chain] || r.chain.toUpperCase()}
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
