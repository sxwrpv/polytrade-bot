import { useEffect, useState } from 'react'

const BOT_URL = 'https://t.me/cpolytrade_bot'

const Arrow = () => <span aria-hidden="true">↗</span>

const prefersReducedMotion = () =>
  typeof window !== 'undefined' &&
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

/* Reveal-on-scroll. The `js-reveal` root class gates the hidden state, so if
   this effect never runs the page renders fully visible instead of blank. */
function useReveal() {
  useEffect(() => {
    const root = document.documentElement
    root.classList.add('js-reveal')
    const targets = document.querySelectorAll('.reveal')
    if (prefersReducedMotion() || !('IntersectionObserver' in window)) {
      targets.forEach((el) => el.classList.add('in'))
      return () => root.classList.remove('js-reveal')
    }
    const observer = new IntersectionObserver(
      (entries) => entries.forEach((entry) => {
        if (!entry.isIntersecting) return
        entry.target.classList.add('in')
        observer.unobserve(entry.target)
      }),
      { threshold: 0.15, rootMargin: '0px 0px -10% 0px' },
    )
    targets.forEach((el) => observer.observe(el))
    return () => { observer.disconnect(); root.classList.remove('js-reveal') }
  }, [])
}

const PIPELINE = [
  { title: 'Leader trade detected', detail: 'OrderFilled log or activity poll', time: 'every 2s' },
  { title: 'Your limits checked', detail: 'ratio, caps, price band, exposure', time: 'one transaction' },
  { title: 'Order submitted', detail: 'market fill-or-kill', time: 'all or nothing' },
  { title: 'Recorded and alerted', detail: 'position stored, Telegram ping', time: 'then reconciled' },
]

