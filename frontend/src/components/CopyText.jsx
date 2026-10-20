import { useEffect, useRef, useState } from 'react'
import Icon from './Icon'

// Click-to-copy with visible confirmation — every copyable value in the app
// (addresses, keys) goes through this so the user always gets feedback
// instead of wondering whether the tap did anything. A real <button>, so it is
// reachable by keyboard and announced; the copy icon morphs into a drawn
// check (motion kit: copy-button) and "Copy" rolls to "Copied".
export default function CopyText({ value, display, className = '', label = 'Copy' }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef(null)

  useEffect(() => () => clearTimeout(timer.current), [])

  async function copy() {
    try {
      await navigator.clipboard?.writeText(value)
    } catch {
      /* clipboard refused (permissions, old WebView) — the value stays visible to copy by hand */
    }
    setCopied(true)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1600)
  }

  return (
    <button
      type="button"
      className={`copy-text ${copied ? 'is-done' : ''} ${className}`.trim()}
      onClick={copy}
      aria-label={`${label}: ${value}`}
    >
      <span className="copy-value">{display || value}</span>
      <span className="copy-icons" aria-hidden="true">
        <Icon name="copy" className="icon copy-doc" />
        <Icon name="check" className="icon copy-check" />
      </span>
      <span className="copy-state" aria-live="polite">{copied ? 'Copied' : 'Copy'}</span>
    </button>
  )
}
