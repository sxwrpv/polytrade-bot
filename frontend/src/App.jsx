import { useState, useEffect } from 'react'
import { api, getSession, saveSession } from './api'
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
  const [session, setSession] = useState(getSession())
  const [tab, setTab] = useState(() => window.location.hash.replace('#', '') || 'home')
  // Inside Telegram with no stored session: try initData login before showing
  // onboarding — a returning Telegram user signs straight back in.
  const [tgAuthing, setTgAuthing] = useState(Boolean(!getSession() && tg?.initData))

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
    if (session || !tg?.initData) return
    api
      .telegramAuth(tg.initData)
      .then((r) => {
        if (r.address && r.api_token) {
          saveSession({ address: r.address, token: r.api_token })
          setSession(getSession())
        }
      })
      .catch(() => {})
      .finally(() => setTgAuthing(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    window.location.hash = tab
  }, [tab])

  if (tgAuthing) {
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
