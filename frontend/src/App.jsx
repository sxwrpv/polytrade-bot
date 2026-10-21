import { Component, Suspense, lazy, useState, useEffect, useCallback } from 'react'
import { api, clearWallet, getSession, saveSession } from './api'
import { bootstrapLaunch } from './authBootstrap'
import Onboarding from './pages/Onboarding'
import LegacyLink from './pages/LegacyLink'
import Home from './pages/Home'
import Positions from './pages/Positions'
import User from './pages/User'
import Icon from './components/Icon'

const TABS = [['home', 'Home', 'home'], ['positions', 'Positions', 'positions'], ['user', 'Account', 'account']]
const tg = window.Telegram?.WebApp

// The public homepage is its own chunk, stylesheet included, so a Mini App
// launch inside Telegram never downloads it. Outside Telegram the visitor is
// almost always headed there, so the download starts now, alongside the
// session check.
const loadPublicHome = () => import('./pages/PublicHome')
const PublicHome = lazy(loadPublicHome)
if (!tg?.initData) loadPublicHome().catch(() => { /* the boundary below reports it */ })

/* A chunk that fails to download (a dropped connection, or a deploy that
   replaced it mid-visit) would otherwise unmount the app to a blank page. */
class ChunkBoundary extends Component {
  state = { failed: false }

  static getDerivedStateFromError() { return { failed: true } }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="onboard launch-state" role="alert">
        <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
        <span className="state-icon is-error"><Icon name="alert" /></span>
        <h1>This page didn’t load.</h1>
        <p className="muted">Check your connection and reload. You can also go straight to @cpolytrade_bot.</p>
        <div className="launch-actions">
          <button className="btn btn-block" onClick={() => window.location.reload()}>
            <Icon name="refresh" />Reload
          </button>
          <a className="btn btn-ghost btn-block" href="https://t.me/cpolytrade_bot">Open the bot</a>
        </div>
      </div>
    )
  }
}

function selectionHaptic() {
  try {
    // HapticFeedback arrived in Bot API 6.1; outside Telegram the SDK reports
    // 6.0 and only logs a warning, so skip it there.
    if (tg?.isVersionAtLeast?.('6.1')) tg.HapticFeedback.selectionChanged()
  } catch {
    /* older Telegram clients */
  }
}

export default function App() {
  const [launch, setLaunch] = useState({ mode: 'loading', session: getSession() })
  const [tab, setTab] = useState('home')
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!tg) return
    tg.ready()
    tg.expand()
    try {
      // Colour control arrived in Bot API 6.1; outside Telegram the SDK reports
      // 6.0 and only logs a warning on every page load, so skip it there.
      if (tg.isVersionAtLeast?.('6.1')) {
        tg.setHeaderColor('#eef2ef')
        tg.setBackgroundColor('#eef2ef')
      }
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

  const goTo = useCallback((key) => {
    selectionHaptic()
    // Tapping the tab you are on returns to its top, the way native tab bars do.
    window.scrollTo({ top: 0, behavior: key === tab ? 'smooth' : 'auto' })
    setTab(key)
  }, [tab])

  if (launch.mode === 'loading') return <LaunchLoading />
  if (launch.mode === 'public') {
    return (
      <ChunkBoundary>
        <Suspense fallback={<LaunchLoading />}><PublicHome /></Suspense>
      </ChunkBoundary>
    )
  }
  if (launch.mode === 'telegram-error') {
    return (
      <div className="onboard launch-state">
        <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
        <span className="state-icon is-error"><Icon name="alert" /></span>
        <h1>We couldn’t verify this launch.</h1>
        <p className="muted">{launch.message} Return to @cpolytrade_bot and open the app again, or retry below.</p>
        <div className="launch-actions">
          <button className="btn btn-block" onClick={() => { setLaunch({ mode: 'loading', session: null }); setAttempt((n) => n + 1) }}>
            <Icon name="refresh" />Retry Telegram sign-in
          </button>
          <a className="btn btn-ghost btn-block" href="https://t.me/cpolytrade_bot">Return to the bot</a>
        </div>
      </div>
    )
  }
  if (launch.mode === 'session-error') {
    return (
      <div className="onboard launch-state" role="alert">
        <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
        <span className="state-icon is-error"><Icon name="alert" /></span>
        <h1>We couldn’t verify your session.</h1>
        <p className="muted">{launch.message}</p>
        <div className="launch-actions">
          <button className="btn btn-block" onClick={() => { setLaunch({ mode: 'loading', session: launch.session }); setAttempt((n) => n + 1) }}>
            <Icon name="refresh" />Retry session check
          </button>
        </div>
      </div>
    )
  }
  if (launch.mode === 'telegram-onboarding') return <Onboarding onDone={finishOnboarding} />
  if (launch.mode === 'legacy-link') return (
    <LegacyLink initData={launch.initData} address={launch.session.address} onDone={finishOnboarding} />
  )

  const index = TABS.findIndex(([key]) => key === tab)
  return (
    <div className="app">
      <header className="app-header">
        <span className="app-brand">
          <img className="brand-logo" src="/brand/polytrade-mark.png" alt="" />
          <span>PolyTrade</span>
        </span>
        <span className="app-page">{TABS[index][1]}</span>
      </header>
      <main className="content" key={tab}>
        {tab === 'home' && <Home />}
        {tab === 'positions' && <Positions onNavigate={goTo} />}
        {tab === 'user' && <User onLogout={() => setLaunch({ mode: 'public', session: null })} />}
      </main>
      <nav className="tab-bar" aria-label="Account navigation" style={{ '--i': index }}>
        {TABS.map(([key, label, icon]) => (
          <button
            key={key}
            type="button"
            className={`tab-btn ${tab === key ? 'active' : ''}`}
            aria-current={tab === key ? 'page' : undefined}
            onClick={() => goTo(key)}
          >
            <Icon name={icon} />
            {label}
          </button>
        ))}
      </nav>
    </div>
  )
}

function LaunchLoading() {
  return (
    <div className="onboard launch-loading" role="status" aria-live="polite">
      <div className="logo"><img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /> PolyTrade</div>
      <span className="launch-progress" aria-hidden="true" />
      <div className="muted">Checking your session…</div>
    </div>
  )
}
