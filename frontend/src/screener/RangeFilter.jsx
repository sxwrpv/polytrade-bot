/* One numeric screener filter as a slider.
 *
 * The slider parked at its "off" end means no filter at all — mirrored as the
 * empty string the query builder already treats as inactive. That distinction
 * matters here: a threshold of zero is a real filter that hides every wallet
 * below zero, which is not what an untouched control should do.
 *
 * The value is echoed as text beside the label, so the reader can always see
 * what the handle is set to without dragging it.
 */
export default function RangeFilter({
  id, label, value, onChange, min, max, step = 1, off = 'min', format,
}) {
  const offValue = off === 'min' ? min : max
  const numeric = Number(value)
  const active = value !== '' && value != null && Number.isFinite(numeric)
  const position = active ? Math.min(max, Math.max(min, numeric)) : offValue

  const handle = (event) => {
    const next = Number(event.target.value)
    onChange(next === offValue ? '' : String(next))
  }

  return (
    <div className={`range-filter${active ? ' is-active' : ''}`}>
      <label htmlFor={id}>
        <span>{label}</span>
        <b>{active ? (format ? format(numeric) : numeric) : 'off'}</b>
      </label>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={position}
        onChange={handle}
        // Scrolling the page must never drag a filter: blur so a wheel gesture
        // passing over the track cannot change the value.
        onWheel={(event) => event.currentTarget.blur()}
      />
    </div>
  )
}