export default function PublicHome() {
  useReveal()
  return (
    <div className="public-site">
      <header className="public-nav">
        <a className="public-brand" href="#top" aria-label="PolyTrade home">
          <span className="brand-mark">P</span><span>PolyTrade</span>
        </a>
        <a className="public-nav-link" href="#speed">Speed</a>
        <a className="public-nav-link" href="#how-it-works">How it works</a>
        <a className="btn public-small-cta" href={BOT_URL}>Open Telegram <Arrow /></a>
      </header>

      <main id="top">
        <section className="public-hero public-wrap">
          <div className="hero-copy reveal">
            <div className="eyebrow">BOT-FIRST COPY TRADING</div>
            <h1>Follow the market.<br /><span>Trade with intention.</span></h1>
            <p className="hero-lede">
              Choose Polymarket wallets to follow, set your own limits, and manage copy trading
              from one calm Telegram experience.
            </p>
            <div className="hero-actions">
              <a className="btn public-primary" href={BOT_URL}>Start in Telegram <Arrow /></a>
              <a className="public-text-link" href="#speed">See how fast it moves ↓</a>
            </div>
            <p className="hero-footnote">Uses real money. Copy trading involves loss risk and is not financial advice.</p>
          </div>
          <LiveDemo />
        </section>

        <section id="speed" className="public-wrap speed" aria-labelledby="speed-title">
          <div className="speed-head reveal">
            <div>
              <div className="eyebrow">BUILT FOR THE GAP</div>
              <h2 id="speed-title">Seconds, not minutes.</h2>
            </div>
            <p>
              The window between a leader's trade and yours is where copy trading is won or lost.
              These are the cadences the engine actually runs at — not marketing round numbers.
            </p>
          </div>
          <div className="speed-grid reveal">
            <article className="speed-card">
              <div className="speed-metric">2<em>s</em></div>
              <h3>Detection cadence</h3>
              <p>The fast path watches each followed wallet every two seconds, reading on-chain fill logs where available.</p>
            </article>
            <article className="speed-card">
              <div className="speed-metric">5<em>s</em></div>
              <h3>Reconcile pass</h3>
              <p>A second, slower loop re-compares every position against the wallet, repairing whatever the fast path missed.</p>
            </article>
            <article className="speed-card">
              <div className="speed-metric">FOK</div>
              <h3>All or nothing</h3>
              <p>Orders fill completely or not at all, under a signed price ceiling. No partial fills, no chasing a worse entry.</p>
            </article>
            <article className="speed-card">
              <div className="speed-metric">≤3</div>
              <h3>Retries, then stop</h3>
              <p>An unfilled intent retries three times and is abandoned. A missed copy beats a copy at the wrong price.</p>
            </article>
          </div>
        </section>

        <section id="how-it-works" className="public-wrap pipeline" aria-labelledby="pipeline-title">
          <div className="eyebrow">FROM THEIR TRADE TO YOURS</div>
          <h2 id="pipeline-title">Four steps, every time.</h2>
          <div className="pipeline-figure reveal">
            <PipelineDiagram />
          </div>
        </section>

        <section className="public-wrap benefits" aria-labelledby="benefits-title">
          <div className="reveal">
            <div className="eyebrow">WHY POLYTRADE</div>
            <h2 id="benefits-title">Control without the clutter.</h2>
          </div>
          <div className="benefit-grid">
            <article className="public-card reveal"><span className="benefit-icon" aria-hidden="true">◎</span><h3>You choose who to follow</h3><p>Explore wallet activity and decide which traders fit your approach.</p></article>
            <article className="public-card reveal" data-delay="1"><span className="benefit-icon" aria-hidden="true">⌁</span><h3>Your limits stay yours</h3><p>Set allocation and exposure controls rather than copying without boundaries.</p></article>
            <article className="public-card reveal" data-delay="2"><span className="benefit-icon" aria-hidden="true">◌</span><h3>Built around Telegram</h3><p>Launch, review, and manage the experience where you already communicate.</p></article>
          </div>
        </section>

        <section className="public-wrap features" aria-labelledby="features-title">
          <div className="eyebrow">INSIDE THE APP</div>
          <h2 id="features-title">See the signal,<br />not the noise.</h2>

          <div className="feature-row reveal">
            <div className="feature-copy">
              <h3>A screener that admits what it knows</h3>
              <p>Wallets are ranked on windowed statistics rebuilt from public history — and every card shows how much of that history was actually fetched.</p>
              <div className="feature-list">
                <span>7-day, 30-day and 90-day windows</span>
                <span>Coverage stated on the card, not hidden</span>
                <span>Reconstructed ratios kept behind advanced filters</span>
              </div>
            </div>
            <ScreenerPanel />
          </div>

          <div className="feature-row flip reveal">
            <div className="feature-copy">
              <h3>Limits enforced where it counts</h3>
              <p>Your settings are not a UI suggestion. They are re-read inside the database transaction that reserves your money, and checked again immediately before the order is sent.</p>
              <div className="feature-list">
                <span>Per-wallet ratio, position cap and price band</span>
                <span>Account-wide exposure ceiling</span>
                <span>Pause takes effect on in-flight orders</span>
              </div>
            </div>
            <RiskPanel />
          </div>

          <div className="feature-row reveal">
            <div className="feature-copy">
              <h3>Every state is visible</h3>
              <p>Open, closing, resolved, claimable, uncertain — positions are never quietly dropped. When a submission outcome is unknown, the wallet itself settles the question.</p>
              <div className="feature-list">
                <span>Live position and exposure view</span>
                <span>Persisted activity log with realised PnL</span>
                <span>Telegram alerts after confirmed persistence</span>
              </div>
            </div>
            <PositionsPanel />
          </div>
        </section>

        <section className="public-wrap know" aria-labelledby="know-title">
          <div className="reveal"><div className="eyebrow">A CALM CHECKPOINT</div><h2 id="know-title">Know before you fund.</h2></div>
          <div className="know-list reveal">
            <article><strong>Real-money risk</strong><p>Prediction-market positions can lose some or all of the money committed. Past wallet activity does not predict future results.</p></article>
            <article><strong>Custodial wallet</strong><p>PolyTrade creates and operates a wallet for you. Review the security model and protect access to your Telegram account.</p></article>
            <article><strong>Funding and withdrawals</strong><p>There is no in-app withdrawal or automatic redemption. Exiting and moving funds may require external tools and network fees.</p></article>
            <article><strong>Eligibility is your responsibility</strong><p>Use only where Polymarket and this service are legally available to you. Geographic and other restrictions may apply.</p></article>
          </div>
        </section>

        <section className="public-final">
          <div className="public-wrap final-inner reveal">
            <div><div className="eyebrow">READY TO EXPLORE?</div><h2>Start with the bot.<br />Move at your pace.</h2></div>
            <a className="btn public-primary" href={BOT_URL}>Open @cpolytrade_bot <Arrow /></a>
          </div>
        </section>
      </main>

      <footer className="public-footer public-wrap">
        <a className="public-brand" href="#top"><span className="brand-mark">P</span><span>PolyTrade</span></a>
        <p>Copy trading infrastructure for Polymarket. Not financial advice.</p>
        <div><a href="/docs">Documentation</a><a href="/docs/risk-and-security">Risk &amp; security</a><a href={BOT_URL}>Telegram</a></div>
        <small>© {new Date().getFullYear()} PolyTrade</small>
      </footer>
    </div>
  )
}

