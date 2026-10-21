import { useId } from 'react'

// Slider + numeric input for one always-on per-wallet setting (unlike a
// screener filter, there's no "off" — a copy setting always has a value).
// Shows the current value inline with its unit. Calls onChange live as the
// user drags; the parent debounces the actual save. The track fills to the
// thumb (--fill), and the number field opens a decimal keypad on phones.
const formatValue = (v, unit) => {
  if (unit === '$') return `$${v.toLocaleString('en-US')}`
  if (unit === '%') return `${v}%`
  if (unit === '¢') return `${Math.round(v * 100)}¢`
  return `${v}${unit}`
}

export default function SettingSlider({
  label, value, onChange, min, max, step = 1, unit = '', hint = '',
}) {
  const v = value == null || value === '' ? min : Number(value)
  const fill = max > min ? Math.min(100, Math.max(0, ((v - min) / (max - min)) * 100)) : 0
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
          min={min}
          max={max}
          step={step}
          value={v}
          style={{ '--fill': `${fill}%` }}
          aria-label={label}
          aria-describedby={hint ? hintId : undefined}
          onChange={(e) => onChange(Number(e.target.value))}
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
