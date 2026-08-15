const managers = new WeakMap()

const isConnectedFocusableTarget = (target) => (
  target
  && target.isConnected !== false
  && typeof target.focus === 'function'
)

const focusSafely = (doc, target) => {
  if (!isConnectedFocusableTarget(target)) return false
  try {
    target.focus({ preventScroll: true })
    return !('activeElement' in doc) || doc.activeElement === target
  } catch {
    return false
  }
}

const makeProgrammaticallyFocusable = (target) => {
  if (
    target?.isConnected !== false
    && target?.setAttribute
    && !target.hasAttribute?.('tabindex')
  ) {
    target.setAttribute('tabindex', '-1')
  }
  return target
}

/**
 * Coordinates modal ownership and body locking for one document. Kept free of
 * React/DOM globals so stack behavior can be tested with small fakes.
 */
export function createModalManager(doc) {
  const stack = []
  let originalOverflow

  const top = () => stack[stack.length - 1]

  const register = (entry) => {
    if (stack.includes(entry)) return
    if (stack.length === 0) {
      originalOverflow = doc.body.style.overflow
      doc.body.style.overflow = 'hidden'
    }
    entry.appFallback ??= entry.opener?.closest?.(
      '[data-modal-focus-fallback], main, [role="main"], .app, #root',
    )
    stack.push(entry)
  }

  const unregister = (entry) => {
    const index = stack.indexOf(entry)
    if (index < 0) return
    const wasTop = index === stack.length - 1
    stack.splice(index, 1)

    if (stack.length === 0) {
      doc.body.style.overflow = originalOverflow
      originalOverflow = undefined
    }

    // Removing a covered modal must not steal focus from the current top one.
    if (!wasTop) return
    const next = top()
    if (next) {
      focusSafely(doc, next.dialog)
      return
    }

    if (focusSafely(doc, entry.opener)) return
    const explicitFallback = typeof entry.returnFocus === 'function'
      ? entry.returnFocus()
      : entry.returnFocus
    if (focusSafely(doc, explicitFallback)) return
    if (focusSafely(doc, makeProgrammaticallyFocusable(entry.appFallback))) return
    const appFallback = doc.querySelector?.(
      '[data-modal-focus-fallback], main, [role="main"], .app, #root',
    )
    // Main/app containers are logical destinations but are not focusable by
    // default. Make the fallback programmatically focusable without adding it
    // to keyboard tab order.
    focusSafely(doc, makeProgrammaticallyFocusable(appFallback))
  }

  const handleKeyDown = (entry, event) => {
    if (top() !== entry) return false
    if (event.key !== 'Escape') return false

    // Escape belongs to the top modal even when that modal cannot be closed.
    event.preventDefault?.()
    event.stopPropagation?.()
    const canClose = typeof entry.canClose === 'function'
      ? entry.canClose()
      : entry.canClose !== false
    if (canClose) entry.onClose?.()
    return true
  }

  const redirectFocus = (entry, focusedTarget) => {
    if (top() !== entry) return false
    if (entry.dialog?.contains?.(focusedTarget)) return false
    return focusSafely(doc, entry.dialog)
  }

  return {
    register,
    unregister,
    handleKeyDown,
    redirectFocus,
    isTop: (entry) => top() === entry,
    top,
    size: () => stack.length,
  }
}

export function getModalManager(doc) {
  let manager = managers.get(doc)
  if (!manager) {
    manager = createModalManager(doc)
    managers.set(doc, manager)
  }
  return manager
}