/* Hero demo: walks the copy pipeline on a loop. Illustrative only — it never
   shows market data, prices, or balances that could be read as a real quote. */
function LiveDemo() {
  const still = prefersReducedMotion()
  const [step, setStep] = useState(still ? PIPELINE.length : 0)

  useEffect(() => {
    if (still) return undefined
    const timer = setInterval(
      () => setStep((current) => (current >= PIPELINE.length + 1 ? 0 : current + 1)),
      1500,
    )
    return () => clearInterval(timer)
  }, [still])

  return (
    <div className="product-preview" aria-label="Illustrative product preview">
      <div className="preview-label">ILLUSTRATIVE — NOT LIVE MARKET DATA</div>
      <div className="demo-shell">
        <div className="demo-head">
          <span><b>P</b> PolyTrade</span>
          <i className="demo-live">COPY ENGINE</i>
        </div>
        <div className="demo-steps">
          {PIPELINE.map((item, index) => (
            <div
              key={item.title}
              className={`demo-step${index === step ? ' active' : ''}${index < step ? ' done' : ''}`}
            >
              <span className="demo-dot">{index < step ? '✓' : index + 1}</span>
              <span><b>{item.title}</b><small>{item.detail}</small></span>
              <span className="demo-time">{item.time}</span>
            </div>
          ))}
        </div>
        <div className={`demo-result${step > PIPELINE.length - 1 ? '' : ' pending'}`}>
          <small>RESULTING POSITION</small>
          <b>Example market · YES</b>
          <div className="demo-result-row">
            <span>SIZE <i>$15.00</i></span>
            <span>STATE <i>open</i></span>
          </div>
        </div>
      </div>
      <p className="demo-note">SIZES AND MARKETS SHOWN ARE EXAMPLES</p>
    </div>
  )
}

