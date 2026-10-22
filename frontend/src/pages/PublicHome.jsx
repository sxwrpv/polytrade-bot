import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import SiteSwitcher from '../components/SiteSwitcher'
import { redeemMinutes, useRedeemStatus } from '../redeemStatus'
// The homepage's own styles travel in this lazily loaded chunk, so a Mini App
// launch inside Telegram never downloads them.
import '../styles/public-landing.css'

const BOT_URL = 'https://t.me/cpolytrade_bot'

const Arrow = () => <span aria-hidden="true">↗</span>

const prefersReducedMotion = () =>
  typeof window !== 'undefined' &&
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

/* Reveal-on-scroll. The `js-reveal` root class gates the hidden state, so if
   this effect never runs the page still renders fully visible.
   Children marked `.rise` stagger in by their `--i` once their section is in. */
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

/** True once the element has been on screen (at once under reduced motion),
 *  for pieces whose animation starts from a React value (useReveal covers
 *  the class-driven ones). */
function useInView(ref, threshold = 0.3) {
  const [seen, setSeen] = useState(false)
  useEffect(() => {
    const node = ref.current
    if (!node) return undefined
    if (prefersReducedMotion() || !('IntersectionObserver' in window)) { setSeen(true); return undefined }
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return
      setSeen(true)
      observer.disconnect()
    }, { threshold })
    observer.observe(node)
    return () => observer.disconnect()
  }, [ref, threshold])
  return seen
}

/* Scroll plays the cadence timeline: a playhead sweeps fifteen seconds as the
   figure crosses the screen, and each event appears when the playhead reaches
   it. Without motion (or without this script) every event is simply shown. */
function useTimelineScrub(ref) {
  useEffect(() => {
    const figure = ref.current
    if (!figure || prefersReducedMotion()) return undefined
    const events = [...figure.querySelectorAll('[data-t]')].map((node) => [node, Number(node.dataset.t)])
    const heads = [...figure.querySelectorAll('.cad-playhead')]
    figure.classList.add('is-scrub')
    let raf = 0
    const update = () => {
      raf = 0
      const vh = window.innerHeight
      const rect = figure.getBoundingClientRect()
      // From the figure's top reaching 85% of the screen to its bottom
      // reaching 55%: one comfortable scroll plays the whole timeline.
      const p = Math.min(1, Math.max(0, (vh * 0.85 - rect.top) / (vh * 0.3 + rect.height)))
      const t = p * 15.4
      for (const [node, at] of events) node.classList.toggle('on', at <= t)
      for (const head of heads) {
        const tall = head.dataset.axis === 'y'
        head.setAttribute('transform', tall ? `translate(0 ${(t * 30).toFixed(1)})` : `translate(${(t * 48).toFixed(1)} 0)`)
        head.querySelector('text').textContent = `${Math.min(15, t).toFixed(1)}s`
      }
      figure.classList.toggle('is-done', p >= 1)
    }
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(update) }
    update()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
      figure.classList.remove('is-scrub')
    }
  }, [ref])
}

const PIPELINE = [
  { title: 'Leader trade detected', detail: 'OrderFilled log or activity poll', time: 'every 2s' },
  { title: 'Your limits checked', detail: 'ratio, caps, price band, exposure', time: 'one transaction' },
  { title: 'Order submitted', detail: 'market fill-or-kill', time: 'all or nothing' },
  { title: 'Recorded and alerted', detail: 'position stored, Telegram ping', time: 'then reconciled' },
]

/* Simplified from System Design diagram 04 (risk surfaces) for a consumer
   audience: four of its six enforcement layers, in order.
   Every line is a real control; nothing here is a live reading. */
const GATES = [
  { tag: 'SETTINGS', title: 'You set the limits', detail: 'Copy ratio, max per position, price band and exposure caps, for each wallet you follow.' },
  { tag: 'RESERVE', title: 'Checked when reserved', detail: 'Re-read inside the database transaction that reserves your money.' },
  { tag: 'SUBMIT', title: 'Re-checked at submit', detail: 'Read again just before the order is signed, so a limit you tightened still applies to an order already in flight.' },
  { tag: 'ORDER', title: 'Bounded in the order', detail: 'The price ceiling is encoded into the signed order itself, so the exchange cannot fill it above your band.' },
]

/* The Wallet Screener's research cohort, counted by verdict. Real figures from
   the screener's dated snapshot (trader-screener/data/dataset.json, window
   anchored 25 Aug 2026); the wording is the screener's own (screenerModel
   CLASS_DEF). Refresh both together if the snapshot is regenerated. */
const COHORT = {
  asOf: '25 Aug 2026',
  rows: [
    ['Proven', 'proven', 114, 'Copying them has worked on their finished trades.'],
    ['Ahead', 'ahead', 461, 'They finish just ahead of what copying costs.'],
    ['Caution', 'caution', 1266, 'They make money, and copying costs take all of it.'],
    ['Losing', 'losing', 1084, 'Losing on their own trades, before copying costs start.'],
    ['Not comparable', 'other', 386, 'They close out early, so they are not ranked against holders.'],
    ['Not scored', 'other', 455, 'No verdict yet: the research has no score for them.'],
  ],
}
COHORT.total = COHORT.rows.reduce((sum, [, , n]) => sum + n, 0)

/* The product, screen by screen: real Mini App and screener screens captured
   from the built app with a sample account (frontend/public/showcase). */
