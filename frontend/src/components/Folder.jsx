import { useEffect, useId, useState } from 'react'
import Icon from './Icon'

const storageKey = (id) => `folder:${id}`

function readPersisted(id, fallback) {
  if (!id) return fallback
  try {
    const saved = localStorage.getItem(storageKey(id))
    return saved == null ? fallback : saved === '1'
  } catch {
    return fallback
  }
}

// Collapsible section — a full-width header (52px, so it is an easy thumb
// target) that expands/collapses its content. Nestable (children can
// themselves be Folders). Pass a stable `id` to persist open/closed state
// across reloads; without an id it resets to `open` on every mount.
export default function Folder({ id, title, children, open: initial = false, count }) {
  const [open, setOpen] = useState(() => readPersisted(id, initial))
  const bodyId = useId()

  useEffect(() => {
    if (!id) return
    try {
      localStorage.setItem(storageKey(id), open ? '1' : '0')
    } catch {
      /* localStorage unavailable — persistence is a nicety, not required */
    }
  }, [id, open])

  return (
    <section className="folder">
      <button
        type="button"
        className={`folder-header ${open ? 'open' : ''}`}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon name="chevron" className="icon icon-sm folder-caret" />
        <span className="folder-title">{title}</span>
        {count != null && <span className="folder-count">{count}</span>}
      </button>
      {open && <div className="folder-body" id={bodyId}>{children}</div>}
    </section>
  )
}
