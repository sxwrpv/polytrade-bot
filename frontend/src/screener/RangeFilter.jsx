/* One numeric screener filter as a slider with explicit activation.
 *
 * Enabled state is separate from the numeric value. This keeps every value in
 * the slider domain usable — including zero and exact min/max boundaries —
 * while the empty string continues to mean that no threshold is applied.
 */
export default function RangeFilter({
  id, label, value, onChange, min, max, step = 1, off = 'min', format,
}) {
  const defaultValue = off === 'min' ? min : max
  const numeric = Number(value)
  const active = value !== '' && value != null && Number.isFinite(numeric)
  const position = active ? Math.min(max, Math.max(min, numeric)) : defaultValue
  const renderedValue = active ? (format ? format(numeric) : String(numeric)) : 'off'
  const toggleId = `${id}-enabled`

  const handle = (event) => onChange(String(Number(event.target.value)))
  const toggle = (event) => onChange(event.target.checked ? String(position) : '')

  return (
    <div className={`range-filter${active ? ' is-active' : ''}`}>
      <div className="range-filter-head">
        <label htmlFor={id}>{label}</label>
        <b>{renderedValue}</b>
      </div>
      <div className="range-filter-controls">
        <label className="range-filter-toggle" htmlFor={toggleId}>
          <input
            id={toggleId}
            type="checkbox"
            checked={active}
            aria-label={`${label} filter enabled`}
            onChange={toggle}
          />
          Apply
        </label>
        <input
          id={id}
          type="range"
          min={min}
          max={max}
          step={step}
          value={position}
          disabled={!active}
          aria-valuetext={active ? renderedValue : 'Filter off'}
          onChange={handle}
          // Scrolling the page must never drag a filter: blur so a wheel gesture
          // passing over the track cannot change the value.
          onWheel={(event) => event.currentTarget.blur()}
        />
      </div>
    </div>
  )
}
