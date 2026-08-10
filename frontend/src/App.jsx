import { useState, useEffect } from 'react'
import { api, clearWallet, getSession, saveSession } from './api'
import { bootstrapSession } from './authBootstrap'
import Onboarding from './pages/Onboarding'
import Home from './pages/Home'
import Positions from './pages/Positions'
import User from './pages/User'

const TABS = [
  ['home', 'HOME'],
  ['positions', 'POSITIONS'],
  ['user', 'USER'],
]

const tg = window.Telegram?.WebApp

export default function App() {
  const [session, setSession] = useState(() => getSession())
  // Always launch on HOME — don't restore whatever tab the hash held from a
  // previous session (owner request). Hash still tracks tab within a session.
  const [tab, setTab] = useState('home')
  // A cached address is not authentication. Validate its HttpOnly cookie before
  // rendering account pages; if it is stale, re-auth from Telegram initData.
  const [authChecking, setAuthChecking] = useState(Boolean(getSession() || tg?.initData))

  useEffect(() => {
    if (!tg) return
    tg.ready()
    tg.expand()
    try {
      tg.setHeaderColor('#eef2ef')
      tg.setBackgroundColor('#eef2ef')
    } catch {
      /* older Telegram clients */
    }
  }, [])

  useEffect(() => {
    if (!getSession() && !tg?.initData) return
    bootstrapSession({
      cachedSession: getSession(),
      initData: tg?.initData || '',
      api,
      saveSession,
      clearSession: clearWallet,
    })
      .then(setSession)
      .finally(() => setAuthChecking(false))
  }, [])

  useEffect(() => {
    window.location.hash = tab
  }, [tab])

  if (authChecking) {
    return (
      <div className="onboard">
        <div className="logo">&gt; POLYMARKET COPYBOT</div>
        <div className="muted">signing in with telegram…</div>
      </div>
    )
  }

  if (!session) return <Onboarding onDone={() => setSession(getSession())} />

  return (
    <div className="app">
      <header className="app-header">&gt; POLYMARKET COPYBOT</header>
      <div className="content">
        {tab === 'home' && <Home />}
        {tab === 'positions' && <Positions />}
        {tab === 'user' && <User onLogout={() => setSession(null)} />}
      </div>
      <nav className="tab-bar">
        {TABS.map(([k, label]) => (
          <button
            key={k}
            className={`tab-btn ${tab === k ? 'active' : ''}`}
            onClick={() => setTab(k)}
          >
            {label}
          </button>
        ))}
      </nav>
    </div>
  )
}
