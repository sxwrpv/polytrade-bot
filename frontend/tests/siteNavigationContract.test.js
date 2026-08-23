import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const root = new URL('../', import.meta.url)
const read = (path) => readFile(new URL(path, root), 'utf8')

test('the shared public switcher links Home, Screener and Docs with one active destination', async () => {
  const switcher = await read('src/components/SiteSwitcher.jsx')

  assert.match(switcher, /aria-label="PolyTrade sites"/)
  assert.match(switcher, /\['home', '\/', 'Home'\]/)
  assert.match(switcher, /\['screener', '\/screener', 'Screener'\]/)
  assert.match(switcher, /\['docs', '\/docs', 'Docs'\]/)
  assert.match(switcher, /aria-current=\{active === key \? 'page' : undefined\}/)
})

test('home and screener place the same switcher in the center of their headers', async () => {
  const [home, screener] = await Promise.all([
    read('src/pages/PublicHome.jsx'),
    read('src/screener/ScreenerPage.jsx'),
  ])

  assert.match(home, /<SiteSwitcher active="home" \/>/)
  assert.match(screener, /<SiteSwitcher active="screener" \/>/)
})

test('screener centers the switcher in a three-track header and stacks safely before tablet widths', async () => {
  const [page, css] = await Promise.all([
    read('src/screener/ScreenerPage.jsx'),
    read('src/styles/screener.css'),
  ])

  assert.match(page, /className="screener-nav-primary"/)
  const desktop = css.match(/\.screener-nav\s*{[^}]*}/s)
  assert.ok(desktop)
  assert.match(desktop[0], /display:\s*grid/)
  assert.match(desktop[0], /grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto\s+minmax\(0,\s*1fr\)/)
  assert.match(css, /\.screener-nav\s*>\s*\.site-switcher\s*{[^}]*justify-self:\s*center/s)

  const tablet = css.slice(css.indexOf('@media (max-width: 820px)'))
  assert.match(tablet, /\.screener-nav-primary\s*{[^}]*display:\s*contents/s)
  assert.match(tablet, /\.screener-nav\s*>\s*\.site-switcher\s*{[^}]*grid-row:\s*2/s)
  assert.match(tablet, /\.screener-search\s*{[^}]*grid-row:\s*3/s)
})

test('the switcher is a restrained liquid-glass capsule like the Saved control', async () => {
  const css = await read('src/styles/brutalism.css')
  const rule = css.match(/\.site-switcher\s*{[^}]*}/s)
  const active = css.match(/\.site-switcher a\[aria-current='page'\]\s*{[^}]*}/s)

  assert.ok(rule, 'missing .site-switcher')
  assert.match(rule[0], /border-radius:\s*999px/)
  assert.match(rule[0], /backdrop-filter:\s*blur\(/)
  assert.match(rule[0], /background:\s*rgba\(/)
  assert.match(rule[0], /box-shadow:/)
  assert.ok(active, 'missing active liquid-glass destination')
  assert.match(active[0], /var\(--green-deep\)/)
  assert.doesNotMatch(active[0], /background:\s*var\(--green\)\s*;/)
})
