// Slider + numeric input pair for one screener filter. The slider parked at
// its "off" end (min for ≥ filters, max for ≤ filters) means "no filter" —
// mirrored as the empty-string value the screener already treats as off. The
// numeric input stays editable for values outside the slider's range.
export default function FilterSlider({
  label, value, onChange, min, max, step = 1, off = 'min', placeholder = '',
}) {
  const offValue = off === 'min' ? min : max
  const sliderValue = value === '' || value == null ? offValue : Number(value)

  function fromSlider(e) {
    const v = Number(e.target.value)
    onChange(v === offValue ? '' : String(v))
  }

  return (
    <label className="fld fld-slider">
      {label}
      <div className="slider-row">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={sliderValue}
          onChange={fromSlider}
          // scrolling the page must never drag a filter — blur so wheel/scroll
          // gestures passing over the track can't change the value
          onWheel={(e) => e.currentTarget.blur()}
        />
        <input
          className="slider-num"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
        />
      </div>
    </label>
  )
}
