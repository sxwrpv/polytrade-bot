// Compact green/red/gray dot visualization — the "how many green days vs red
// days" consistency concept made visible at a glance. `values` is an array of
// numbers: >0 renders green, <0 renders red, 0 renders neutral gray.
export default function DotStrip({ values = [], titles = [], max = 30 }) {
  const shown = values.slice(0, max)
  if (shown.length === 0) return <span className="muted small">no history yet</span>
  return (
    <div className="dot-strip">
      {shown.map((v, i) => (
        <span
          key={i}
          className={`dot ${v > 0 ? 'dot-green' : v < 0 ? 'dot-red' : 'dot-flat'}`}
          title={titles[i]}
        />
      ))}
    </div>
  )
}
