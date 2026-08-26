import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { contrast } from './landingDesignSystem.test.js'

const root = new URL('../', import.meta.url)
const read = (path) => readFile(new URL(path, root), 'utf8')

const token = (css, name) => {
  const match = css.match(new RegExp(`--${name}:\\s*([^;]+);`))
  assert.ok(match, `missing token --${name}`)
  return match[1].trim()
}

test('the three green roles are exactly the specified palette', async () => {
  const css = await read('src/styles/brutalism.css')

  // Main accent and border.
  assert.equal(token(css, 'green'), '#0b9e63')
  // Darker green text.
  assert.equal(token(css, 'green-text'), '#087a4b')
  // Especially contrasty green text.
  assert.equal(token(css, 'green-deep'), '#07603c')
})

test('the screener page carries the Mini App green gradient', async () => {
  const css = await read('src/styles/screener.css')

  const shell = css.match(/\.screener\s*{[^}]*}/s)[0]
  assert.match(shell, /radial-gradient/)
  // The Mini App's mint, not a new colour invented here.
  assert.match(shell, /61,\s*220,\s*132/)
  assert.equal((shell.match(/radial-gradient/g) || []).length, 3)
  // Fixed, so the wash does not slide around while reading a long table.
  assert.match(shell, /background-attachment:\s*fixed/)
})

test('green reads light: tinted pills, not solid slabs of fill', async () => {
  const css = await read('src/styles/screener.css')

  // Period/sort controls and the row action are tinted pills whose label is
  // the high-contrast green, which is what makes them read as light.
  for (const selector of ['.segmented button.active', '.btn-analyze']) {
    const rule = css.match(new RegExp(`\\${selector}\\s*{[^}]*}`, 's'))
    assert.ok(rule, `missing rule for ${selector}`)
    assert.match(rule[0], /var\(--green-deep\)/, `${selector} should use the deep green label`)
    assert.doesNotMatch(
      rule[0], /background:\s*var\(--green\)\s*;/,
      `${selector} should be tinted, not a solid green slab`,
    )
  }
  // Pill geometry, matching the Mini App's chips.
  assert.match(css, /\.segmented button[^}]*border-radius:\s*999px/s)
})

test('tinted controls keep their label above AA over the tint', async () => {
  const [tokens, screener] = await Promise.all([
    read('src/styles/brutalism.css'),
    read('src/styles/screener.css'),
  ])

  const tint = screener.match(/--pill-tint:\s*rgba\(([^)]+)\)/)
  assert.ok(tint, 'missing --pill-tint')
  const [r, g, b, alpha] = tint[1].split(',').map((n) => Number(n.trim()))
  const paper = [0xee, 0xf2, 0xef]
  const blended = `#${[r, g, b]
    .map((channel, index) => Math.round(paper[index] + (channel - paper[index]) * alpha))
    .map((n) => n.toString(16).padStart(2, '0'))
    .join('')}`

  const deep = token(tokens, 'green-deep')
  const ratio = contrast(deep, blended)
  assert.ok(ratio >= 4.5, `deep green on the pill tint is ${ratio.toFixed(2)}:1`)
})

test('numeric filters use an explicit enable control so exact boundaries remain usable', async () => {
  const [page, slider] = await Promise.all([
    read('src/screener/ScreenerPage.jsx'),
    read('src/screener/RangeFilter.jsx'),
  ])

  assert.match(slider, /type="range"/)
  assert.match(page, /<RangeFilter/)
  // No numeric text inputs left anywhere in the filter rail.
  assert.doesNotMatch(page, /type="number"/)
  assert.doesNotMatch(slider, /type="number"/)
  // Enabled state is separate from the numeric value, so 0 and exact slider
  // endpoints can be active thresholds rather than overloaded off sentinels.
  assert.match(slider, /type="checkbox"/)
  assert.match(slider, /aria-label={`\$\{label\} filter enabled`}/)
  assert.match(slider, /type="checkbox"[\s\S]*type="range"/)
  assert.match(slider, /aria-valuetext/)
  assert.match(slider, /disabled={!active}/)
  assert.doesNotMatch(slider, /next === offValue \? ''/)
  assert.match(slider, /'off'/)
})

test('Copy Score band checkboxes cannot inherit the global full-width input rule', async () => {
  const css = await read('src/styles/screener.css')
  const rule = css.match(/\.band-check input\s*{[^}]*}/s)

  assert.ok(rule, 'missing band checkbox rule')
  assert.match(rule[0], /width:\s*14px/)
  assert.match(rule[0], /height:\s*14px/)
})

test('the notes rail detaches before a 1280px desktop squeezes the Analyze action', async () => {
  const css = await read('src/styles/screener.css')

  assert.match(css, /@media \(max-width:\s*1400px\)/)
})

test('the screener uses the documentation shell layout', async () => {
  const [page, css] = await Promise.all([
    read('src/screener/ScreenerPage.jsx'),
    read('src/styles/screener.css'),
  ])

  // Sidebar + content column + right rail, like /docs.
  assert.match(page, /screener-sidebar/)
  assert.match(page, /screener-rail/)
  assert.match(css, /\.screener-shell\s*{[^}]*grid-template-columns/s)
  // The sidebar is real navigation furniture, so it is labelled.
  assert.match(page, /<aside[^>]*aria-label/)
})

test('no control flips to paper-on-green, which is only 3.05:1', async () => {
  const [tokens, css] = await Promise.all([
    read('src/styles/brutalism.css'),
    read('src/styles/screener.css'),
  ])
  const green = token(tokens, 'green')
  const paper = token(tokens, 'bg')
  assert.ok(contrast(paper, green) < 4.5, 'premise: paper on green fails AA')

  // Any rule that paints the green background must not also set the paper
  // colour on top of it.
  for (const rule of css.split('}')) {
    if (!/background:\s*var\(--green\)\s*;/.test(rule)) continue
    assert.doesNotMatch(
      rule, /color:\s*var\(--bg\)/,
      `"${rule.split('{')[0].trim()}" puts paper text on a solid green fill`,
    )
  }
})
