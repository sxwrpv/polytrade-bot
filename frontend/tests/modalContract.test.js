import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const readSource = (path) => readFile(new URL(path, import.meta.url), 'utf8')

test('Modal is an SSR-guarded document-root portal with a uniquely labelled dialog', async () => {
  const source = await readSource('../src/components/Modal.jsx')

  assert.match(source, /import\s*\{\s*createPortal\s*\}\s*from\s*['"]react-dom['"]/)
  assert.match(source, /typeof document\s*!==\s*['"]undefined['"]/)
  assert.match(source, /if\s*\(!portalTarget\)\s*return null/)
  assert.match(source, /createPortal\s*\([\s\S]*portalTarget\s*,?\s*\)/)
  assert.match(source, /useId\s*\(/)
  assert.match(source, /role=["']dialog["']/)
  assert.match(source, /aria-modal=["']true["']/)
  assert.match(source, /aria-labelledby=\{titleId\}/)
  assert.match(source, /id=\{titleId\}/)
})

test('Modal delegates shared stack, body-lock, Escape, and restoration policy', async () => {
  const source = await readSource('../src/components/Modal.jsx')
  const manager = await readSource('../src/components/modalManager.js')

  assert.match(source, /getModalManager\s*\(/)
  assert.match(source, /manager\.register\s*\(entry\)/)
  assert.match(source, /manager\.unregister\s*\(entry\)/)
  assert.match(source, /manager\.isTop\s*\(entry\)/)
  assert.match(source, /manager\.handleKeyDown\s*\(entry, event\)/)
  assert.match(manager, /const managers\s*=\s*new WeakMap/)
  assert.match(manager, /originalOverflow/)
  assert.match(manager, /stack\.length\s*===\s*0/)
  assert.match(manager, /doc\.body\.style\.overflow\s*=\s*['"]hidden['"]/)
})

test('Modal guards backdrop and close button with topmost and canClose policy', async () => {
  const source = await readSource('../src/components/Modal.jsx')

  assert.match(source, /canClose\s*=\s*true/)
  assert.match(source, /event\.target\s*!==\s*event\.currentTarget/)
  assert.match(source, /manager\.isTop\s*\(entryRef\.current\)/)
  assert.match(source, /canCloseRef\.current[\s\S]*onCloseRef\.current/)
  assert.match(source, /disabled=\{!canClose\}/)
})

test('Modal traps focus, filters unsafe targets, and repairs focus after content mutation', async () => {
  const source = await readSource('../src/components/Modal.jsx')

  assert.match(source, /tabIndex=\{-1\}/)
  assert.match(source, /element\.tabIndex\s*<\s*0/)
  assert.match(source, /element\.disabled/)
  assert.match(source, /\[inert\]/)
  assert.match(source, /\[hidden\]/)
  assert.match(source, /\[aria-hidden=["']true["']\]/)
  assert.match(source, /visibility\s*!==\s*['"]hidden['"]/)
  assert.match(source, /event\.key\s*!==\s*['"]Tab['"]/)
  assert.match(source, /focusableElements\.length\s*===\s*0/)
  assert.match(source, /event\.shiftKey/)
  assert.match(source, /MutationObserver/)
  assert.match(source, /manager\.redirectFocus\s*\(entry, doc\.activeElement\)/)
})

test('Modal mount effect is stable while callbacks, policy and return ref stay current', async () => {
  const source = await readSource('../src/components/Modal.jsx')

  assert.match(source, /onCloseRef\.current\s*=\s*onClose/)
  assert.match(source, /canCloseRef\.current\s*=\s*canClose/)
  assert.match(source, /returnFocusRefRef\.current\s*=\s*returnFocusRef/)
  assert.match(source, /useEffect\s*\([\s\S]*manager\.register\(entry\)[\s\S]*,\s*\[\s*\]\s*\)/)
})

test('modal CSS owns the viewport and safe area above all app chrome with reduced motion support', async () => {
  const css = await readSource('../src/styles/brutalism.css')
  const overlay = css.match(/\.modal-overlay\s*\{([\s\S]*?)\}/)?.[1] || ''

  assert.match(overlay, /position:\s*fixed/)
  assert.match(overlay, /inset:\s*0/)
  assert.match(overlay, /100dvh/)
  assert.match(overlay, /safe-area-inset-(?:top|bottom)/)
  assert.match(overlay, /overflow-y:\s*auto/)
  const zIndex = Number(overlay.match(/z-index:\s*(\d+)/)?.[1])
  assert.ok(zIndex >= 1000, `expected deliberate top-level z-index, received ${zIndex}`)
  assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)/)
  assert.match(css, /transition-duration:\s*\.01ms\s*!important/)
  assert.match(css, /animation-duration:\s*\.01ms\s*!important/)
})

test('Positions supplies a stable focus fallback and state-machine dismissal policy', async () => {
  const source = await readSource('../src/pages/Positions.jsx')

  assert.match(source, /const pageContainerRef\s*=\s*useRef\(null\)/)
  assert.match(source, /ref=\{pageContainerRef\}[\s\S]*tabIndex=\{-1\}/)
  assert.match(source, /returnFocusRef=\{pageContainerRef\}/)
  assert.match(source, /canClose=\{canDismissClosePosition\(closeState\)\}/)
})
