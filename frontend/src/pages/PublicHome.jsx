const BOT_URL = 'https://t.me/cpolytrade_bot'

const Arrow = () => <span aria-hidden="true">↗</span>

export default function PublicHome() {
  return (
    <div className="public-site">
      <header className="public-nav">
        <a className="public-brand" href="#top" aria-label="PolyTrade home">
          <span className="brand-mark">P</span><span>PolyTrade</span>
        </a>
        <a className="public-nav-link" href="#how-it-works">How it works</a>
        <a className="btn public-small-cta" href={BOT_URL}>Open Telegram <Arrow /></a>
      </header>

      <main id="top">
        <section className="public-hero public-wrap">
          <div className="hero-copy">
            <div className="eyebrow">BOT-FIRST COPY TRADING</div>
            <h1>Follow the market.<br /><span>Trade with intention.</span></h1>
            <p className="hero-lede">
              Choose Polymarket wallets to follow, set your own limits, and manage copy trading
              from one calm Telegram experience.
            </p>
            <div className="hero-actions">
              <a className="btn public-primary" href={BOT_URL}>Start in Telegram <Arrow /></a>
              <a className="public-text-link" href="#how-it-works">See how it works ↓</a>
            </div>
            <p className="hero-footnote">Uses real money. Copy trading involves loss risk and is not financial advice.</p>
          </div>
          <ProductPreview />
        </section>

        <section className="public-wrap benefits" aria-labelledby="benefits-title">
          <div>
            <div className="eyebrow">WHY POLYTRADE</div>
            <h2 id="benefits-title">Control without the clutter.</h2>
          </div>
          <div className="benefit-grid">
            <article className="public-card"><span className="benefit-icon" aria-hidden="true">◎</span><h3>You choose who to follow</h3><p>Explore wallet activity and decide which traders fit your approach.</p></article>
            <article className="public-card"><span className="benefit-icon" aria-hidden="true">⌁</span><h3>Your limits stay yours</h3><p>Set allocation and exposure controls rather than copying without boundaries.</p></article>
            <article className="public-card"><span className="benefit-icon" aria-hidden="true">◌</span><h3>Built around Telegram</h3><p>Launch, review, and manage the experience where you already communicate.</p></article>
          </div>
        </section>

        <section id="how-it-works" className="public-wrap process" aria-labelledby="process-title">
          <div className="eyebrow">THREE CLEAR STEPS</div>
          <h2 id="process-title">From curious to configured.</h2>
          <div className="process-grid">
            <article><span>01</span><h3>Open the bot</h3><p>Launch @cpolytrade_bot in Telegram and review the custody, eligibility, and risk terms.</p></article>
            <article><span>02</span><h3>Fund carefully</h3><p>Create a custodial wallet only after acknowledging the security model, then use a supported funding route.</p></article>
            <article><span>03</span><h3>Choose and control</h3><p>Select wallets, configure limits, and monitor activity. Execution can differ or fail due to liquidity, timing, or market conditions.</p></article>
          </div>
        </section>

        <section className="public-wrap preview-section" aria-labelledby="preview-title">
          <div className="preview-intro">
            <div className="eyebrow">A FOCUSED WORKSPACE</div>
            <h2 id="preview-title">See the signal,<br />not the noise.</h2>
            <p>Review followed wallets, open positions, and risk controls in a compact interface designed for decisions—not hype.</p>
          </div>
          <ProductPreview wide />
        </section>

        <section className="public-wrap know" aria-labelledby="know-title">
          <div><div className="eyebrow">A CALM CHECKPOINT</div><h2 id="know-title">Know before you fund.</h2></div>
          <div className="know-list">
            <article><strong>Real-money risk</strong><p>Prediction-market positions can lose some or all of the money committed. Past wallet activity does not predict future results.</p></article>
            <article><strong>Custodial wallet</strong><p>PolyTrade creates and operates a wallet for you. Review the security model and protect access to your Telegram account.</p></article>
            <article><strong>Funding and withdrawals</strong><p>There is no in-app withdrawal or automatic redemption. Exiting and moving funds may require external tools and network fees.</p></article>
            <article><strong>Eligibility is your responsibility</strong><p>Use only where Polymarket and this service are legally available to you. Geographic and other restrictions may apply.</p></article>
          </div>
        </section>

        <section className="public-final">
          <div className="public-wrap final-inner">
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

function ProductPreview({ wide = false }) {
  return (
    <div className={`product-preview ${wide ? 'wide' : ''}`} aria-label="Illustrative product preview">
      <div className="preview-label">ILLUSTRATIVE PRODUCT PREVIEW</div>
      <div className="preview-shell">
        <div className="preview-head"><span><b>P</b> PolyTrade</span><i>● Connected</i></div>
        <div className="preview-stats"><div><small>WALLET BALANCE</small><strong>$—</strong></div><div><small>OPEN POSITIONS</small><strong>—</strong></div><div><small>TOTAL EXPOSURE</small><strong>$—</strong></div></div>
        <div className="preview-row"><span className="avatar">N</span><div><b>Example wallet</b><small>Illustrative allocation</small></div><em>Following</em></div>
        <div className="preview-market"><small>EXAMPLE MARKET</small><b>Will this event occur?</b><div><span>YES <i>54¢</i></span><span>LIMIT <i>$25</i></span></div></div>
        <div className="preview-tabs"><b>HOME</b><span>POSITIONS</span><span>USER</span></div>
      </div>
    </div>
  )
}
