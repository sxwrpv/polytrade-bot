/* The Copy Score column's furniture: the band pill, the week-over-week move,
 * the "harder to mirror" flag and the trend sparkline.
 *
 * Every one of these renders Polycopy's figure, not PolyTrade's. The pill's
 * tooltip and accessible label say what the band means; the board states whose
 * number it is and when it was generated once, above the table, rather than
 * repeating a provenance sentence on all fifty rows.
 */
import {
  classDef, scoreIsNumeric, scoreMagnitude, scoreSigned,
} from './cohortModel'

const TONE_COLOR = {
  clear: 'var(--green-deep)',
  ahead: 'var(--green-text)',
  caution: 'var(--yellow)',
  negative: 'var(--red)',
  neutral: 'var(--cyan)',
  unknown: 'var(--faint)',
}

/** The Copy Score pill. `variant="signed"` prints +6 / −4 instead of 11%. */
export function CopyChip({ copyClass, copyNet, variant = 'board', showState = true }) {
  const def = classDef(copyClass)
  const numeric = scoreIsNumeric(copyClass, copyNet)
  // A number-only chip with no number to show would render as an empty pill.
  if (!showState && !numeric) return null
  return (
    <span
      className="cchip"
      style={{ '--tone': TONE_COLOR[def.tone] }}
      title={`${def.chip} — ${def.line}`}
      aria-label={
        `Copy Score: ${numeric ? `${scoreSigned(copyNet)}. ${def.chip}` : def.chip}. `
        + 'A read on the trader, not on this trade.'
      }
    >
      {showState && <span className="cchip-state">{def.chip}</span>}
      {numeric && (
        <span className="cchip-num">
          {variant === 'signed' ? scoreSigned(copyNet) : scoreMagnitude(copyNet)}
        </span>
      )}
    </span>
  )
}

/** Week-over-week score move. Suppressed below 5 points, exactly as upstream:
 *  smaller moves are inside the noise the score is recomputed with. */
export function ScoreMove({ delta }) {
  if (!delta || !Number.isFinite(delta[0])) return null
  const [points, days] = delta
  const size = Math.round(Math.abs(points))
  if (size < 5) return null
  const up = points > 0
  return (
    <span
      className={`smove ${up ? 'up' : 'down'}`}
      title={`Copy Score is ${size} ${size === 1 ? 'point' : 'points'} ${up ? 'higher' : 'lower'} than ${days} days ago`}
    >{up ? '▲' : '▼'} {size} pts</span>
  )
}

const HARD_TO_MIRROR = {
  market_making: {
    label: 'Market maker',
    why: 'They earn mainly from the buy/sell spread, so copying means paying that spread instead of collecting it.',
  },
  arb: {
    label: 'Arbitrage',
    why: 'They profit by holding both sides of a market at once; a bot mirrors one side, which leaves the risk without the locked-in profit.',
  },
  frequency: {
    label: 'Very high frequency',
    why: 'They place 90+ orders a day, so their edge is often gone before a copy can fill.',
  },
}

export function hardToMirrorFlags({ mm, arb, freq }) {
  const out = []
  if (mm === 'market_maker') out.push(HARD_TO_MIRROR.market_making)
  if (arb === 'arb') out.push(HARD_TO_MIRROR.arb)
  if (freq === 'vhft') out.push(HARD_TO_MIRROR.frequency)
  return out
}

export function MirrorChip({ row }) {
  const flags = hardToMirrorFlags(row)
  if (!flags.length) return null
  return (
    <span
      className="hchip"
      title={`Harder to mirror — ${flags.map((flag) => flag.why).join(' ')}`}
    >{flags.length === 1 ? flags[0].label : `${flags[0].label} +${flags.length - 1}`}</span>
  )
}

const UP = '#0b9e63'
const DOWN = '#b3242f'

/** Weekly realised-PnL sparkline. Fewer than three points is not a shape, so
 *  it says so rather than drawing a line through two dots. */
export function Sparkline({ values, width = 84, height = 28 }) {
  if (!values || values.length < 3) return <span className="coverage">no series</span>
  const min = Math.min(...values)
  const span = Math.max(...values) - min || 1
  const points = values.map((value, index) => (
    `${((index / (values.length - 1)) * width).toFixed(1)},`
    + `${(height - 2 - ((value - min) / span) * (height - 4)).toFixed(1)}`
  )).join(' ')
  const up = values[values.length - 1] >= values[0]
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      style={{ width: `${width}px`, height: `${height}px`, display: 'block' }}
      aria-hidden="true"
    >
      <polyline
        points={points}
        fill="none"
        stroke={up ? UP : DOWN}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  )
}

/** One band header inside the table, printed once per run of equal bands. */
export function BandRow({ band, total, colSpan }) {
  const def = classDef(band)
  return (
    <tr className="band-row">
      <td colSpan={colSpan}>
        <div className={`band band-${band}`}>
          <span className="band-dot" />
          <span className="band-name">{def.chip}</span>
          <span className="band-n">
            {total.toLocaleString('en-US')} {total === 1 ? 'wallet' : 'wallets'}
          </span>
          <span className="band-line">{def.line}</span>
        </div>
      </td>
    </tr>
  )
}
