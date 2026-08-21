import Folder from '../components/Folder'
import CopiedWallets from '../components/CopiedWallets'

/* Wallet research is a standalone public surface at /screener. The authenticated
   Mini App home is intentionally limited to the wallets already copied by this
   account and the settings attached to them. */
export default function Home() {
  return (
    <div>
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
