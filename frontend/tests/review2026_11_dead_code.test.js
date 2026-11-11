import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'

const root = new URL('../', import.meta.url)
const read = (path) => readFileSync(new URL(path, root), 'utf8')
const app = read('src/App.jsx')

// Everything a removed name could still be referenced from.
function sources(dir = 'src') {
  return readdirSync(new URL(`${dir}/`, root), { withFileTypes: true }).flatMap((entry) => (
    entry.isDirectory() ? sources(`${dir}/${entry.name}`) : [`${dir}/${entry.name}`]
  ))
}
const everything = [...sources(), 'index.html'].map((path) => [path, read(path)])

test('the open tab lives in state and the URL hash is left untouched', () => {
  for (const [path, text] of everything) {
    assert.doesNotMatch(text, /location\.hash|hashchange/, path)
  }
  assert.match(app, /const \[tab, setTab\] = useState\('home'\)/)
})

test('the stylesheet rules removed as unused have no user left', () => {
  const css = read('src/styles/brutalism.css')
  for (const gone of ['.neutral', '.sort-row', '.toggle-row', '.chip', '.pc-row', '.funding-note', '.center-link', '--ease-exit']) {
    assert.ok(!css.includes(gone), `${gone} is back in brutalism.css`)
  }
  // The names as markup would carry them: a class on a className or classList
  // line, a quoted value handed to one, or a name built from parts.
  const name = '(neutral|sort-row|toggle-row|chip|chip-icon|pc-row|funding-note|center-link)'
  const onClassLine = new RegExp(`(?<![\\w-])${name}(?![\\w-])`)
  const quoted = new RegExp(`['"\`]${name}['"\`]`)
  const built = /(pc|funding|center|sort|toggle|chip)-\$\{|\$\{[^}]*\}-(row|note|link|icon)\b/
  for (const [path, text] of everything) {
    if (path.endsWith('.css')) {
      assert.doesNotMatch(text, /var\(--ease-exit\)/, path)
      continue
    }
    const classLines = text.split('\n').filter((line) => /className|classList/.test(line)).join('\n')
    assert.doesNotMatch(classLines, onClassLine, path)
    assert.doesNotMatch(text, quoted, path)
    assert.doesNotMatch(text, built, path)
  }
})
