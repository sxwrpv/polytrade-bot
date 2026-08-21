import { useEffect, useState } from 'react'
import { api } from '../api'
import Folder from '../components/Folder'
import GettingStarted from '../components/GettingStarted'
import KpiStrip from '../components/KpiStrip'
import CopiedWallets from '../components/CopiedWallets'
import TraderCard from '../components/TraderCard'

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

/* Wallet research moved to the standalone screener at /screener, which is a
   research surface in its own right rather than a card list squeezed into a
   Mini App tab. Everything account-specific — who you copy, and the risk
   settings for each — stays here, behind the session. */
export default function Home() {
  const [me, setMe] = useState(null)
  const [pnl, setPnl] = useState(null)
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
    refreshFollowing()
  }, [])

  return (
    <div>
      <KpiStrip me={me} pnl={pnl} followingCount={followingCount} />

      {me && <GettingStarted balance={me.balance} followingCount={followingCount} />}

      <ScreenerEntryPoint />

      <Folder id="home-add-wallet" title="COPY A WALLET BY ADDRESS">
        <AddWalletByAddress onFollowed={refreshFollowing} balance={me?.balance} />
      </Folder>

      <Folder id="home-copied" title="COPIED WALLETS" count={followingCount}>
        <CopiedWallets onChange={refreshFollowing} />
      </Folder>
    </div>
  )
}

/* Browsing moved to /screener, but acting on what you found did not: this is
   where an address pasted back from the screener becomes a copied wallet. It
   uses the authenticated /api/traders/{address} lookup — the same one the old
   in-app screener used — so consent, risk settings and the follow transaction
   keep exactly the semantics they had. */
function AddWalletByAddress({ onFollowed, balance }) {
  const [address, setAddress] = useState('')
  const [trader, setTrader] = useState(null)
  const [state, setState] = useState('idle')
  const [error, setError] = useState('')

  const value = address.trim()
  const valid = ADDRESS_RE.test(value)

  const look = async (event) => {
    event.preventDefault()
    if (!valid) return
    setState('loading')
    setError('')
    setTrader(null)
    try {
      setTrader(await api.trader(value.toLowerCase()))
      setState('ready')
    } catch (problem) {
      setError(String(problem.message || problem))
      setState('error')
    }
  }

  return (
    <div>
      <form className="add-wallet-row" onSubmit={look}>
        <label className="visually-hidden" htmlFor="add-wallet">Wallet address</label>
        <input
          id="add-wallet" value={address} spellCheck="false" autoComplete="off"
          placeholder="paste a 0x wallet address"
          onChange={(event) => setAddress(event.target.value)}
        />
        <button className="btn" type="submit" disabled={!valid || state === 'loading'}>
          {state === 'loading' ? 'CHECKING…' : 'LOOK UP'}
        </button>
      </form>
      {value && !valid && (
        <div className="muted small">expected 0x followed by 40 hex characters</div>
      )}
      {state === 'error' && <div className="warn-box">wallet check failed: {error}</div>}
      {state === 'loading' && <div className="card skeleton" />}
      {state === 'ready' && trader && (
        <TraderCard t={trader} period="30d" onFollowed={onFollowed} balance={balance} />
      )}
    </div>
  )
}

/* Opens in a new tab. Inside Telegram, `target="_blank"` on an <a> is what the
   WebView hands to the system browser (or Telegram's in-app browser) on both
   iOS and Android; window.open is the call Telegram's WebView commonly blocks.
   `rel="noreferrer noopener"` keeps the opener detached either way. */
function ScreenerEntryPoint() {
  return (
    <div className="card screener-entry">
      <div className="section-header">FIND WALLETS TO COPY</div>
      <p className="muted">
        Research Polymarket wallets on 7-day, 30-day and 90-day statistics, with the fetched
        history coverage stated for every figure.
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
