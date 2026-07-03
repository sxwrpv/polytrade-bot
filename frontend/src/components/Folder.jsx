import { useEffect, useState } from 'react'

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

// Collapsible brutalist "folder" — a clickable section header that expands/
// collapses its content. Nestable (children can themselves be Folders).
// Pass a stable `id` to persist open/closed state across reloads; without an
// id it behaves exactly as before (resets to `open` on every mount).
export default function Folder({ id, title, children, open: initial = false, count }) {
  const [open, setOpen] = useState(() => readPersisted(id, initial))

  useEffect(() => {
    if (!id) return
    try {
      localStorage.setItem(storageKey(id), open ? '1' : '0')
    } catch {
      /* localStorage unavailable — persistence is a nicety, not required */
    }
  }, [id, open])

  return (
    <div className="folder">
      <button className={`folder-header ${open ? 'open' : ''}`} onClick={() => setOpen((o) => !o)}>
        <span className="folder-caret">{open ? '▾' : '▸'}</span>
        {title}
        {count != null && <span className="folder-count">[{count}]</span>}
      </button>
      {open && <div className="folder-body">{children}</div>}
    </div>
  )
}
