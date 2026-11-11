import { useEffect, useState } from 'react'
import { loadVersion } from './serverVersion.js'

/* Whether this server lets every linked account withdraw in the app, from the
   shared /api/version read. Until it answers, or if it cannot, the wording
   says there is no in-app withdrawal, which is also true while withdrawals are
   switched off or open only to an allowlist. */
const CLOSED = { open: false }

export function loadWithdrawStatus() {
  return loadVersion()
    .then((v) => ({ ...CLOSED, ...(v?.withdrawals || {}) }))
    .catch(() => CLOSED)
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
