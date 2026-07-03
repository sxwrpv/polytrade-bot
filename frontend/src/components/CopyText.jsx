import { useState } from 'react'

// Click-to-copy with visible confirmation — every copyable value in the app
// (addresses, referral codes) goes through this so the user always gets
// feedback instead of wondering whether the click did anything.
export default function CopyText({ value, display, className = 'addr' }) {
  const [copied, setCopied] = useState(false)

  function copy() {
    navigator.clipboard?.writeText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

  return (
    <div className={className} onClick={copy} title="click to copy">
      {copied ? 'COPIED ✓' : display || value}
    </div>
  )
}
