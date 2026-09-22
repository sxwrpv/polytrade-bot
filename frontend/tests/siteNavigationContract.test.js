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

test('home places the shared switcher in the center of its header', async () => {
  const home = await read('src/pages/PublicHome.jsx')

  assert.match(home, /<SiteSwitcher active="home" \/>/)
})

/* The screener's own header layout is no longer contracted here. That test
   read src/screener/ScreenerPage.jsx and src/styles/screener.css, which this
   app no longer ships — /screener is the dedicated trader-screener service.
   Its header is that service's to cover; see trader-screener/tests. The
   switcher's own destinations are still pinned above. */

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
