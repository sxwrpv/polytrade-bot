import { useMemo, useRef, useState } from 'react'

/* Daily realised PnL for the last 30 days, as a calendar heat-map.
 *
 * Replaces the dot strip. A dot could say green / red / flat; this says how
 * green and how red, and which day, which is the question anyone reading a
 * consistency graphic is actually asking.
 *
 * Two scales, not one. Wins and losses are normalised against their own
 * extremes rather than a shared range, so a single huge loss cannot wash every
 * winning day out to the same pale tint. Both use a square root, for the same
 * reason at a smaller scale.
 *
 * Days with no closings are deliberately distinct from days that closed at
 * exactly zero: "nothing happened" and "something happened and netted nothing"
 * are different facts about a wallet.
 */

const DAYS = 30
const WEEKDAYS = ['M', 'T', 'W', 'T', 'F', 'S', 'S']

const iso = (d) => d.toISOString().slice(0, 10)
const money = (v) => `${v >= 0 ? '+' : '−'}$${Math.abs(v).toFixed(2)}`

export default function PnlHeatmap({ curve = [] }) {
  const [pinned, setPinned] = useState(null)
  const [hover, setHover] = useState(null)
  const cellsRef = useRef([])

  const { grid, stats } = useMemo(() => {
    const byDate = new Map(
      (curve || [])
        .filter((d) => d && d.date)
        .map((d) => [String(d.date).slice(0, 10), Number(d.pnl) || 0]),
    )

    // Walk back 30 days, then pad to whole weeks so the columns line up.
    const today = new Date()
    today.setUTCHours(0, 0, 0, 0)
    const days = []
    for (let i = DAYS - 1; i >= 0; i--) {
      const d = new Date(today)
      d.setUTCDate(d.getUTCDate() - i)
      const key = iso(d)
      days.push({ date: key, pnl: byDate.has(key) ? byDate.get(key) : null })
    }
    // Monday-first offset for the first column.
    const lead = (new Date(`${days[0].date}T00:00:00Z`).getUTCDay() + 6) % 7
    const padded = [...Array.from({ length: lead }, () => null), ...days]
    while (padded.length % 7) padded.push(null)

    const values = days.map((d) => d.pnl).filter((v) => v != null)
    const wins = values.filter((v) => v > 0)
    const losses = values.filter((v) => v < 0)
    return {
      grid: padded,
      stats: {
        bestWin: wins.length ? Math.max(...wins) : 0,
        worstLoss: losses.length ? Math.abs(Math.min(...losses)) : 0,
        up: wins.length,
        down: losses.length,
        flat: values.length - wins.length - losses.length,
        traded: values.length,
      },
    }
  }, [curve])

  if (!curve || curve.length === 0) {
    return <span className="muted small">no history yet</span>
  }

  const tone = (pnl) => {
    if (pnl == null) return { className: 'hm-cell hm-none' }
    if (pnl === 0) return { className: 'hm-cell hm-zero' }
    const scale = pnl > 0 ? stats.bestWin : stats.worstLoss
    const t = scale > 0 ? Math.sqrt(Math.abs(pnl) / scale) : 1
    return {
      className: `hm-cell ${pnl > 0 ? 'hm-up' : 'hm-down'}`,
      style: { '--t': t.toFixed(3) },
    }
  }

  const describe = (cell) => {
    if (!cell) return null
    const when = new Date(`${cell.date}T00:00:00Z`)
      .toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
    if (cell.pnl == null) return `${when} — no closings`
    if (cell.pnl === 0) return `${when} — closed flat`
    return `${when} — ${money(cell.pnl)}`
  }

  const firstCell = grid.findIndex(Boolean)
  const shown = pinned ?? hover
  const move = (e, index) => {
    const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 7, ArrowUp: -7 }[e.key]
    if (step === undefined) return
    e.preventDefault()
    const next = Math.min(grid.length - 1, Math.max(0, index + step))
    cellsRef.current[next]?.focus()
  }

  return (
    <div className="hm">
      <div className="hm-readout">
        {describe(shown) || `${stats.up} up · ${stats.down} down · ${stats.flat} flat over ${stats.traded} trading days`}
      </div>

      <div className="hm-week" aria-hidden="true">
        {WEEKDAYS.map((d, i) => <span key={i}>{d}</span>)}
      </div>
      <div
        className="hm-grid"
        onFocus={(e) => {
          const key = e.target?.dataset?.date
          if (key) setHover(grid.find((c) => c && c.date === key) || null)
        }}
        onBlur={(e) => {
          // Only clear when focus leaves the grid entirely, not when it moves
          // between two cells inside it.
          if (!e.currentTarget.contains(e.relatedTarget)) setHover(null)
        }}
      >
        {grid.map((cell, i) => {
          if (!cell) return <span key={i} className="hm-cell hm-pad" />
          const { className, style } = tone(cell.pnl)
          const isPinned = pinned?.date === cell.date
          return (
            <button
              key={cell.date}
              ref={(el) => { cellsRef.current[i] = el }}
              type="button"
              className={`${className}${isPinned ? ' is-pinned' : ''}`}
              style={style}
              aria-label={describe(cell)}
              data-date={cell.date}
              tabIndex={firstCell === i ? 0 : -1}
              onPointerEnter={() => setHover(cell)}
              onPointerLeave={() => setHover(null)}
              onClick={() => setPinned(isPinned ? null : cell)}
              onKeyDown={(e) => move(e, i)}
            />
          )
        })}
      </div>

      <div className="hm-legend">
        <span>loss</span>
        <i className="hm-cell hm-down" style={{ '--t': 1 }} />
        <i className="hm-cell hm-down" style={{ '--t': 0.45 }} />
        <i className="hm-cell hm-zero" />
        <i className="hm-cell hm-up" style={{ '--t': 0.45 }} />
        <i className="hm-cell hm-up" style={{ '--t': 1 }} />
        <span>gain</span>
        <span className="hm-legend-none"><i className="hm-cell hm-none" /> no closings</span>
      </div>
    </div>
  )
}
