import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const root = new URL('../', import.meta.url)
const read = (path) => readFileSync(new URL(path, root), 'utf8')
const app = read('src/App.jsx')

test('the authenticated pages render inside an error boundary', () => {
  const boundary = app.slice(app.indexOf('class PageBoundary extends Component {'), app.indexOf('function selectionHaptic() {'))
  assert.match(boundary, /static getDerivedStateFromError\(\) \{ return \{ failed: true \} \}/)
  assert.match(boundary, /if \(!this\.state\.failed\) return this\.props\.children/)

  // Inside <main key={tab}>: the header and the tab bar stay mounted when a
  // page fails, and a tab change remounts the boundary with a clean state.
  const main = app.slice(app.indexOf('<main className="content" key={tab}>'), app.indexOf('</main>'))
  assert.match(main, /^<main className="content" key=\{tab\}>\s*<PageBoundary>/)
  assert.match(main, /<\/PageBoundary>\s*$/)
  for (const page of ['<Home />', '<Positions onNavigate={goTo} />', '<User onLogout=']) {
    assert.ok(main.includes(page), page)
  }
  assert.ok(app.indexOf('<nav className="tab-bar"') > app.indexOf('</main>'))
})

test('the fallback is the shared error state with a reload action', () => {
  const boundary = app.slice(app.indexOf('class PageBoundary extends Component {'), app.indexOf('function selectionHaptic() {'))
  assert.match(app, /import \{ StateBlock \} from '\.\/components\/UiState'/)
  assert.match(boundary, /<StateBlock\s+icon="alert"\s+tone="error"\s+title="This page didn’t load"/)
  assert.match(boundary, /className="btn btn-ghost" onClick=\{\(\) => window\.location\.reload\(\)\}/)
  assert.match(boundary, /<Icon name="refresh" \/>Reload/)
  // Built from what the stylesheet already has.
  const css = read('src/styles/brutalism.css')
  for (const selector of ['.state {', '.state.is-error .state-icon', '.btn-ghost']) {
    assert.ok(css.includes(selector), selector)
  }
  assert.doesNotMatch(boundary, /style=/)
})

test('the homepage chunk keeps its own boundary', () => {
  assert.match(app, /<ChunkBoundary>\s*<Suspense fallback=\{<LaunchLoading \/>\}><PublicHome \/><\/Suspense>\s*<\/ChunkBoundary>/)
})
