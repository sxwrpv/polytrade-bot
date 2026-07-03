import { useEffect, useState, useCallback } from 'react'
import { api } from '../api'
import CopyWalletForm from './CopyWalletForm'
import WalletRiskCard from './WalletRiskCard'
import Folder from './Folder'

export default function CopiedWallets({ defaults, onChange }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    setLoading(true)
    api.following()
      .then((r) => { setRows(r); setLoading(false); onChange?.() })
      .catch(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => { load() }, [load])

  return (
    <div>
      <Folder id="copied-add-wallet" title="COPY A WALLET" open={rows.length === 0}>
        <CopyWalletForm defaults={defaults} onAdded={load} />
      </Folder>

      <div className="section-header">YOUR WALLETS ({rows.length})</div>
      {loading ? (
        <div className="muted">loading…</div>
      ) : rows.length === 0 ? (
        <div className="muted">not copying anyone yet — add a wallet above or COPY from the WALLET SCREENER</div>
      ) : (
        rows.map((w) => <WalletRiskCard key={w.trader_address} w={w} onChange={load} />)
      )}
    </div>
  )
}
