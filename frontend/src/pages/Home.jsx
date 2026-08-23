import Folder from '../components/Folder'
import CopiedWallets from '../components/CopiedWallets'
import AccountSummary from '../components/AccountSummary'

/* Home leads with the account's own money — balance and the equity curve —
   because that is what the account opens the app to see. Wallet research stays
   a standalone public surface at /screener; everything else here is the wallets
   already copied by this account. */
export default function Home() {
  return (
    <div>
      <AccountSummary />

      <ScreenerEntryPoint />

      <Folder id="home-copied" title="COPIED WALLETS">
        <CopiedWallets />
      </Folder>
    </div>
  )
}

/* Opens in a new tab. Inside Telegram, `target="_blank"` on an <a> is what the
   WebView hands to the system browser (or Telegram's in-app browser) on both
   iOS and Android. */
function ScreenerEntryPoint() {
  return (
    <div className="card screener-entry">
      <div className="section-header">WALLET SCREENER</div>
      <p className="muted">
        Research public Polymarket wallet history in the standalone screener.
      </p>
      <a
        className="btn"
        href="/screener"
        target="_blank"
        rel="noreferrer noopener"
      >OPEN WALLET SCREENER ↗</a>
    </div>
  )
}
