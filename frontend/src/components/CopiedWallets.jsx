import { useEffect, useState, useCallback } from 'react'
import { api } from '../api'
import WalletRiskCard from './WalletRiskCard'
import { ErrorState, SkeletonCards, StateBlock } from './UiState'

export default function CopiedWallets({ onChange, onCount }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    setLoading(true)
    api.following()
      .then((r) => { setRows(r); setError(null); setLoading(false); onCount?.(r.length); onChange?.() })
      .catch((e) => { setError(e); setLoading(false) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => { load() }, [load])

  if (loading && rows.length === 0) return <SkeletonCards count={2} label="Loading copied wallets" />
  if (error && rows.length === 0) {
    return <ErrorState title="Couldn’t load your copied wallets" error={error} onRetry={load} retrying={loading} />
  }
  if (rows.length === 0) {
    return (
      <StateBlock icon="wallet" title="No copied wallets yet">
        Paste a wallet address under Copy a wallet, or find one to follow in the Wallet Screener.
      </StateBlock>
    )
  }
  return (
    <div>
      {rows.map((w) => <WalletRiskCard key={w.trader_address} w={w} onChange={load} />)}
    </div>
  )
}
