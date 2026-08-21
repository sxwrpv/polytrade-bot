import { useState, useEffect, useCallback } from 'react'
import { api, clearWallet, getSession, saveSession } from './api'
import { bootstrapLaunch } from './authBootstrap'
import PublicHome from './pages/PublicHome'
import Onboarding from './pages/Onboarding'
import LegacyLink from './pages/LegacyLink'
import Home from './pages/Home'
import Positions from './pages/Positions'
import User from './pages/User'

const TABS = [['home', 'HOME'], ['positions', 'POSITIONS'], ['user', 'USER']]
const tg = window.Telegram?.WebApp

export default function App() {
  const [launch, setLaunch] = useState({ mode: 'loading', session: getSession() })
  const [tab, setTab] = useState('home')
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!tg) return
    tg.ready()
    tg.expand()
    try {
      tg.setHeaderColor('#eef2ef')
      tg.setBackgroundColor('#eef2ef')
    } catch { /* older Telegram clients */ }
  }, [])

  useEffect(() => {
    let current = true
    bootstrapLaunch({
      cachedSession: getSession(),
      initData: tg?.initData || '',
      api,
      saveSession,
      clearSession: clearWallet,
    }).then((result) => { if (current) setLaunch(result) })
    return () => { current = false }
  }, [attempt])

  useEffect(() => {
    if (launch.mode === 'authenticated') window.location.hash = tab
  }, [tab, launch.mode])

  const finishOnboarding = useCallback((address) => {
    setLaunch({ mode: 'authenticated', session: { address } })
    setTab('home')
  }, [])

  if (launch.mode === 'loading') return <LaunchLoading />
  if (launch.mode === 'public') return <PublicHome />
  if (launch.mode === 'telegram-error') {
    return (
      <div className="onboard launch-state card">
        <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
        <div className="section-header">TELEGRAM SIGN-IN</div>
        <h1>We couldn’t verify this launch.</h1>
        <p className="muted">{launch.message} Return to @cpolytrade_bot and open the app again, or retry below.</p>
        <button className="btn" onClick={() => { setLaunch({ mode: 'loading', session: null }); setAttempt((n) => n + 1) }}>RETRY TELEGRAM SIGN-IN</button>
        <a className="btn btn-ghost center-link" href="https://t.me/cpolytrade_bot">RETURN TO BOT</a>
      </div>
    )
  }
  if (launch.mode === 'session-error') {
    return (
      <div className="onboard launch-state card" role="alert">
        <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
        <div className="section-header">SESSION CHECK</div>
        <h1>We couldn’t verify your session.</h1>
        <p className="muted">{launch.message}</p>
        <button className="btn" onClick={() => { setLaunch({ mode: 'loading', session: launch.session }); setAttempt((n) => n + 1) }}>RETRY SESSION CHECK</button>
      </div>
    )
  }
  if (launch.mode === 'telegram-onboarding') return <Onboarding onDone={finishOnboarding} />
  if (launch.mode === 'legacy-link') return (
    <LegacyLink initData={launch.initData} address={launch.session.address} onDone={finishOnboarding} />
  )

  return (
    <div className="app">
      <header className="app-header">POLYTRADE</header>
      <div className="content">
        {tab === 'home' && <Home />}
        {tab === 'positions' && <Positions />}
        {tab === 'user' && <User onLogout={() => setLaunch({ mode: 'public', session: null })} />}
      </div>
      <nav className="tab-bar" aria-label="Account navigation">
        {TABS.map(([key, label]) => <button key={key} className={`tab-btn ${tab === key ? 'active' : ''}`} aria-current={tab === key ? 'page' : undefined} onClick={() => setTab(key)}>{label}</button>)}
      </nav>
    </div>
  )
}

function LaunchLoading() {
  return <div className="onboard launch-loading" role="status" aria-live="polite"><div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div><div className="muted">Checking your session…</div></div>
}