const SHOWCASE = [
  {
    img: 'copy-wallet', step: 'COPY', title: 'Paste a wallet, set its limits',
    body: 'An address or a Polymarket profile link, then this wallet’s copy ratio and caps. Copying starts with their next trade, and positions they already hold are left alone.',
    alt: 'The Copy a wallet screen: a wallet address field, then copy ratio and max per trade sliders.',
  },
  {
    img: 'positions', step: 'POSITIONS', title: 'Every copy, with its numbers',
    body: 'Entry, live price and shares for each copied position, and the one action it allows.',
    alt: 'The Positions screen: open exposure and unrealised P&L above a list of copied positions.',
  },
  {
    img: 'activity', step: 'ACTIVITY', title: 'A log of what happened',
    body: 'Opened, resized, closed and resolved, each written down after it is confirmed.',
    alt: 'The Activity screen: opened, resized, closed and resolved events with their amounts.',
  },
  {
    img: 'wallet-limits', step: 'CONTROL', title: 'Pause or tighten, per wallet',
    body: 'Stop new buys from one wallet without touching what is open, or change its limits at any time.',
    alt: 'A copied wallet’s settings: a copying switch and its position sizing sliders.',
  },
  {
    img: 'screener-filters', step: 'RESEARCH', title: 'Research before you copy',
    body: 'The Wallet Screener shows where every public wallet sits before you set a single threshold.',
    alt: 'The Wallet Screener filters: a histogram of wallets above each threshold slider.',
  },
]

/* Position states as the Mini App names them. */
const LIFECYCLE = [
  {
    states: [['Open', 'pos'], ['Closing', 'warn'], ['Closed', 'quiet']],
    note: 'A sale in flight shows as closing. If it stalls, the wallet decides: shares still held reopen it, shares gone close it.',
  },
  {
    states: [['Open', 'pos'], ['Resolved', 'pos']],
    note: 'Settled markets say so. Winnings are redeemed on polymarket.com; there is nothing to sell.',
    noteAuto: 'Settled markets say so. Winnings are redeemed to your balance automatically; there is nothing to sell.',
  },
  {
    states: [['Uncertain', 'warn']],
    note: 'An order whose outcome is unknown stays flagged until the wallet itself settles the question.',
  },
]

