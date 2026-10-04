import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const appSource = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')

test('authenticated app header carries the supplied logo and PolyTrade wordmark', () => {
  const header = appSource.slice(
    appSource.indexOf('<header className="app-header">'),
    appSource.indexOf('</header>', appSource.indexOf('<header className="app-header">')),
  )

  assert.match(header, /src="\/brand\/polytrade-mark\.png"/)
  assert.match(header, /PolyTrade/)
  assert.match(header, /className="app-brand"/)
  assert.doesNotMatch(header, /<span className="brand-mark mini">P<\/span>/)
})
