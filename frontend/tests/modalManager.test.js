import test from 'node:test'
import assert from 'node:assert/strict'
import { createModalManager } from '../src/components/modalManager.js'

function element(name, { connected = true } = {}) {
  return {
    name,
    isConnected: connected,
    focused: 0,
    focus() { this.focused += 1 },
  }
}

function nativeFocusElement(doc, name, { connected = true, focusable = true } = {}) {
  const attributes = new Map()
  return {
    name,
    isConnected: connected,
    focused: 0,
    focus() {
      this.focused += 1
      if (focusable || attributes.has('tabindex')) doc.activeElement = this
    },
    hasAttribute(attribute) { return attributes.has(attribute) },
    setAttribute(attribute, value) { attributes.set(attribute, String(value)) },
    getAttribute(attribute) { return attributes.get(attribute) ?? null },
  }
}

function fakeDocument(overflow = '') {
  const fallback = element('main')
  return {
    body: { style: { overflow } },
    querySelector() { return fallback },
    fallback,
  }
}

function event(key = 'Escape') {
  return {
    key,
    prevented: 0,
    stopped: 0,
    preventDefault() { this.prevented += 1 },
    stopPropagation() { this.stopped += 1 },
  }
}

test('only the top modal handles Escape, and a non-closable top consumes it', () => {
  const manager = createModalManager(fakeDocument())
  let lowerCloses = 0
  let upperCloses = 0
  let upperClosable = false
  const lower = { dialog: element('lower'), canClose: () => true, onClose: () => { lowerCloses += 1 } }
  const upper = { dialog: element('upper'), canClose: () => upperClosable, onClose: () => { upperCloses += 1 } }
  manager.register(lower)
  manager.register(upper)

  const lowerEvent = event()
  assert.equal(manager.handleKeyDown(lower, lowerEvent), false)
  const upperEvent = event()
  assert.equal(manager.handleKeyDown(upper, upperEvent), true)
  assert.equal(upperEvent.prevented, 1)
  assert.equal(upperEvent.stopped, 1)
  assert.equal(lowerCloses, 0)
  assert.equal(upperCloses, 0)

  upperClosable = true
  manager.handleKeyDown(upper, event())
  assert.equal(upperCloses, 1)
  assert.equal(lowerCloses, 0)

  manager.unregister(upper)
  manager.handleKeyDown(lower, event())
  assert.equal(lowerCloses, 1)
})

test('body lock captures once and survives non-LIFO modal removal', () => {
  const doc = fakeDocument('clip')
  const manager = createModalManager(doc)
  const first = { dialog: element('first') }
  const second = { dialog: element('second') }
  manager.register(first)
  manager.register(second)
  assert.equal(doc.body.style.overflow, 'hidden')
  manager.unregister(first)
  assert.equal(doc.body.style.overflow, 'hidden')
  manager.unregister(second)
  assert.equal(doc.body.style.overflow, 'clip')
})

test('top removal focuses next modal before anything behind it', () => {
  const doc = fakeDocument()
  const manager = createModalManager(doc)
  const lower = { dialog: element('lower') }
  const behind = element('behind')
  const upper = { dialog: element('upper'), opener: behind, returnFocus: () => behind }
  manager.register(lower)
  manager.register(upper)
  manager.unregister(upper)
  assert.equal(lower.dialog.focused, 1)
  assert.equal(behind.focused, 0)
})

test('content removal redirects escaped focus to the neutral top dialog only', () => {
  const manager = createModalManager(fakeDocument())
  const lower = { dialog: element('lower') }
  const top = { dialog: element('top') }
  top.dialog.contains = () => false
  manager.register(lower)
  manager.register(top)

  assert.equal(manager.redirectFocus(lower, element('body')), false)
  assert.equal(manager.redirectFocus(top, element('body')), true)
  assert.equal(lower.dialog.focused, 0)
  assert.equal(top.dialog.focused, 1)
})

test('removed opener uses a connected explicit return-focus fallback', () => {
  const doc = fakeDocument()
  const manager = createModalManager(doc)
  const opener = element('removed opener', { connected: false })
  const fallback = element('positions page')
  const modal = { dialog: element('dialog'), opener, returnFocus: () => fallback }
  manager.register(modal)
  manager.unregister(modal)
  assert.equal(fallback.focused, 1)
  assert.equal(doc.fallback.focused, 0)
})

test('removed opener and fallback use a safe connected app/main fallback', () => {
  const doc = {
    body: { style: { overflow: '' } },
    activeElement: { name: 'body' },
    querySelector() { throw new Error('cached fallback should be used') },
  }
  const appFallback = nativeFocusElement(doc, 'main', { focusable: false })
  const opener = nativeFocusElement(doc, 'removed', { connected: false })
  opener.closest = () => appFallback
  const manager = createModalManager(doc)
  const modal = {
    dialog: element('dialog'),
    opener,
    returnFocus: () => element('removed fallback', { connected: false }),
  }
  manager.register(modal)
  manager.unregister(modal)
  assert.equal(appFallback.getAttribute('tabindex'), '-1')
  assert.equal(appFallback.focused, 1)
  assert.equal(doc.activeElement, appFallback)
})

test('queried app/main fallback is made programmatically focusable', () => {
  const doc = {
    body: { style: { overflow: '' } },
    activeElement: { name: 'body' },
  }
  const appFallback = nativeFocusElement(doc, 'root', { focusable: false })
  doc.querySelector = () => appFallback
  const manager = createModalManager(doc)
  const modal = {
    dialog: element('dialog'),
    opener: nativeFocusElement(doc, 'removed opener', { connected: false }),
    returnFocus: () => nativeFocusElement(doc, 'removed fallback', { connected: false }),
  }

  manager.register(modal)
  manager.unregister(modal)

  assert.equal(appFallback.getAttribute('tabindex'), '-1')
  assert.equal(doc.activeElement, appFallback)
})

test('a focus call that does not move activeElement falls through to the next fallback', () => {
  const doc = {
    body: { style: { overflow: '' } },
    activeElement: { name: 'body' },
    querySelector() { return null },
  }
  const opener = nativeFocusElement(doc, 'no-op opener', { focusable: false })
  const explicitFallback = nativeFocusElement(doc, 'explicit fallback')
  const manager = createModalManager(doc)
  const modal = {
    dialog: element('dialog'),
    opener,
    returnFocus: () => explicitFallback,
  }

  manager.register(modal)
  manager.unregister(modal)

  assert.equal(opener.focused, 1)
  assert.equal(explicitFallback.focused, 1)
  assert.equal(doc.activeElement, explicitFallback)
})