export default function PublicHome() {
  useReveal()
  const redeem = useRedeemStatus()
  const cadence = useRef(null)
  useTimelineScrub(cadence)
  return (
    <div className="public-site">
      <header className="public-nav">
        <a className="public-brand" href="#top" aria-label="PolyTrade home">
          <img className="brand-logo" src="/brand/polytrade-mark.png" alt="" /><span>PolyTrade</span>
        </a>
        <SiteSwitcher active="home" />
        <div className="public-nav-actions">
          <a className="public-nav-link" href="/docs/system-design">System Design</a>
          <a className="btn public-small-cta" href={BOT_URL}>Open Telegram <Arrow /></a>
        </div>
      </header>

      <main id="top">
        <div className="hero-stage">
          <HeroField />
          <section className="public-hero public-wrap" aria-labelledby="hero-title">
            <div className="hero-copy">
              <div className="eyebrow hero-in" style={{ '--i': 0 }}>BOT-FIRST COPY TRADING</div>
              <h1 id="hero-title">
                <span className="hero-line"><Words text="Follow the market." from={1} /></span>
                <span className="hero-line is-accent"><Words text="Trade with intention." from={4} /></span>
              </h1>
              <p className="hero-lede hero-in" style={{ '--i': 6 }}>
                Choose Polymarket wallets to follow, set your own limits, and manage copy trading
                from one calm Telegram experience.
              </p>
              <div className="hero-actions hero-in" style={{ '--i': 7 }}>
                <a className="btn public-primary" href={BOT_URL}>Start in Telegram <Arrow /></a>
                <a className="public-text-link" href="#speed">See how fast it moves ↓</a>
              </div>
              <p className="hero-footnote hero-in" style={{ '--i': 8 }}>
                Uses real money. Copy trading involves loss risk and is not financial advice.
              </p>
            </div>
            <Sizer />
          </section>
        </div>

        <Showcase />

        <section id="speed" className="public-wrap speed" aria-labelledby="speed-title">
          <div className="speed-head reveal">
            <div className="rise" style={{ '--i': 0 }}>
              <div className="eyebrow">BUILT FOR THE GAP</div>
              <h2 id="speed-title">Checked every two seconds.</h2>
            </div>
            <p className="rise" style={{ '--i': 1 }}>
              The window between a leader's trade and yours is where copy trading is won or lost.
              These are the intervals the engine runs at in production.
            </p>
          </div>
          <figure ref={cadence} className="cadence-figure reveal">
            <CadenceWide />
            <CadenceTall />
          </figure>
          <dl className="cadence-facts reveal">
            <div className="rise" style={{ '--i': 0 }}>
              <dt><span className="speed-metric">2<em>s</em></span>Detection cadence</dt>
              <dd>Each wallet you follow is checked every two seconds, reading on-chain fill logs where available. How soon a trade appears depends on Polymarket's data.</dd>
            </div>
            <div className="rise" style={{ '--i': 1 }}>
              <dt><span className="speed-metric">5<em>s</em></span>Reconcile pass</dt>
              <dd>A second, slower loop re-compares every position against the wallet, repairing whatever the fast path missed.</dd>
            </div>
            <div className="rise" style={{ '--i': 2 }}>
              <dt><span className="speed-metric">FOK</span>All or nothing</dt>
              <dd>Orders fill completely or not at all, under a signed price ceiling.</dd>
            </div>
            <div className="rise" style={{ '--i': 3 }}>
              <dt><span className="speed-metric">3</span>Tries, then stop</dt>
              <dd>An order that does not fill is tried at most three times, then dropped, which keeps every copy inside your price band.</dd>
            </div>
          </dl>
        </section>

        <section id="how-it-works" className="public-wrap pipeline" aria-labelledby="pipeline-title">
          <div className="reveal">
            <div className="eyebrow rise" style={{ '--i': 0 }}>FROM THEIR TRADE TO YOURS</div>
            <h2 id="pipeline-title" className="rise" style={{ '--i': 1 }}>Four steps, every time.</h2>
          </div>
          <ol className="flow reveal">
            {PIPELINE.map((step, index) => (
              <li key={step.title} className="flow-step" style={{ '--i': index }}>
                <span className="flow-node" aria-hidden="true" />
                <b>{step.title}</b>
                <span className="flow-detail">{step.detail}</span>
                <span className="flow-time">{step.time}</span>
              </li>
            ))}
          </ol>
          <div className="architecture-link reveal">
            <div>
              <strong>See how the whole system fits together.</strong>
              <p>Explore seven architecture diagrams covering topology, onboarding, execution, risk controls, position lifecycle, the screener pipeline and production workflow.</p>
            </div>
            <a className="btn btn-ghost" href="/docs/system-design">Explore full System Design <Arrow /></a>
          </div>
        </section>

        <section className="public-wrap benefits" aria-labelledby="benefits-title">
          <div className="benefits-head reveal">
            <div className="eyebrow rise" style={{ '--i': 0 }}>WHERE YOUR LIMITS APPLY</div>
            <h2 id="benefits-title" className="rise" style={{ '--i': 1 }}>Your limits apply at every step.</h2>
            <p className="benefits-lede rise" style={{ '--i': 2 }}>
              Your settings are re-read inside the database transaction that reserves your money,
              and checked once more immediately before the order is signed.
            </p>
            <p className="benefits-footnote rise" style={{ '--i': 3 }}>
              Simplified from the full enforcement diagram in the{' '}
              <a href="/docs/system-design">System Design documentation</a>.
            </p>
          </div>
          <div className="gates-figure reveal">
            <ol className="gates">
              {GATES.map((gate, index) => (
                <li key={gate.tag} className="gate" style={{ '--i': index }}>
                  <span className="gate-tag">{gate.tag}</span>
                  <div>
                    <b>{gate.title}</b>
                    <p>{gate.detail}</p>
                  </div>
                </li>
              ))}
            </ol>
            <p className="gates-caption">IN ORDER · EACH POINT CAN STOP THE COPY</p>
          </div>
        </section>

        <section id="wallet-screener" className="screener-spotlight" aria-labelledby="wallet-screener-title">
          <div className="public-wrap screener-spotlight-grid">
            <div className="feature-copy reveal">
              <div className="eyebrow rise" style={{ '--i': 0 }}>PUBLIC WALLET RESEARCH</div>
              <h2 id="wallet-screener-title" className="rise" style={{ '--i': 1 }}>Find the signal before you copy.</h2>
              <p className="rise" style={{ '--i': 2 }}>
                Explore public history from Polymarket wallets in a dedicated research view. Compare
                7-day, 30-day and all-time results, filter the board, and see whose record survives
                the cost of being copied.
              </p>
              <div className="feature-list rise" style={{ '--i': 3 }}>
                <span>No account or Telegram login required</span>
                <span>Missing data is marked as missing</span>
                <span>Past results do not predict future ones</span>
              </div>
              <a className="btn public-primary screener-spotlight-cta rise" style={{ '--i': 4 }} href="/screener">
                Open Wallet Screener <Arrow />
              </a>
            </div>
            <div className="reveal">
              <CohortChart />
              <p className="screener-spotlight-note">
                Read-only public data. Wallet statistics can be incomplete or delayed and are not financial advice.
              </p>
            </div>
          </div>
        </section>

        <section className="public-wrap features" aria-labelledby="features-title">
          <div className="reveal">
            <div className="eyebrow rise" style={{ '--i': 0 }}>POSITION MANAGEMENT</div>
            <h2 id="features-title" className="rise" style={{ '--i': 1 }}>Every state stays visible.</h2>
          </div>
          <div className="feature-row">
            <Lifecycle />
            <div className="feature-copy reveal">
              <h3 className="rise" style={{ '--i': 0 }}>Every position stays on the list</h3>
              <p className="rise" style={{ '--i': 1 }}>Open, closing, resolved, claimable and uncertain positions all stay on the list. When a submission outcome is unknown, the wallet itself settles the question.</p>
              <div className="feature-list rise" style={{ '--i': 2 }}>
                <span>Live position and exposure view</span>
                <span>Persisted activity log with realised PnL</span>
                <span>Telegram alerts after confirmed persistence</span>
              </div>
            </div>
          </div>
        </section>

        <section className="public-wrap know" aria-labelledby="know-title">
          <div className="reveal"><div className="eyebrow">A CALM CHECKPOINT</div><h2 id="know-title">Know before you fund.</h2></div>
          <div className="know-list reveal">
            <article className="rise" style={{ '--i': 0 }}><strong>Real-money risk</strong><p>Prediction-market positions can lose some or all of the money committed. Past wallet activity does not predict future results.</p></article>
            <article className="rise" style={{ '--i': 1 }}><strong>Custodial wallet</strong><p>PolyTrade creates and operates a wallet for you. Review the security model and protect access to your Telegram account.</p></article>
            <article className="rise" style={{ '--i': 2 }}><strong>Funding and withdrawals</strong><p>{redeem.auto
              ? `There is no in-app withdrawal. Resolved positions are redeemed automatically about every ${redeemMinutes(redeem)} minutes. Exiting and moving funds may require external tools and network fees.`
              : 'There is no in-app withdrawal or automatic redemption. Exiting and moving funds may require external tools and network fees.'}</p></article>
            <article className="rise" style={{ '--i': 3 }}><strong>Eligibility is your responsibility</strong><p>Use only where Polymarket and this service are legally available to you. Geographic and other restrictions may apply.</p></article>
          </div>
        </section>

        <section className="public-final" aria-labelledby="final-title">
          <div className="public-wrap final-inner reveal">
            <div className="rise" style={{ '--i': 0 }}><div className="eyebrow">READY TO EXPLORE?</div><h2 id="final-title">Start with the bot.<br />Move at your pace.</h2></div>
            {/* The destination, shown the way Telegram shows it: the bot's
                avatar, name and handle, so the hand-off is recognisable. */}
            <div className="bot-card rise" style={{ '--i': 1 }}>
              <img className="bot-card-avatar" src="/brand/polytrade-mark.png" alt="" />
              <div className="bot-card-id">
                <b>PolyTrade</b>
                <span>@cpolytrade_bot · Telegram bot</span>
              </div>
              <p className="bot-card-copy">Create your wallet, fund it and choose the wallets you copy, all inside Telegram.</p>
              <a className="btn public-primary" href={BOT_URL}>Open @cpolytrade_bot <Arrow /></a>
            </div>
          </div>
        </section>
      </main>

      <footer className="public-footer">
        <div className="public-wrap footer-top">
          <p>Copy trading infrastructure for Polymarket. Not financial advice.</p>
          <nav className="footer-links" aria-label="Footer">
            <a href="/screener">Wallet Screener</a>
            <a href="/docs">Documentation</a>
            <a href="/docs/system-design">System Design</a>
            <a href="/docs/risk-and-security">Risk &amp; security</a>
            <a href={BOT_URL}>Telegram</a>
          </nav>
        </div>
        {/* The name returns at full width as the page's close. */}
        <div className="public-wrap footer-mark reveal">
          <a className="footer-wordmark rise" href="#top" aria-label="PolyTrade, back to top">
            <img src="/brand/polytrade-mark.png" alt="" /><span>PolyTrade</span>
          </a>
        </div>
        <small className="public-wrap footer-legal">© {new Date().getFullYear()} PolyTrade · Real money, real risk.</small>
      </footer>
    </div>
  )
}

