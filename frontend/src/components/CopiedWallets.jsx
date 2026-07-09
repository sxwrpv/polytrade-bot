import { useEffect, useState, useCallback } from 'react'
import { api } from '../api'
import WalletRiskCard from './WalletRiskCard'

export default function CopiedWallets({ onChange }) {
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
      {loading ? (
        <div className="muted">loading…</div>
      ) : rows.length === 0 ? (
        <div className="muted">
          not copying anyone yet — find a wallet under COPY WALLET and hit COPY TRADER
        </div>
      ) : (
        rows.map((w) => <WalletRiskCard key={w.trader_address} w={w} onChange={load} />)
      )}
    </div>
  )
}
