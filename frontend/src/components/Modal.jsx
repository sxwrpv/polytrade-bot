import { useEffect, useId, useRef } from 'react'
import { createPortal } from 'react-dom'
import { getModalManager } from './modalManager'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button',
  'input:not([type="hidden"])',
  'select',
  'textarea',
  'iframe',
  'object',
  'embed',
  '[contenteditable="true"]',
  '[tabindex]',
].join(',')

const isActuallyFocusable = (element) => {
  if (!element || element.disabled || element.hidden || element.tabIndex < 0) return false
  if (element.closest?.('[inert], [hidden], [aria-hidden="true"]')) return false
  if (element.getClientRects?.().length === 0) return false

  const view = element.ownerDocument?.defaultView
  const style = view?.getComputedStyle?.(element)
  return !style || (style.display !== 'none' && style.visibility !== 'hidden')
}

export default function Modal({
  title,
  children,
  onClose,
  accent = 'green',
  canClose = true,
  returnFocusRef,
}) {
  const dialogRef = useRef(null)
  const entryRef = useRef(null)
  const onCloseRef = useRef(onClose)
  const canCloseRef = useRef(canClose)
  const returnFocusRefRef = useRef(returnFocusRef)
  const titleId = useId()

  // Event listeners are mounted once; these refs keep their behavior current
  // without unlocking scroll or moving focus when callback identity changes.
  onCloseRef.current = onClose
  canCloseRef.current = canClose
  returnFocusRefRef.current = returnFocusRef

  useEffect(() => {
    if (typeof document === 'undefined' || !dialogRef.current) return undefined

    const doc = dialogRef.current.ownerDocument
    const manager = getModalManager(doc)
    const entry = {
      dialog: dialogRef.current,
      opener: doc.activeElement,
      returnFocus: () => returnFocusRefRef.current?.current ?? returnFocusRefRef.current,
      canClose: () => canCloseRef.current,
      onClose: () => onCloseRef.current?.(),
    }
    entryRef.current = entry
    manager.register(entry)

    const getFocusableElements = () => Array.from(
      entry.dialog.querySelectorAll(FOCUSABLE_SELECTOR),
    ).filter(isActuallyFocusable)

    const handleKeyDown = (event) => {
      if (!manager.isTop(entry)) return
      if (manager.handleKeyDown(entry, event)) return
      if (event.key !== 'Tab') return

      const focusableElements = getFocusableElements()
      if (focusableElements.length === 0) {
        event.preventDefault()
        entry.dialog.focus({ preventScroll: true })
        return
      }

      const first = focusableElements[0]
      const last = focusableElements[focusableElements.length - 1]
      const active = doc.activeElement

      if (!entry.dialog.contains(active) || active === entry.dialog) {
        event.preventDefault()
        ;(event.shiftKey ? last : first).focus()
      } else if (event.shiftKey && active === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }

    const handleFocusIn = (event) => {
      manager.redirectFocus(entry, event.target)
    }

    const MutationObserverClass = doc.defaultView?.MutationObserver
      ?? (typeof MutationObserver !== 'undefined' ? MutationObserver : null)
    const observer = MutationObserverClass
      ? new MutationObserverClass(() => {
          // If React replaced the focused child, browsers commonly move focus
          // to body. Focus the neutral dialog container, never an action.
          manager.redirectFocus(entry, doc.activeElement)
        })
      : null

    doc.addEventListener('keydown', handleKeyDown)
    doc.addEventListener('focusin', handleFocusIn)
    observer?.observe(entry.dialog, { childList: true, subtree: true })
    entry.dialog.focus({ preventScroll: true })

    return () => {
      observer?.disconnect()
      doc.removeEventListener('keydown', handleKeyDown)
      doc.removeEventListener('focusin', handleFocusIn)
      manager.unregister(entry)
      entryRef.current = null
    }
  }, [])

  // Covers content replacement committed before MutationObserver delivery.
  useEffect(() => {
    if (typeof document === 'undefined' || !entryRef.current) return
    const entry = entryRef.current
    getModalManager(entry.dialog.ownerDocument).redirectFocus(
      entry,
      entry.dialog.ownerDocument.activeElement,
    )
  })

  const handleBackdropClick = (event) => {
    if (event.target !== event.currentTarget || !entryRef.current) return
    const manager = getModalManager(entryRef.current.dialog.ownerDocument)
    if (!manager.isTop(entryRef.current)) return
    if (canCloseRef.current) onCloseRef.current?.()
  }

  // Hooks still run consistently during SSR, but no DOM global is read by the
  // render path and no portal is attempted until a browser document exists.
  const portalTarget = typeof document !== 'undefined' ? document.body : null
  if (!portalTarget) return null

  return createPortal(
    <div className="modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={dialogRef}
        className={`modal ${accent === 'red' ? 'modal-red' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="modal-title" id={titleId}>{title}</div>
        {children}
        <button
          className="modal-x"
          onClick={() => {
            const entry = entryRef.current
            if (!entry) return
            const manager = getModalManager(entry.dialog.ownerDocument)
            if (manager.isTop(entry) && canCloseRef.current) onCloseRef.current?.()
          }}
          disabled={!canClose}
        >
          [ CLOSE ]
        </button>
      </div>
    </div>,
    portalTarget,
  )
}