/* The headline rises word by word from a mask (the kit's split-text). The
   words stay real text inside the real heading, so it reads as one sentence. */
function Words({ text, from }) {
  const words = text.split(' ')
  return words.map((word, index) => (
    <Fragment key={word}>
      <span className="hero-word" style={{ '--i': from + index }}>{word}</span>
      {index < words.length - 1 && ' '}
    </Fragment>
  ))
}

/* An odometer (the kit's number roll), built from React elements so every
   custom property is set through the CSSOM: the CSP forbids inline style
   attributes. The 0-9 strip is drawn by the stylesheet, so the page's text
   holds only the real value. Cells are keyed from the right, so units stay
   units when the number grows a digit and only the changed digits travel. */
function Roll({ text, className = '' }) {
  const chars = [...text]
  return (
    <span className={`roll ${className}`}>
      <span className="sr-only">{text}</span>
      <span className="roll-row" aria-hidden="true">
        {chars.map((char, index) => {
          const place = chars.length - index
          return /\d/.test(char) ? (
            <span key={`d${place}`} className="roll-d" style={{ '--n': char }}>
              <span className="roll-strip" />
            </span>
          ) : (
            <span key={`c${place}${char}`} className="roll-c">{char}</span>
          )
        })}
      </span>
    </span>
  )
}

/* A label whose width changes eases to its new size (the kit's multi-state
   badge). */
function useWidthMorph(ref, key) {
  const last = useRef(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const to = el.getBoundingClientRect().width
    const from = last.current
    last.current = to
    if (!from || Math.abs(from - to) < 1 || prefersReducedMotion() || !el.animate) return
    el.animate(
      [{ width: `${from}px` }, { width: `${to}px` }],
      { duration: 420, easing: 'cubic-bezier(.2, .9, .25, 1.1)' },
    )
  }, [ref, key])
}

// What the wallet you follow holds in one market, and your max per position.
// The app's own range for that cap ends at $500 (routes_traders.py, the Mini
// App's slider), so the presets stop there: a bigger order would be a claim
// the product cannot keep.
const LEADER_POSITIONS = [40_000, 120_000, 1_500_000]
const CAPS = [100, 250, 500]
const MIN_COPY = 2          // the app's default "Min copy size"
const EXAMPLE_PRICE = 0.42  // an example YES price inside the default 10¢–98¢ band

const usd = (value, digits = 2) => `$${value.toLocaleString('en-US', {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
})}`
/** $40K, $1.5M: preset buttons are narrow, the math line spells it out. */
const shortUsd = (value) => (value >= 1e6 ? `$${+(value / 1e6).toFixed(1)}M`
  : value >= 1e3 ? `$${+(value / 1e3).toFixed(1)}K` : usd(value, 0))

/* The engine's opening rule (backend/core/copy_engine.py, plan_actions): the
   copy is their position × your ratio, capped by your max per position, and
   skipped when it comes out under your minimum copy size. Available balance
   and exposure caps can only lower it further; the example leaves them out. */
function sizeCopy(position, ratioPct, cap) {
  const raw = (position * ratioPct) / 100
  const order = Math.min(raw, cap)
  if (order < MIN_COPY) return { raw, order: 0, state: 'skipped' }
  return { raw, order, state: raw > cap ? 'capped' : 'copied' }
}

const OUTCOME = {
  copied: ['pos', () => 'Copied in full'],
  capped: ['warn', (cap) => `Capped at your ${usd(cap, 0)} max`],
  skipped: ['quiet', () => `Skipped · under ${usd(MIN_COPY, 0)}`],
}

/* Hero: the copy-sizing rule as something to try, in place of a looping
   animation. The controls reuse the Mini App's own slider and segmented
   control, with its labels and hints, so this is what the app looks like. */
