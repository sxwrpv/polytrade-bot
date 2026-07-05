// Tiny inline SVG equity curve — cumulative realized PnL per day. Pure SVG
// (no chart lib) so 50 of these can sit in the screener list for free.
// `daily` is [{ date, pnl }] ascending; the drawn line is the running sum.
export default function Sparkline({ daily, width = 260, height = 48 }) {
  if (!daily || daily.length === 0) {
    return <div className="spark-empty muted small">no realized trades in this window</div>
  }

  let cum = 0
  const points = daily.map((d) => (cum += d.pnl))
  const final = points[points.length - 1]
  // include 0 so the baseline is always in view
  const lo = Math.min(0, ...points)
  const hi = Math.max(0, ...points)
  const span = hi - lo || 1
  const x = (i) => (points.length === 1 ? width / 2 : (i / (points.length - 1)) * width)
  const y = (v) => height - ((v - lo) / span) * height
  const path = points.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const color = final >= 0 ? 'var(--green)' : 'var(--red)'

  return (
    <svg
      className="sparkline"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`realized pnl curve, ${final >= 0 ? '+' : '-'}$${Math.abs(final).toFixed(0)}`}
    >
      <line x1="0" y1={y(0)} x2={width} y2={y(0)} stroke="var(--border)" strokeWidth="1" />
      <polyline points={path} fill="none" stroke={color} strokeWidth="1.5" />
      <circle cx={x(points.length - 1)} cy={y(final)} r="2" fill={color} />
    </svg>
  )
}
