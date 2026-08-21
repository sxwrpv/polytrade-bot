import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const root = new URL('../', import.meta.url)
const read = (path) => readFile(new URL(path, root), 'utf8')

/* ---------- WCAG contrast, computed rather than assumed --------------------
   The brief suggested ink-on-green and warned against white-on-green. Both are
   checked here against the real formula so the palette cannot drift back. */
const channel = (value) => {
  const c = value / 255
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

export const luminance = (hex) => {
  const h = hex.replace('#', '')
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16))
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

export const contrast = (a, b) => {
  const [x, y] = [luminance(a), luminance(b)]
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)
}

/** Read a `--name: value;` custom property out of a stylesheet's :root block. */
const token = (css, name) => {
  const match = css.match(new RegExp(`--${name}:\\s*([^;]+);`))
  assert.ok(match, `missing token --${name}`)
  return match[1].trim()
}

test('contrast helper agrees with the published WCAG reference values', () => {
  assert.equal(Math.round(contrast('#ffffff', '#000000')), 21)
  assert.ok(Math.abs(contrast('#14201a', '#0b9e63') - 4.86) < 0.02)
})

test('product tokens are the documentation palette, not the neon glass palette', async () => {
  const css = await read('src/styles/brutalism.css')

  assert.equal(token(css, 'bg'), '#eef2ef')
  assert.equal(token(css, 'text'), '#14201a')
  assert.equal(token(css, 'muted'), '#5c6b62')
  assert.equal(token(css, 'green'), '#0b9e63')
  assert.equal(token(css, 'border'), 'rgba(20, 32, 26, 0.12)')

  // The neon mint primary fill is gone from every stylesheet and every page.
  for (const path of [
    'src/styles/brutalism.css',
    'src/styles/public-landing.css',
    'src/pages/PublicHome.jsx',
  ]) {
    const source = await read(path)
    assert.doesNotMatch(source, /#3ddc84/i, `${path} still uses the neon mint fill`)
    assert.doesNotMatch(source, /61,\s*220,\s*132/, `${path} still uses the neon mint fill`)
  }
})

test('every foreground/background pair in the token contract meets WCAG AA', async () => {
  const css = await read('src/styles/brutalism.css')
  const t = (name) => token(css, name)

  const pairs = [
    ['text on paper', t('text'), t('bg')],
    ['muted on paper', t('muted'), t('bg')],
    // Small green text uses the darker green — #0b9e63 on paper is only 3.05.
    ['green text on paper', t('green-text'), t('bg')],
    ['deep green on paper', t('green-deep'), t('bg')],
    // The label on a solid green button. Dark ink, not white: white on
    // #0b9e63 is 3.45 and fails AA for normal text.
    ['button label on green fill', t('green-ink'), t('green')],
    ['inverse label on ink', t('bg'), t('text')],
  ]

  for (const [name, fg, bg] of pairs) {
    const ratio = contrast(fg, bg)
    assert.ok(ratio >= 4.5, `${name}: ${fg} on ${bg} is ${ratio.toFixed(2)}:1, below AA 4.5`)
  }

  // The mistake the brief called out explicitly, locked out by measurement.
  assert.ok(contrast('#ffffff', t('green')) < 4.5)
})

test('landing typography assigns one typeface per role', async () => {
  const css = await read('src/styles/brutalism.css')
  const landing = await read('src/styles/public-landing.css')

  // Instrument Serif: editorial display headings only.
  assert.match(css, /\.public-site h1,\s*\.public-site h2[^}]*font-family:\s*var\(--serif\)/s)
  // Inter carries body, navigation and controls; it is the body default.
  assert.match(css, /body\s*{[^}]*font-family:\s*'Inter'/s)
  // JetBrains Mono is reserved for technical labels, timings and metrics —
  // never for a dense product control or a paragraph of prose.
  assert.match(css, /\.eyebrow\s*{[^}]*font-family:\s*var\(--mono\)/s)
  assert.match(landing, /\.speed-metric[^}]*var\(--mono\)/s)
  assert.match(landing, /\.demo-time[^}]*var\(--mono\)/s)
  for (const prose of ['hero-lede', 'speed-head p', 'feature-copy p', 'benefits-lede']) {
    const rule = (css + landing).match(new RegExp(`\\.${prose}\\s*{[^}]*}`, 's'))
    assert.ok(rule, `missing rule for .${prose}`)
    assert.doesNotMatch(rule[0], /var\(--mono\)/, `.${prose} must not be set in mono`)
  }
})

test('the hero keeps exactly one primary Telegram call to action', async () => {
  const home = await read('src/pages/PublicHome.jsx')
  const hero = home.slice(home.indexOf('public-hero'), home.indexOf('id="speed"'))

  assert.equal((hero.match(/public-primary/g) || []).length, 1)
  assert.match(hero, /href=\{BOT_URL\}/)
})

test('landing explains the product with diagrams, not more generic cards', async () => {
  const home = await read('src/pages/PublicHome.jsx')

  // The three-icon benefit grid is replaced by a diagram simplified from the
  // published System Design set.
  assert.doesNotMatch(home, /benefit-icon/)
  assert.match(home, /<svg/)
  assert.match(home, /<title id=/)
  assert.match(home, /<desc id=/)
  assert.match(home, /\/docs\/system-design/)
})