function Sizer() {
  const [leader, setLeader] = useState(1)
  const [ratio, setRatio] = useState(1)
  const [cap, setCap] = useState(2)
  // First paint shows a flat curve and $0.00; the real figures arrive as the
  // headline lands, so the number rolls and the curve rises once.
  const [live, setLive] = useState(false)
  const badge = useRef(null)

  useEffect(() => {
    if (prefersReducedMotion()) { setLive(true); return undefined }
    const timer = setTimeout(() => setLive(true), 700)
    return () => clearTimeout(timer)
  }, [])

  const position = LEADER_POSITIONS[leader]
  const max = CAPS[cap]
  const result = sizeCopy(position, ratio, max)
  const [tone, label] = OUTCOME[result.state]
  useWidthMorph(badge, result.state + max)

  const math = `${usd(position, 0)} × ${ratio.toFixed(1)}% = ${usd(result.raw)}`
  const verdict = {
    copied: 'copied in full.',
    capped: `capped at ${usd(max)}.`,
    skipped: `under the ${usd(MIN_COPY, 0)} minimum, so no order.`,
  }[result.state]

  return (
    <aside className="sizer hero-in" style={{ '--i': 3 }} aria-labelledby="sizer-title">
      <div className="sizer-head">
        <span className="sizer-brand">
          <img className="brand-logo" src="/brand/polytrade-mark.png" alt="" />
          <b id="sizer-title">Size a copy</b>
        </span>
        <span className="sizer-tag">ILLUSTRATIVE</span>
      </div>

      <div className="sizer-controls">
        <div className="sizer-field">
          <div className="setting-head"><span id="sz-leader">Their position</span></div>
          <div className="seg seg-sm" role="group" aria-labelledby="sz-leader" style={{ '--n': 3, '--i': leader }}>
            {LEADER_POSITIONS.map((value, index) => (
              <button key={value} type="button" aria-pressed={index === leader} onClick={() => setLeader(index)}>
                {shortUsd(value)}
              </button>
            ))}
          </div>
        </div>
        <div className="sizer-field">
          <div className="setting-head"><span id="sz-cap">Max per position</span></div>
          <div className="seg seg-sm" role="group" aria-labelledby="sz-cap" style={{ '--n': 3, '--i': cap }}>
            {CAPS.map((value, index) => (
              <button key={value} type="button" aria-pressed={index === cap} onClick={() => setCap(index)}>
                {usd(value, 0)}
              </button>
            ))}
          </div>
        </div>
        <div className="sizer-field is-wide">
          <div className="setting-head">
            <label htmlFor="sz-ratio">Copy ratio</label>
            <Roll text={`${ratio.toFixed(1)}%`} className="setting-val" />
          </div>
          <div className="slider-row">
            <input
              id="sz-ratio"
              type="range"
              min="0.5"
              max="20"
              step="0.5"
              value={ratio}
              style={{ '--fill': `${((ratio - 0.5) / 19.5) * 100}%` }}
              aria-describedby="sz-ratio-hint"
              aria-valuetext={`${ratio.toFixed(1)} percent`}
              onChange={(event) => setRatio(Number(event.target.value))}
            />
          </div>
          <span className="setting-hint" id="sz-ratio-hint">Your copy = their position × this %.</span>
        </div>
      </div>

      <div className={`sizer-out is-${result.state}`}>
        <div className="sizer-out-head">
          <span>Your order</span>
          <span ref={badge} className={`badge sizer-badge ${tone}`}>{label(max)}</span>
        </div>
        <Roll text={usd(live ? result.order : 0)} className="sizer-amount" />
        <SizerChart position={position} ratio={ratio} cap={max} live={live} />
        <p className="sizer-math" aria-live="polite" aria-atomic="true">
          {math} — {verdict}
        </p>
        <p className="demo-time">
          {result.state === 'skipped'
            ? 'No order is sent.'
            : `≈ ${(result.order / EXAMPLE_PRICE).toLocaleString('en-US', { maximumFractionDigits: 0 })} YES shares at an example 42¢, as a fill-or-kill order.`}
        </p>
      </div>
      <p className="demo-note">Example figures. The app’s max per position goes up to $500, and real copies also stay within your balance and exposure caps.</p>
    </aside>
  )
}

/* The rule as a curve: your order for every size of their position, from $100
   to $10M on a log scale, with the example above marked. Under the minimum it
   is skipped, then it rises with your ratio, then it flattens at your cap. A
   control change morphs the curve from its previous shape (the kit's chart
   rule for data changes). */
const CURVE_STEPS = 96
const CURVE_FLOOR = 38   // baseline, in viewBox units (0..40, y down)
const CURVE_SPAN = 33    // how far above the baseline the cap sits
const logX = (value) => ((Math.log10(value) - 2) / 5) * 100   // $100 → 0, $10M → 100
const clampPct = (v) => Math.min(100, Math.max(0, v))

