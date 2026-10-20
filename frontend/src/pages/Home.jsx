import { useState } from 'react'
import Folder from '../components/Folder'
import CopiedWallets from '../components/CopiedWallets'
import BalanceCard from '../components/BalanceCard'
import CopyWalletCard from '../components/CopyWalletCard'
import Icon from '../components/Icon'

/* Home leads with the account's own money — balance and the equity curve —
   because that is what the account opens the app to see. Wallet research stays
   a standalone public surface at /screener; everything else here is the wallets
   already copied by this account. */
export default function Home() {
  // Bumped after a wallet is added so the list below refetches rather than
  // showing a stale set until the next tab switch.
  const [added, setAdded] = useState(0)
  const [count, setCount] = useState(null)
  return (
    <div>
      <BalanceCard />

      <Folder id="home-copy-wallet" title="Copy a wallet">
        <CopyWalletCard onAdded={() => setAdded((n) => n + 1)} />
      </Folder>

      <Folder id="home-copied" title="Copied wallets" open count={count ?? undefined}>
        <CopiedWallets key={added} onCount={setCount} />
      </Folder>

      <ScreenerEntryPoint />
    </div>
  )
}

/* Opens in a new tab. Inside Telegram, `target="_blank"` on an <a> is what the
   WebView hands to the system browser (or Telegram's in-app browser) on both
   iOS and Android. */
function ScreenerEntryPoint() {
  return (
    <a
      className="screener-entry"
      href="/screener"
      target="_blank"
      rel="noreferrer noopener"
    >
      <span className="state-icon"><Icon name="search" /></span>
      <span>
        <b>Find wallets to copy</b>
        <span>Research public Polymarket wallet history in the Wallet Screener.</span>
      </span>
      <Icon name="external" className="icon icon-sm" />
    </a>
  )
}
