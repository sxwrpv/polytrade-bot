import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'

/* Total balance and its curve, at the top of Home.
 *
 * Deliberately one number and one shape. This is the glance surface — the
 * account opens the app to see what it is worth — so the cash / positions /
 * claimable breakdown lives on the User tab rather than competing here.
 *
 * The chart is hand-drawn SVG rather than the Chart.js instance the User tab
 * uses. Axes, gridlines and tick labels are most of that component's height,
 * and none of them earn their space at a glance: the figure above the chart
 * already states the current value, and hovering states any other point.
 */

const UP = '#0b9e63'
const DOWN = '#b3242f'
const PERIODS = [['7d', '7D'], ['30d', '30D'], ['all', 'ALL']]
const H = 78                  // plot height; the whole card lands near 190px
const PAD = 3                 // keeps the 2px stroke off the clip edge

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))
const money = (v) => (v == null || !Number.isFinite(v) ? '—' : `$${v.toFixed(2)}`)
const signed = (v) => `${v >= 0 ? '+' : '−'}$${Math.abs(v).toFixed(2)}`

export default function BalanceCard() {
  const [me, setMe] = useState(null)
  const [period, setPeriod] = useState('30d')
  const [series, setSeries] = useState([])
  const [at, setAt] = useState(null)          // hovered index, null = idle
  const svgRef = useRef(null)

  useEffect(() => {
    // Cheap read paints the card; the second costs a server-side CLOB client
    // build on first call, so the balance lands late. Until it does the figure
    // shows a dash — a balance we have not read is not a balance of nothing.
    api.me().then((m) => setMe((prev) => prev ?? m)).catch(() => {})
    api.me(true).then(setMe).catch(() => {})
  }, [])

  useEffect(() => {
    setAt(null)
    api.equitySeries(period).then(setSeries).catch(() => setSeries([]))
  }, [period])

  const points = useMemo(
    () => (series || []).filter((s) => Number.isFinite(s.equity)).map((s) => ({ t: s.ts, y: s.equity })),
    [series],
  )

  const geom = useMemo(() => {
    if (points.length < 2) return null
    const ys = points.map((p) => p.y)
    const lo = Math.min(...ys)
    const hi = Math.max(...ys)
    const span = hi - lo || Math.max(1, Math.abs(hi) * 0.1)
    const x = (i) => (i / (points.length - 1)) * 100
    const y = (v) => H - PAD - ((v - lo) / span) * (H - PAD * 2)
    return { x, y, rising: ys[ys.length - 1] >= ys[0] }
  }, [points])

  const cursor = at == null ? points.length - 1 : clamp(at, 0, points.length - 1)
  const shown = points[cursor]
  const delta = points.length ? (shown?.y ?? 0) - points[0].y : null

  const track = (clientX) => {
    const rect = svgRef.current?.getBoundingClientRect()
    // A zero-width measurement means the card has not been laid out; snapping
    // to the first point would be a confident wrong answer.
    if (!rect?.width || points.length < 2) return
    setAt(Math.round(((clientX - rect.left) / rect.width) * (points.length - 1)))
  }

  return (
    <div className="card balance-card">
      <div className="balance-head">
        <span className="balance-label">TOTAL BALANCE</span>
        {me?.claimable > 0 && (
          <span className="balance-claimable" title="resolved wins not yet redeemed">
            {money(me.claimable)} claimable
          </span>
        )}
      </div>

      <div className="balance-figure">
        <span className="balance-value">{money(at == null ? me?.equity : shown?.y)}</span>
        {delta != null && points.length > 1 && (
          <span className={`balance-delta ${delta >= 0 ? 'pos' : 'neg'}`}>{signed(delta)}</span>
        )}
      </div>

      <div className="balance-chart">
        {geom ? (
          <>
            {at != null && shown && (
              <div
                className="balance-tip"
                style={{ left: `${clamp(geom.x(cursor), 6, 94)}%` }}
              >
                <b>{new Date(shown.t).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}</b>
                <i>{money(shown.y)}</i>
              </div>
            )}
            <svg
              ref={svgRef}
              viewBox={`0 0 100 ${H}`}
              preserveAspectRatio="none"
              role="img"
              aria-label={`Equity over the last ${period}, currently ${money(me?.equity)}`}
              onPointerMove={(e) => track(e.clientX)}
              onPointerDown={(e) => track(e.clientX)}
              onPointerLeave={() => setAt(null)}
            >
              <defs>
                <linearGradient id="balance-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={geom.rising ? UP : DOWN} stopOpacity="0.26" />
                  <stop offset="100%" stopColor={geom.rising ? UP : DOWN} stopOpacity="0" />
                </linearGradient>
              </defs>
              <polygon
                points={`0,${H} ${points.map((p, i) => `${geom.x(i)},${geom.y(p.y)}`).join(' ')} 100,${H}`}
                fill="url(#balance-fill)"
              />
              <polyline
                points={points.map((p, i) => `${geom.x(i)},${geom.y(p.y)}`).join(' ')}
                fill="none"
                stroke={geom.rising ? UP : DOWN}
                strokeWidth="2"
                strokeLinejoin="round"
                strokeLinecap="round"
                vectorEffect="non-scaling-stroke"
              />
              {at != null && (
                <line
                  x1={geom.x(cursor)} y1="0" x2={geom.x(cursor)} y2={H}
                  stroke="rgba(20,32,26,0.22)" strokeWidth="1" vectorEffect="non-scaling-stroke"
                />
              )}
              <circle
                cx={geom.x(cursor)} cy={geom.y(shown.y)} r="3"
                fill={geom.rising ? UP : DOWN} stroke="var(--bg)" strokeWidth="2"
                vectorEffect="non-scaling-stroke"
              />
            </svg>
          </>
        ) : (
          <p className="balance-empty">
            {points.length === 1
              ? 'One snapshot so far — the curve fills in as the bot records your account.'
              : 'Collecting snapshots. Your balance curve appears once the bot has recorded your account a few times.'}
          </p>
        )}
      </div>

      <div className="balance-periods">
        {PERIODS.map(([key, text]) => (
          <button
            key={key}
            type="button"
            className={`chip ${period === key ? 'active' : ''}`}
            aria-pressed={period === key}
            onClick={() => setPeriod(key)}
          >{text}</button>
        ))}
      </div>
    </div>
  )
}
