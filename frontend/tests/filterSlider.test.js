/* FilterSlider is retained but currently unmounted.
 *
 * Its only consumer was the in-app WalletScreener, removed when the standalone
 * /screener took over. This test moved here from walletScreenerModel.test.js so
 * that deleting the screener's suite did not also drop the component's only
 * coverage. See the note at the top of FilterSlider.jsx.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const readSource = (path) => readFile(new URL(path, import.meta.url), 'utf8')

test('FilterSlider keeps text state but gives range input a finite fallback', async () => {
  const source = await readSource('../src/components/FilterSlider.jsx')
  assert.match(source, /Number\.isFinite/)
  assert.match(source, /value=\{value\}/)
})
