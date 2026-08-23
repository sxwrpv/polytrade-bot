import { useMemo, useRef, useState } from 'react'

/* An interactive series chart.
 *
 * The pointer position is read from the element's own bounding rect rather
 * than from the viewBox, so the maths survives `preserveAspectRatio="none"`
 * and any container width. A zero-width measurement returns null instead of
 * index 0 — a chart that has not been laid out does not know where the pointer
 * is, and snapping the crosshair to the first point would be a confident wrong
 * answer.
 *
 * Keyboard drives the same cursor as the pointer, and the value is announced,
 * because a chart whose numbers are only reachable by hovering is a chart half
 * the readers cannot use.
 */

const UP = '#0b9e63'
const DOWN = '#b3242f'
const W = 720
const PAD = { top: 10, right: 10, bottom: 16, left: 10 }

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

export default function SeriesChart({
  points, height = 190, shape = 'area', format = (v) => String(v), label = 'Value',
}) {
  const svgRef = useRef(null)
  const clean = useMemo(() => (points || []).filter((p) => Number.isFinite(p.value)), [points])
  const [cursor, setCursor] = useState(null)   // index, or null when idle

  const geom = useMemo(() => {
    if (clean.length < 2) return null
    const values = clean.map((p) => p.value)
    const lo = Math.min(...values, 0)
    const hi = Math.max(...values, 0)
    const span = hi - lo || 1
    const x = (i) => PAD.left + (i / (clean.length - 1)) * (W - PAD.left - PAD.right)
    const y = (v) => height - PAD.bottom - ((v - lo) / span) * (height - PAD.top - PAD.bottom)
    return { lo, hi, x, y, zero: y(0), rising: values[values.length - 1] >= values[0] }
  }, [clean, height])

  if (!geom) {
    return <p className="chart-empty">Not enough data in this window to draw a series.</p>
  }

  const { lo, hi, x, y, zero, rising } = geom
  const stroke = rising ? UP : DOWN
  const at = cursor == null ? clean.length - 1 : clamp(cursor, 0, clean.length - 1)
  const point = clean[at]
  const delta = point.value - clean[0].value

  const fromPointer = (clientX) => {
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect?.width) return null
    return Math.round(((clientX - rect.left) / rect.width) * (clean.length - 1))
  }
  const track = (e) => {
    const i = fromPointer(e.clientX)
    if (i !== null) setCursor(i)
  }
  const onKeyDown = (e) => {
    const step = e.shiftKey ? 7 : 1
    const next = {
      ArrowRight: at + step, ArrowLeft: at - step, Home: 0, End: clean.length - 1,
    }[e.key]
    if (next === undefined) return
    e.preventDefault()
    setCursor(clamp(next, 0, clean.length - 1))
  }

  const path = clean.map((p, i) => `${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ')
  const barWidth = Math.max(1, (W - PAD.left - PAD.right) / clean.length - 1)

  return (
    <div className="chart">
      <div className="chart-readout">
        <span className={`chart-readout-value ${point.value >= 0 ? 'pos' : 'neg'}`}>
          {format(point.value)}
        </span>
        <span className="chart-readout-date">{point.date}</span>
        {at > 0 && (
          <span className="chart-readout-delta">
            {delta >= 0 ? '+' : '−'}{format(Math.abs(delta))} in window
          </span>
        )}
      </div>

      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        preserveAspectRatio="none"
        style={{ width: '100%', height, display: 'block', touchAction: 'pan-y' }}
        role="slider"
        tabIndex={0}
        aria-label={`${label} over ${clean.length} days`}
        aria-valuemin={0}
        aria-valuemax={clean.length - 1}
        aria-valuenow={at}
        aria-valuetext={`${point.date}, ${format(point.value)}`}
        onPointerMove={track}
        onPointerDown={track}
        onPointerLeave={() => setCursor(null)}
        onKeyDown={onKeyDown}
        onBlur={() => setCursor(null)}
      >
        {(lo < 0 || hi > 0) && (
          <line
            x1={PAD.left} y1={zero} x2={W - PAD.right} y2={zero}
            stroke="rgba(20,32,26,0.16)" strokeWidth="1" strokeDasharray="3 3"
          />
        )}

        {shape === 'bar' ? (
          clean.map((p, i) => (
            <rect
              key={p.date}
              x={x(i) - barWidth / 2}
              y={Math.min(y(p.value), zero)}
              width={barWidth}
              height={Math.max(1, Math.abs(y(p.value) - zero))}
              fill={p.value >= 0 ? UP : DOWN}
              fillOpacity="0.55"
            />
          ))
        ) : (
          <>
            <polygon
              points={`${x(0).toFixed(1)},${zero.toFixed(1)} ${path} ${x(clean.length - 1).toFixed(1)},${zero.toFixed(1)}`}
              fill={rising ? 'rgba(11,158,99,.14)' : 'rgba(179,36,47,.12)'}
            />
            <polyline
              points={path} fill="none" stroke={stroke} strokeWidth="2"
              strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke"
            />
          </>
        )}

        {cursor !== null && (
          <g>
            <line
              x1={x(at)} y1={PAD.top} x2={x(at)} y2={height - PAD.bottom}
              stroke="rgba(20,32,26,0.34)" strokeWidth="1" vectorEffect="non-scaling-stroke"
            />
            <circle cx={x(at)} cy={y(point.value)} r="4" fill={stroke} stroke="#eef2ef" strokeWidth="2" />
          </g>
        )}
      </svg>

      <div className="chart-axis">
        <span>{clean[0].date}</span>
        <span className="chart-axis-range">{format(lo)} — {format(hi)}</span>
        <span>{clean[clean.length - 1].date}</span>
      </div>
    </div>
  )
}
