// Three-step activation strip — shown until the account is actually copying
// with money behind it, then disappears for good. Each pending step says
// exactly where to go, because a funded-but-idle or copying-but-unfunded
// account silently does nothing.
export default function GettingStarted({ balance, followingCount }) {
  const funded = (balance || 0) > 0
  const copying = (followingCount || 0) > 0
  if (funded && copying) return null

  const steps = [
    ['1. FUND WALLET', funded, 'USER tab > FUND WALLET — choose a listed network and supported asset'],
    ['2. COPY A WALLET', copying, 'pick a trader below and hit COPY TRADER'],
    ['3. MONITOR ACTIVITY', false, 'eligible trades are attempted within your limits; timing and execution can differ'],
  ]

  return (
    <div className="card getting-started">
      <div className="section-header" style={{ marginTop: 0 }}>GET STARTED</div>
      {steps.map(([label, done, hint]) => (
        <div className={`gs-step ${done ? 'done' : ''}`} key={label}>
          <span className="gs-check">{done ? '[✓]' : '[ ]'}</span>
          <span className="gs-label">{label}</span>
          <span className="muted small">{done ? '' : hint}</span>
        </div>
      ))}
      {!funded && <p className="muted small funding-note">No in-app withdrawals or automatic redemption. Moving funds may require external tools and network fees.</p>}
    </div>
  )
}
