// Three-step activation strip — shown until the account is actually copying
// with money behind it, then disappears for good. Each pending step says
// exactly where to go, because a funded-but-idle or copying-but-unfunded
// account silently does nothing.
export default function GettingStarted({ balance, followingCount }) {
  const funded = (balance || 0) > 0
  const copying = (followingCount || 0) > 0
  if (funded && copying) return null

  const steps = [
    ['1. FUND WALLET', funded, 'USER tab > FUND WALLET — send USDC/USDT from any chain'],
    ['2. COPY A WALLET', copying, 'pick a trader below and hit COPY TRADER'],
    ['3. BOT TRADES FOR YOU', false, 'the engine mirrors every trade they make, 24/7'],
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
    </div>
  )
}
