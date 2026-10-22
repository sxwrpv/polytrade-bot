import { useEffect, useState } from 'react'
import { api } from './api'

/* Whether this server lets every linked account withdraw in the app, read once
   from /api/version. Until it answers, or if it cannot, the wording says there
   is no in-app withdrawal, which is also true while withdrawals are switched
   off or open only to an allowlist. */
const CLOSED = { open: false }
let pending = null

export function loadWithdrawStatus() {
  pending ||= api.version()
    .then((v) => ({ ...CLOSED, ...(v?.withdrawals || {}) }))
    .catch(() => CLOSED)
  return pending
}

export function useWithdrawStatus() {
  const [status, setStatus] = useState(CLOSED)
  useEffect(() => {
    let live = true
    loadWithdrawStatus().then((s) => { if (live) setStatus(s) })
    return () => { live = false }
  }, [])
  return status
}