function PipelineDiagram() {
  const nodes = [
    { x: 20, w: 190, title: 'Leader trades', sub: 'wallet you follow' },
    { x: 270, w: 190, title: 'Limits checked', sub: 'reserved in the database' },
    { x: 520, w: 190, title: 'FOK order', sub: 'signed price ceiling' },
    { x: 770, w: 170, title: 'Your position', sub: 'recorded + alerted' },
  ]
  return (
    <svg viewBox="0 0 960 190" role="img" aria-labelledby="pipe-title pipe-desc">
      <title id="pipe-title">How a copied trade travels</title>
      <desc id="pipe-desc">
        A trade by the wallet you follow is detected, checked against your limits and reserved in
        the database, submitted as a fill-or-kill order, then recorded as your position.
      </desc>
      <defs>
        <marker id="pipe-arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
          <polygon points="0 0, 8 3, 0 6" fill="#0b9e63" />
        </marker>
      </defs>

      {[210, 460, 710].map((x, index) => (
        <line
          key={x} className={`pipe-line d${index + 1}`} style={{ '--len': 60 }}
          x1={x} y1="100" x2={x + 60} y2="100"
          stroke="#0b9e63" strokeWidth="1.4" markerEnd="url(#pipe-arrow)"
        />
      ))}

      {nodes.map((node, index) => (
        <g key={node.title} className={`pipe-node n${index + 1}`}>
          <rect
            x={node.x} y="62" width={node.w} height="76" rx="14"
            fill="rgba(255,255,255,.66)" stroke="rgba(20,32,26,.12)"
          />
          <text
            x={node.x + node.w / 2} y="96" textAnchor="middle"
            fill="#14201a" fontSize="14" fontWeight="600"
            fontFamily="Inter, system-ui, sans-serif"
          >{node.title}</text>
          <text
            x={node.x + node.w / 2} y="116" textAnchor="middle"
            fill="#5c6b62" fontSize="10"
            fontFamily="'JetBrains Mono', monospace"
          >{node.sub}</text>
        </g>
      ))}
    </svg>
  )
}

function ScreenerPanel() {
  return (
    <div className="panel">
      <p className="panel-label">WALLET SCREENER · ILLUSTRATIVE</p>
      <div className="panel-card">
        <div className="panel-top">
          <span className="avatar">0x</span>
          <b>0x7a…4c21</b>
          <span className="panel-chip">30D WINDOW</span>
        </div>
        <div className="panel-stats">
          <div><small>REALISED</small><strong className="pos">+$4.2k</strong></div>
          <div><small>TRADES</small><strong>128</strong></div>
          <div><small>COVERAGE</small><strong>94%</strong></div>
        </div>
      </div>
      <div className="panel-card">
        <div className="panel-top">
          <span className="avatar">0x</span>
          <b>0x19…8ef0</b>
          <span className="panel-chip muted">PARTIAL DATA</span>
        </div>
        <div className="panel-stats">
          <div><small>REALISED</small><strong className="pos">+$980</strong></div>
          <div><small>TRADES</small><strong>41</strong></div>
          <div><small>COVERAGE</small><strong>62%</strong></div>
        </div>
      </div>
    </div>
  )
}

function RiskPanel() {
  const rows = [
    ['Copy ratio', '1%', 12],
    ['Max per position', '$15', 34],
    ['Entry price band', '0.10–0.98', 88],
    ['Max slippage', '2%', 20],
  ]
  return (
    <div className="panel">
      <p className="panel-label">RISK CONTROLS · ILLUSTRATIVE</p>
      <div className="panel-card">
        <div className="slider-row">
          {rows.map(([label, value, pct]) => (
            <div key={label}>
              <label>{label}<b>{value}</b></label>
              <div className="slider-track">
                <div className="slider-fill" style={{ width: `${pct}%` }} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function PositionsPanel() {
  const rows = [
    ['Example market A', 'open', '$15.00', 'panel-chip'],
    ['Example market B', 'closing', '$8.40', 'panel-chip muted'],
    ['Example market C', 'resolved', '$12.00', 'panel-chip muted'],
  ]
  return (
    <div className="panel">
      <p className="panel-label">POSITIONS · ILLUSTRATIVE</p>
      {rows.map(([market, state, size, chip]) => (
        <div className="panel-card" key={market}>
          <div className="panel-top">
            <b>{market}</b>
            <span className={chip}>{state.toUpperCase()}</span>
          </div>
          <div className="panel-stats">
            <div><small>SIZE</small><strong>{size}</strong></div>
            <div><small>OUTCOME</small><strong>YES</strong></div>
            <div><small>STATE</small><strong>{state}</strong></div>
          </div>
        </div>
      ))}
    </div>
  )
}