function curvePoints(ratio, cap, flat) {
  return Array.from({ length: CURVE_STEPS + 1 }, (_, i) => {
    const order = flat ? 0 : sizeCopy(10 ** (2 + (5 * i) / CURVE_STEPS), ratio, cap).order
    return [(i / CURVE_STEPS) * 100, CURVE_FLOOR - (order / cap) * CURVE_SPAN]
  })
}
const curvePath = (points) => points.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`).join('')

function SizerChart({ position, ratio, cap, live }) {
  const path = useRef(null)
  const shown = useRef(null)
  const target = useMemo(() => curvePoints(ratio, cap, !live), [ratio, cap, live])

  useEffect(() => {
    const node = path.current
    const from = shown.current
    if (!node) return undefined
    if (!from || prefersReducedMotion()) {
      shown.current = target
      node.setAttribute('d', curvePath(target))
      return undefined
    }
    let raf = 0
    const start = performance.now()
    const step = (now) => {
      const t = Math.min(1, (now - start) / 480)
      const k = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2
      shown.current = target.map(([x, y], i) => [x, from[i][1] + (y - from[i][1]) * k])
      node.setAttribute('d', curvePath(shown.current))
      if (t < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target])

  const skipAt = clampPct(logX(MIN_COPY / (ratio / 100)))
  const capAt = clampPct(logX(cap / (ratio / 100)))
  const { order } = sizeCopy(position, ratio, cap)
  const markX = logX(position)
  const markY = ((CURVE_FLOOR - (live ? order / cap : 0) * CURVE_SPAN) / 40) * 100
  const desc = `Their position on a log scale from $100 to $10M. Under ${usd(MIN_COPY / (ratio / 100), 0)} `
    + `the copy is skipped; up to ${usd(cap / (ratio / 100), 0)} it is ${ratio.toFixed(1)}% of their position; `
    + `above that it is capped at ${usd(cap, 0)}. The marked point is the example above.`
  return (
    <figure className="sizer-chart">
      <div className="sc-plot">
        <svg viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-labelledby="sc-title sc-desc">
          <title id="sc-title">Your order for every size of their position</title>
          <desc id="sc-desc">{desc}</desc>
          <rect className="sc-skip" x="0" y="0" width={skipAt} height="40" />
          <rect className="sc-cap" x={capAt} y="0" width={100 - capAt} height="40" />
          <path className="sc-base" d={`M0 ${CURVE_FLOOR} H100`} vectorEffect="non-scaling-stroke" />
          <path ref={path} className="sc-curve" vectorEffect="non-scaling-stroke" />
        </svg>
        <span className="sc-mark" style={{ '--x': `${markX}%`, '--y': `${markY}%` }} aria-hidden="true" />
        {skipAt > 14 && <span className="sc-tag" style={{ '--x': `${skipAt / 2}%` }} aria-hidden="true">skipped</span>}
        {100 - capAt > 14 && <span className="sc-tag is-cap" style={{ '--x': `${(capAt + 100) / 2}%` }} aria-hidden="true">capped</span>}
      </div>
      <div className="sc-axis" aria-hidden="true">
        {['$100', '$1K', '$10K', '$100K', '$1M', '$10M'].map((tick) => <span key={tick}>{tick}</span>)}
      </div>
    </figure>
  )
}

/* The cadence timeline. Fifteen seconds, four lanes, drawn twice: wide for
   desktop and tall for phones, so the labels keep a legible size at every
   width. Positions are illustrative; the
   cadences (2s, 5s) and the three-try budget are the engine's own. */
const CADENCE_DESC = 'Fifteen seconds in four lanes. The wallet you follow trades. '
  + 'Detection checks every two seconds, and the first check that shows the trade sends your '
  + 'order at once as fill-or-kill: it fills completely or not at all. If it does not fill, it '
  + 'is tried again on the following reconcile passes, which run every five seconds, three '
  + 'tries at most, and then the copy is dropped. Positions on the timeline are illustrative: '
  + "how soon a trade appears depends on Polymarket's data."

const tx = (t) => 240 + 48 * t
const ty = (t) => 104 + 30 * t
const DETECT_TICKS = [0, 2, 4, 6, 8, 10, 12, 14]
const RECONCILE_TICKS = [0, 5, 10, 15]
const WIDE_LANES = [
  ['Wallet you follow', 'trades any time', 60],
  ['Detection', 'every 2 seconds', 136],
  ['Your order', 'fill-or-kill', 212],
  ['Reconcile', 'every 5 seconds', 288],
]
const TALL_LANES = [
  ['Wallet', 'you follow', 58],
  ['Detection', 'every 2s', 138],
  ['Your order', 'fill-or-kill', 222],
  ['Reconcile', 'every 5s', 310],
]

function CadenceWide() {
  return (
    <svg className="cadence cadence-wide" viewBox="0 22 1000 326" role="img" aria-labelledby="cad-wide-title cad-wide-desc">
      <title id="cad-wide-title">The first fifteen seconds of a copy</title>
      <desc id="cad-wide-desc">{CADENCE_DESC}</desc>

      {WIDE_LANES.map(([name, cadence, y], index) => (
        <g key={name}>
          <text className="cad-name" x="0" y={y - 3}>{name}</text>
          <text className="cad-cadence" x="0" y={y + 16}>{cadence}</text>
          <path className="cad-track" d={`M240 ${y} H960`} pathLength="1" style={{ '--d': index }} />
        </g>
      ))}

      <path className="cad-axis" d="M240 318 H960" />
      {Array.from({ length: 16 }, (_, t) => (
        <path key={t} className="cad-axis" d={`M${tx(t)} 318 V${t % 5 ? 322 : 326}`} />
      ))}
      {RECONCILE_TICKS.map((t) => (
        <text key={t} className="cad-cadence" x={tx(t)} y="342" textAnchor={t === 15 ? 'end' : t ? 'middle' : 'start'}>{t}s</text>
      ))}
      <g className="cad-playhead" data-axis="x">
        <path d="M240 32 V318" />
        <text x="240" y="28" textAnchor="middle">0.0s</text>
      </g>

      <g data-t="0.6">
        <path className="cad-trade" d={`M${tx(0.6)} 51 l9 9 -9 9 -9 -9z`} />
        <text className="cad-note" x={tx(0.6)} y="40" textAnchor="middle">their trade</text>
      </g>

      {DETECT_TICKS.map((t) => (
        <circle key={t} data-t={t} className={`cad-dot${t === 2 ? ' is-seen' : ''}`} cx={tx(t)} cy="136" r={t === 2 ? 7 : 4} />
      ))}
      <text data-t="2" className="cad-note is-green" x={tx(2) + 14} y="122">first check that shows it</text>
      <path data-t="2.1" className="cad-link" d={`M${tx(2)} 143 C ${tx(2)} 178, ${tx(2.3)} 172, ${tx(2.3)} 205`} pathLength="1" />

      {RECONCILE_TICKS.map((t) => (
        <rect key={t} data-t={t} className="cad-tick" x={tx(t) - 4} y="284" width="8" height="8" />
      ))}
      {[5, 10].map((t) => (
        <path key={t} data-t={t} className="cad-guide" d={`M${tx(t)} 280 V252`} />
      ))}

      <g data-t="2.3">
        <circle className="cad-try is-sent" cx={tx(2.3)} cy="212" r="7" />
        <text className="cad-note" x={tx(2.3)} y="240" textAnchor="middle">try 1 · sent at once</text>
      </g>
      {[5, 10].map((t, index) => (
        <g key={t} data-t={t}>
          <circle className="cad-try" cx={tx(t)} cy="212" r="7" />
          <text className="cad-note" x={tx(t)} y="240" textAnchor="middle">{`try ${index + 2}`}</text>
        </g>
      ))}
      <g data-t="5">
        <path className="cad-bracket" d={`M${tx(5)} 196 V190 H${tx(10)} V196`} />
        <text className="cad-note" x={tx(7.5)} y="182" textAnchor="middle">only if the last try did not fill · three at most</text>
      </g>
      <g data-t="11">
        <path className="cad-drop" d={`M${tx(11) - 5} 207 l10 10 M${tx(11) + 5} 207 l-10 10`} />
        <text className="cad-note" x={tx(11) + 14} y="216">then dropped</text>
      </g>

    </svg>
  )
}

function CadenceTall() {
  return (
    <svg className="cadence cadence-tall" viewBox="0 0 360 580" role="img" aria-labelledby="cad-tall-title cad-tall-desc">
      <title id="cad-tall-title">The first fifteen seconds of a copy</title>
      <desc id="cad-tall-desc">{CADENCE_DESC}</desc>

      {TALL_LANES.map(([name, cadence, x], index) => (
        <g key={name}>
          <text className="cad-name" x={x} y="24" textAnchor="middle">{name}</text>
          <text className="cad-cadence" x={x} y="44" textAnchor="middle">{cadence}</text>
          <path className="cad-track" d={`M${x} 92 V560`} pathLength="1" style={{ '--d': index }} />
        </g>
      ))}

      {RECONCILE_TICKS.map((t) => (
        <text key={t} className="cad-cadence" x="0" y={ty(t) + 4}>{t}s</text>
      ))}
      <g className="cad-playhead" data-axis="y">
        <path d="M24 104 H344" />
        <text x="344" y="98" textAnchor="end">0.0s</text>
      </g>

      <g data-t="0.6">
        <path className="cad-trade" d={`M58 ${ty(0.6) - 9} l9 9 -9 9 -9 -9z`} />
        <text className="cad-note" x="58" y={ty(0.6) + 28} textAnchor="middle">their trade</text>
      </g>

      {DETECT_TICKS.map((t) => (
        <circle key={t} data-t={t} className={`cad-dot${t === 2 ? ' is-seen' : ''}`} cx="138" cy={ty(t)} r={t === 2 ? 7 : 4} />
      ))}
      <text data-t="2" className="cad-note is-green" x="150" y={ty(2) - 10}>seen</text>
      <path data-t="2.1" className="cad-link" d={`M145 ${ty(2)} C 180 ${ty(2)}, 186 ${ty(2.3)}, 213 ${ty(2.3)}`} pathLength="1" />

      {RECONCILE_TICKS.map((t) => (
        <rect key={t} data-t={t} className="cad-tick" x="306" y={ty(t) - 4} width="8" height="8" />
      ))}
      {[5, 10].map((t) => (
        <path key={t} data-t={t} className="cad-guide" d={`M302 ${ty(t)} H240`} />
      ))}

      <g data-t="2.3">
        <circle className="cad-try is-sent" cx="222" cy={ty(2.3)} r="10" />
        <text className="cad-num is-sent" x="222" y={ty(2.3) + 4} textAnchor="middle">1</text>
      </g>
      {[5, 10].map((t, index) => (
        <g key={t} data-t={t}>
          <circle className="cad-try" cx="222" cy={ty(t)} r="10" />
          <text className="cad-num" x="222" y={ty(t) + 4} textAnchor="middle">{index + 2}</text>
        </g>
      ))}
      <g data-t="5">
        <path className="cad-bracket" d={`M234 ${ty(5)} H240 V${ty(10)} H234`} />
        <text className="cad-note" x="248" y={ty(7.5) - 10} textAnchor="start">if the</text>
        <text className="cad-note" x="248" y={ty(7.5) + 4} textAnchor="start">last try</text>
        <text className="cad-note" x="248" y={ty(7.5) + 18} textAnchor="start">missed</text>
      </g>
      <g data-t="11.2">
        <path className="cad-drop" d={`M217 ${ty(11.2) - 5} l10 10 M227 ${ty(11.2) - 5} l-10 10`} />
        <text className="cad-note" x="222" y={ty(11.2) + 26} textAnchor="middle">dropped</text>
      </g>

    </svg>
  )
}

/* The screener's research cohort as a bar chart: how many wallets land in each
   verdict. Bars grow in a wave as the panel arrives (the kit's bar-chart
   entrance) and the headline share rolls up from zero. */
function CohortChart() {
  const ref = useRef(null)
  const seen = useInView(ref)
  const peak = Math.max(...COHORT.rows.map(([, , n]) => n))
  const ahead = COHORT.rows
    .filter(([, tone]) => tone === 'proven' || tone === 'ahead')
    .reduce((sum, [, , n]) => sum + n, 0)
  const share = Math.round((ahead / COHORT.total) * 100)
  const pct = (n) => `${Math.round((n / COHORT.total) * 100)}%`
  return (
    <div ref={ref} className="panel cohort">
      <p className="panel-label">WALLET SCREENER · RESEARCH COHORT, {COHORT.asOf.toUpperCase()}</p>
      <div className="cohort-lead">
        <Roll text={`${seen ? share : 0}%`} className="cohort-share" />
        <p>of {COHORT.total.toLocaleString('en-US')} wallets finished ahead of what copying them would cost.</p>
      </div>
      <ul className="cohort-bars">
        {COHORT.rows.map(([label, tone, n, line], index) => (
          <li key={label} className={`cohort-row is-${tone}`} style={{ '--i': index, '--w': n / peak }}>
            <span className="cohort-name">{label}</span>
            <span className="cohort-track" aria-hidden="true"><i /></span>
            <span className="cohort-count"><b>{n.toLocaleString('en-US')}</b> {pct(n)}</span>
            <span className="cohort-line">{line}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function Lifecycle() {
  const redeem = useRedeemStatus()
  return (
    <div className="panel lifecycle reveal">
      <p className="panel-label">POSITION STATES · AS THE APP SHOWS THEM</p>
      <ol className="life-list">
        {LIFECYCLE.map(({ states, note, noteAuto }, row) => (
          <li key={states.map(([state]) => state).join('-')} className="life-row rise" style={{ '--i': row }}>
            <div className="life-states">
              {states.map(([state, tone], index) => (
                <Fragment key={state}>
                  {index > 0 && <span className="life-arrow" aria-hidden="true">→</span>}
                  <span className={`badge ${tone}`}>{state}</span>
                </Fragment>
              ))}
            </div>
            <p>{redeem.auto && noteAuto ? noteAuto : note}</p>
          </li>
        ))}
      </ol>
    </div>
  )
}

/* The product, screen by screen. On a desktop with motion allowed the section
   pins and scrolling carries the strip sideways (the kit's horizontal
   gallery); on a phone, or with reduced motion, it is a strip you swipe. */
function Showcase() {
  const section = useRef(null)
  const track = useRef(null)
  const bar = useRef(null)

  useEffect(() => {
    const s = section.current
    const t = track.current
    const b = bar.current
    if (!s || !t || !b) return undefined
    const pin = window.matchMedia('(min-width: 961px) and (prefers-reduced-motion: no-preference)')
    let travel = 0
    let raf = 0
    const update = () => {
      raf = 0
      if (!s.classList.contains('is-pinned')) return
      const max = s.offsetHeight - window.innerHeight
      const p = max > 0 ? Math.min(1, Math.max(0, -s.getBoundingClientRect().top / max)) : 0
      t.style.transform = `translate3d(${(-p * travel).toFixed(1)}px, 0, 0)`
      b.style.setProperty('--p', p.toFixed(4))
    }
    const layout = () => {
      if (!pin.matches) {
        s.classList.remove('is-pinned')
        s.style.removeProperty('--travel')
        t.style.transform = ''
        return
      }
      s.classList.add('is-pinned')
      travel = Math.max(0, t.scrollWidth - t.parentElement.clientWidth)
      s.style.setProperty('--travel', `${travel}px`)
      update()
    }
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(update) }
    layout()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', layout)
    pin.addEventListener('change', layout)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', layout)
      pin.removeEventListener('change', layout)
    }
  }, [])

  return (
    <section ref={section} className="showcase" aria-labelledby="showcase-title">
      <div className="showcase-stage">
        <div className="public-wrap showcase-head reveal">
          <div className="rise" style={{ '--i': 0 }}>
            <div className="eyebrow">INSIDE THE BOT</div>
            <h2 id="showcase-title">The product, screen by screen.</h2>
          </div>
          <p className="rise" style={{ '--i': 1 }}>
            After sign-up, everything happens in the Telegram Mini App. These are its real
            screens, shown with a sample account and its sample figures.
          </p>
        </div>
        <ol ref={track} className="showcase-track">
          {SHOWCASE.map((item) => (
            <li key={item.img} className="show-card">
              <img src={`/showcase/${item.img}.webp`} width="600" height="1298" alt={item.alt} loading="lazy" decoding="async" />
              <div className="show-text">
                <span className="show-step">{item.step}</span>
                <h3>{item.title}</h3>
                <p>{item.body}</p>
              </div>
            </li>
          ))}
        </ol>
        <div className="public-wrap showcase-progress" aria-hidden="true"><i ref={bar} /></div>
      </div>
    </section>
  )
}

/* Behind the hero, a field of dots drawn with WebGL: the market at rest, one
   ripple as the page opens (a trade) and a smaller one after it (the copy),
   then still. It redraws only when something changes, the pointer near it or
   the page scrolling it away, so nothing loops. It is its own chunk, loaded
   after the page, on desktop widths only, and only where WebGL2 exists; without
   it the paper behind the hero is the whole design. */
function HeroField() {
  const canvas = useRef(null)
  useEffect(() => {
    if (window.matchMedia?.('(max-width: 760px)').matches) return undefined
    let stop = () => {}
    let cancelled = false
    import('./heroField.js')
      .then(({ mountHeroField }) => {
        if (!cancelled && canvas.current) stop = mountHeroField(canvas.current, { still: prefersReducedMotion() })
      })
      .catch(() => { /* no field; the paper stands on its own */ })
    return () => { cancelled = true; stop() }
  }, [])
  return <canvas ref={canvas} className="hero-field" aria-hidden="true" />
}
