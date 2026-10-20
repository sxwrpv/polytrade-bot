import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile, stat } from 'node:fs/promises'

const root = new URL('../', import.meta.url)
const read = (path) => readFile(new URL(path, root), 'utf8')

test('public product loads the documentation typography and supplied PolyTrade mark', async () => {
  const [index, css, home] = await Promise.all([
    read('index.html'),
    read('src/styles/brutalism.css'),
    read('src/pages/PublicHome.jsx'),
  ])

  assert.match(index, /family=Instrument\+Serif/)
  assert.match(index, /href="\/brand\/polytrade-mark\.png"/)
  // The social preview is a 1200×630 card built around the mark (changed on
  // 2026-10-20): link previews cropped and upscaled the bare 256px square.
  // The mark itself stays the icon above, and the card must ship with the build.
  assert.match(index, /property="og:image" content="https:\/\/polytradebot\.live\/brand\/og-image\.png"/)
  assert.match(index, /name="twitter:card" content="summary_large_image"/)
  assert.ok((await stat(new URL('public/brand/og-image.png', root))).size > 10_000)
  assert.match(css, /--serif:\s*'Instrument Serif'/)
  assert.match(css, /\.public-site h1, \.public-site h2[^}]*font-family:\s*var\(--serif\)/s)
  assert.match(home, /src="\/brand\/polytrade-mark\.png"/)
  assert.doesNotMatch(home, /className="brand-mark">P</)
})

test('onboarding and the authenticated app header use the supplied mark', async () => {
  const [app, onboarding, legacy] = await Promise.all([
    read('src/App.jsx'),
    read('src/pages/Onboarding.jsx'),
    read('src/pages/LegacyLink.jsx'),
  ])

  const header = app.slice(
    app.indexOf('<header className="app-header">'),
    app.indexOf('</header>', app.indexOf('<header className="app-header">')),
  )
  assert.match(header, /src="\/brand\/polytrade-mark\.png"/)
  assert.match(header, /className="app-brand"/)
  for (const source of [app, onboarding, legacy]) {
    assert.match(source, /src="\/brand\/polytrade-mark\.png"/)
    assert.doesNotMatch(source, /<div className="logo"><span>P<\/span>/)
  }
})