test('public landing gives the standalone Wallet Screener its own discoverable section', async () => {
  const home = await read('src/pages/PublicHome.jsx')
  const start = home.indexOf('id="wallet-screener"')
  const end = home.indexOf('</section>', start)

  assert.ok(start > 0, 'missing dedicated Wallet Screener section')
  const section = home.slice(start, end)
  assert.match(section, /href="\/screener"/)
  assert.match(section, /Open Wallet Screener/)
  assert.match(section, /public history/i)

  const nav = home.slice(home.indexOf('public-nav'), home.indexOf('</header>'))
  assert.match(nav, /href="\/screener"/)
  assert.doesNotMatch(section, /INSIDE THE APP/)
})

test('public landing makes the full System Design documentation easy to discover', async () => {
  const home = await read('src/pages/PublicHome.jsx')
  const nav = home.slice(home.indexOf('public-nav'), home.indexOf('</header>'))
  const pipeline = home.slice(home.indexOf('id="how-it-works"'), home.indexOf('</section>', home.indexOf('id="how-it-works"')))

  assert.match(nav, /href="\/docs\/system-design"[^>]*>System Design/)
  assert.match(pipeline, /href="\/docs\/system-design"/)
  assert.match(pipeline, /Explore full System Design/)
  assert.match(pipeline, /seven architecture diagrams/i)
})

test('landing keeps the factual, risk-aware copy and illustrative labelling', async () => {
  const home = await read('src/pages/PublicHome.jsx')

  for (const phrase of [
    'Copy trading involves loss risk and is not financial advice.',
    'Real-money risk',
    'Custodial wallet',
    'Funding and withdrawals',
    'Eligibility is your responsibility',
  ]) {
    assert.ok(home.includes(phrase), `missing risk copy: ${phrase}`)
  }
  // Nothing on the page may read as live market data.
  assert.match(home, /ILLUSTRATIVE/)
  assert.doesNotMatch(home, /demo-live/)
})

test('landing motion stays subtle and reduced-motion safe', async () => {
  const landing = await read('src/styles/public-landing.css')

  assert.match(landing, /@media \(prefers-reduced-motion: reduce\)/)
  // No perpetual animation: a looping pulse reads as live system status.
  assert.doesNotMatch(landing, /infinite/)
})

test('flat paper surfaces replace the glass card treatment', async () => {
  const landing = await read('src/styles/public-landing.css')
  const css = await read('src/styles/brutalism.css')

  assert.doesNotMatch(landing, /backdrop-filter/)
  // Heavy drop shadows give way to hairline rules.
  assert.doesNotMatch(landing, /box-shadow:\s*0 \d\d+px/)
  assert.doesNotMatch(css, /radial-gradient/)
})

test('diagrams render complete when the reveal script never runs', async () => {
  const landing = await read('src/styles/public-landing.css')

  // Anything that starts hidden must be gated on the `js-reveal` root class,
  // which only the page script sets. Otherwise a no-JS visitor — or one whose
  // observer never fires — sees an empty frame where the diagram should be.
  for (const rule of landing.split('}')) {
    if (!/opacity:\s*0\b|stroke-dashoffset:\s*var\(--len/.test(rule)) continue
    const selector = rule.split('{')[0].trim()
    if (!selector || selector.startsWith('@') || selector.startsWith('/*')) continue
    assert.match(
      selector, /\.js-reveal\b/,
      `"${selector}" hides content without the js-reveal gate`,
    )
  }
})

test('--faint never sets text: it cannot reach AA on paper', async () => {
  const sheets = await Promise.all([
    read('src/styles/brutalism.css'),
    read('src/styles/public-landing.css'),
    read('src/styles/screener.css'),
  ])
  const css = sheets.join('\n')

  // 3.21:1 against #eef2ef — fine for a rule or a marker, never for type.
  const faint = css.match(/--faint:\s*([^;]+);/)
  assert.ok(faint)
  assert.ok(contrast(faint[1].trim(), '#eef2ef') < 4.5)
  assert.doesNotMatch(css, /color:\s*var\(--faint\)/)
})

test('the selected screener row keeps its text above AA', async () => {
  const [tokens, screener] = await Promise.all([
    read('src/styles/brutalism.css'),
    read('src/styles/screener.css'),
  ])

  // Composite the row wash over paper and check the muted text on top of it.
  const wash = tokens.match(/--green-wash:\s*rgba\(([^)]+)\)/)
  assert.ok(wash, 'missing --green-wash')
  const [r, g, b, a] = wash[1].split(',').map((n) => Number(n.trim()))
  const paper = [0xee, 0xf2, 0xef]
  const blended = [r, g, b]
    .map((channel, index) => Math.round(paper[index] + (channel - paper[index]) * a))
    .map((n) => n.toString(16).padStart(2, '0'))
    .join('')
  const muted = tokens.match(/--muted:\s*([^;]+);/)[1].trim()
  assert.ok(
    contrast(muted, `#${blended}`) >= 4.5,
    `muted on the selected row is ${contrast(muted, `#${blended}`).toFixed(2)}:1`,
  )
  // Selection is not signalled by the tint alone.
  assert.match(screener, /tr\.selected > th[^}]*box-shadow/)
})
