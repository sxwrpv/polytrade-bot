import { useId } from 'react'
import { nearestStop } from '../copySizing'

// Slider + numeric input for one always-on per-wallet setting (unlike a
// screener filter, there's no "off" — a copy setting always has a value).
// Shows the current value inline with its unit. Calls onChange live as the
// user drags; the parent debounces the actual save. The track fills to the
// thumb (--fill), and the number field opens a decimal keypad on phones.
// With `stops`, the thumb moves over those values in order, so a wide range
// such as 0.1× to 100× keeps fine steps where they matter; the number field
// still takes any value.
const formatValue = (v, unit) => {
  if (unit === '$') return `$${v.toLocaleString('en-US')}`
  if (unit === '%') return `${v}%`
  if (unit === '¢') return `${Math.round(v * 100)}¢`
  return `${v}${unit}`
}

export default function SettingSlider({
  label, value, onChange, min, max, step = 1, unit = '', hint = '', stops,
}) {
  const v = value == null || value === '' ? min : Number(value)
  const at = stops ? nearestStop(stops, v) : null
  const fill = stops
    ? (at / (stops.length - 1)) * 100
    : max > min ? Math.min(100, Math.max(0, ((v - min) / (max - min)) * 100)) : 0
  const hintId = useId()

  return (
    <div className="fld fld-slider">
      <div className="setting-head">
        <span>{label}</span>
        <span className="setting-val" aria-hidden="true">{formatValue(v, unit)}</span>
      </div>
      <div className="slider-row">
        <input
          type="range"
          min={stops ? 0 : min}
          max={stops ? stops.length - 1 : max}
          step={stops ? 1 : step}
          value={stops ? at : v}
          style={{ '--fill': `${fill}%` }}
          aria-label={label}
          aria-valuetext={stops ? formatValue(v, unit) : undefined}
          aria-describedby={hint ? hintId : undefined}
          onChange={(e) => onChange(stops ? stops[Number(e.target.value)] : Number(e.target.value))}
          onWheel={(e) => e.currentTarget.blur()}
        />
        <input
          className="slider-num"
          inputMode="decimal"
          aria-label={`${label}, exact value`}
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
        />
      </div>
      {hint && <span className="setting-hint" id={hintId}>{hint}</span>}
    </div>
  )
}
