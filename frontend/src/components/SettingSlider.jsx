// Slider + numeric input for one always-on per-wallet setting (unlike the
// screener's FilterSlider, there's no "off" — a copy setting always has a
// value). Shows the current value inline with an optional unit. Calls onChange
// live as the user drags; the parent debounces the actual save.
export default function SettingSlider({
  label, value, onChange, min, max, step = 1, unit = '', hint = '',
}) {
  const v = value == null || value === '' ? min : Number(value)
  const display = unit === '$' ? `$${v}` : unit === '%' ? `${v}%` : `${v}${unit}`

  return (
    <label className="fld fld-slider">
      <span className="setting-head">
        <span>{label}</span>
        <span className="setting-val">{display}</span>
      </span>
      <div className="slider-row">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={v}
          onChange={(e) => onChange(Number(e.target.value))}
          onWheel={(e) => e.currentTarget.blur()}
        />
        <input
          className="slider-num"
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
        />
      </div>
      {hint && <span className="setting-hint muted small">{hint}</span>}
    </label>
  )
}
