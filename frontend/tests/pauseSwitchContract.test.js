import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const readSource = (path) => readFile(new URL(path, import.meta.url), 'utf8')

// The pause switch is an invisible checkbox stretched over its track. The track
// comes after it and is positioned too, so at the same z-index it paints on top
// and takes every tap: from 2026-10-20 to 2026-10-23 a wallet could not be
// paused by tapping the switch.
test('the pause switch checkbox sits above its track and takes the tap', async () => {
  const css = await readSource('../src/styles/brutalism.css')
  const input = css.match(/\.switch input\s*\{([\s\S]*?)\}/)?.[1] || ''
  const track = css.match(/\.switch-track\s*\{([\s\S]*?)\}/)?.[1] || ''

  assert.match(input, /position:\s*absolute/)
  assert.match(input, /inset:\s*0/)
  const inputZ = Number(input.match(/z-index:\s*(\d+)/)?.[1])
  const trackZ = Number(track.match(/z-index:\s*(\d+)/)?.[1] || 0)
  assert.ok(inputZ > trackZ, `the switch input needs a z-index above the track's ${trackZ}, received ${inputZ}`)

  const card = await readSource('../src/components/WalletRiskCard.jsx')
  assert.match(card, /<span className="switch">\s*<input[\s\S]*?onChange=\{toggleEnabled\}/)
})
