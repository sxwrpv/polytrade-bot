import { useEffect, useState } from 'react'
import { api } from '../api'
import ActivityFeed from '../components/ActivityFeed'
import Folder from '../components/Folder'
import GettingStarted from '../components/GettingStarted'
import KpiStrip from '../components/KpiStrip'
import WalletScreener from '../components/WalletScreener'
import CopiedWallets from '../components/CopiedWallets'

export default function Home() {
  const [me, setMe] = useState(null)
  const [pnl, setPnl] = useState(null)
  const [settings, setSettings] = useState(null)
  const [followingCount, setFollowingCount] = useState(0)

  const refreshFollowing = () => api.following().then((r) => setFollowingCount(r.length)).catch(() => {})

  useEffect(() => {
    // fast paint without balance, then upgrade — the balance read builds the
    // CLOB client server-side on first call, which can take a few seconds.
    // The plain call never overwrites a balance-carrying result if it loses
    // the race.
    api.me().then((m) => setMe((prev) => prev ?? m)).catch(() => {})
    api.me(true).then(setMe).catch(() => {})
    api.pnl('7d').then(setPnl).catch(() => {})
    api.getSettings().then(setSettings).catch(() => {})
    refreshFollowing()
  }, [])

  return (
    <div>
      <KpiStrip me={me} pnl={pnl} followingCount={followingCount} />

      {me && <GettingStarted balance={me.balance} followingCount={followingCount} />}

      <Folder id="home-activity" title="ACTIVITY" open>
        <ActivityFeed />
      </Folder>

      <Folder id="home-screener" title="WALLET SCREENER" open>
        <WalletScreener onFollowed={refreshFollowing} balance={me?.balance} />
      </Folder>

      <Folder id="home-copied" title="COPIED WALLETS" count={followingCount}>
        <CopiedWallets defaults={settings} onChange={refreshFollowing} />
      </Folder>
    </div>
  )
}
