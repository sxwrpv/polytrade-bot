import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const appSource = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')

test('authenticated app header shows the wordmark without the P logo tile', () => {
  assert.match(appSource, /<header className="app-header">POLYTRADE<\/header>/)
  assert.doesNotMatch(appSource, /<header className="app-header"><span className="brand-mark mini">P<\/span>/)
})
