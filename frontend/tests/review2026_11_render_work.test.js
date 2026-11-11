import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')

// The homepage cadence timeline's own scroll update, run against a stand-in
// figure that counts every write to the page.
const home = read('../src/pages/PublicHome.jsx')
const scrub = home.slice(home.indexOf('    let raf = 0'), home.indexOf('    const onScroll = '))

function timeline() {
  const page = { top: 2000, height: 400, writes: 0 }
  const classes = () => {
    const on = new Set()
    return { on, toggle(name, force) { page.writes += 1; if (force) on.add(name); else on.delete(name) } }
  }
  const label = { text: '0.0s' }
  const head = (axis) => ({
    dataset: { axis },
    transform: null,
    setAttribute(_name, value) { page.writes += 1; this.transform = value },
    querySelector: () => ({ set textContent(value) { page.writes += 1; label.text = value } }),
  })
  const figure = { classList: classes(), getBoundingClientRect: () => ({ top: page.top, height: page.height }) }
  const events = [0.6, 2, 5, 11].map((at) => [{ classList: classes() }, at])
  const heads = [head('x'), head('y')]
  const update = new Function('figure', 'events', 'heads', 'window', `${scrub}; return update`)(
    figure, events, heads, { innerHeight: 800 })
  return { page, figure, events, heads, label, update }
}

test('scroll frames away from the cadence figure write nothing to the page', () => {
  const t = timeline()
  t.update()                       // first paint, figure far below the screen
  const first = t.page.writes
  assert.ok(first > 0)
  for (const top of [1900, 1500, 1200]) { t.page.top = top; t.update() }
  assert.equal(t.page.writes, first)

  t.page.top = -5000               // long past it: one frame draws the end state
  t.update()
  const done = t.page.writes
  assert.ok(done > first)
  assert.equal(t.figure.classList.on.has('is-done'), true)
  for (const top of [-5200, -6000]) { t.page.top = top; t.update() }
  assert.equal(t.page.writes, done)
})

test('scrolling across the figure still plays the timeline', () => {
  const t = timeline()
  t.update()
  // vh 800: progress runs from top = 680 over 0.3 × 800 + 400 = 640px.
  t.page.top = 680 - 320           // halfway: 7.7s
  t.update()
  assert.equal(t.label.text, '7.7s')
  assert.equal(t.heads[0].transform, 'translate(369.6 0)')
  assert.equal(t.heads[1].transform, 'translate(0 231.0)')
  assert.deepEqual(t.events.map(([node]) => node.classList.on.has('on')), [true, true, true, false])
  assert.equal(t.figure.classList.on.has('is-done'), false)

  const before = t.page.writes
  t.update()                       // a resize or a second frame at the same spot
  assert.equal(t.page.writes, before)

  t.page.top = 680 - 640
  t.update()
  assert.equal(t.label.text, '15.0s')
  assert.deepEqual(t.events.map(([node]) => node.classList.on.has('on')), [true, true, true, true])
  assert.equal(t.figure.classList.on.has('is-done'), true)
})

// The balance chart's geometry, cut out of its useMemo.
const card = read('../src/components/BalanceCard.jsx')
const memo = card.slice(card.indexOf('  const geom = useMemo(() => {'), card.indexOf('  const cursor = '))

function geometry(points) {
  let deps = null
  const geom = new Function('useMemo', 'points', 'H', 'PAD', `${memo}; return geom`)(
    (build, list) => { deps = list; return build() }, points, 86, 3)
  return { geom, deps }
}

test('the balance curve is built once per series, with the chart geometry', () => {
  const points = [{ t: 1, y: 100 }, { t: 2, y: 150 }, { t: 3, y: 125 }]
  const { geom, deps } = geometry(points)
  // 0..100 across; the lowest value sits 3 above the bottom edge of 86, the
  // highest 3 below the top.
  assert.equal(geom.line, '0,83 50,3 100,43')
  assert.equal(geom.rising, true)
  assert.deepEqual(deps, [points])
  assert.equal(geom.line, points.map((p, i) => `${geom.x(i)},${geom.y(p.y)}`).join(' '))
})

test('a flat or falling series still draws inside the plot', () => {
  const flat = geometry([{ t: 1, y: 40 }, { t: 2, y: 40 }]).geom
  assert.equal(flat.line, '0,83 100,83')
  const falling = geometry([{ t: 1, y: 9 }, { t: 2, y: 5 }]).geom
  assert.equal(falling.line, '0,3 100,83')
  assert.equal(falling.rising, false)
  assert.equal(geometry([{ t: 1, y: 40 }]).geom, null)
})

test('hovering reuses the memoised curve for both shapes', () => {
  const svg = card.slice(card.indexOf('<svg'), card.indexOf('</svg>'))
  assert.match(svg, /<polygon\s+points=\{`0,\$\{H\} \$\{geom\.line\} 100,\$\{H\}`\}/)
  assert.match(svg, /<polyline\s+points=\{geom\.line\}/)
  assert.doesNotMatch(svg, /points\.map\(/)
})
