import { useEffect, useState } from 'react'
import { loadVersion } from './serverVersion.js'

/* Whether this server redeems resolved positions by itself, from the shared
   /api/version read. Until it answers, or if it cannot, the wording sends
   people to polymarket.com, which works whatever the server does. */
const MANUAL = { auto: false, losses: false, every_minutes: null }

export function loadRedeemStatus() {
  return loadVersion()
    .then((v) => ({ ...MANUAL, ...(v?.redeem || {}) }))
    .catch(() => MANUAL)
}

export function useRedeemStatus() {
  const [status, setStatus] = useState(MANUAL)
  useEffect(() => {
    let live = true
    loadRedeemStatus().then((s) => { if (live) setStatus(s) })
    return () => { live = false }
  }, [])
  return status
}

/** The server's redeem cadence in minutes; 30 is the loop's default. */
export const redeemMinutes = (status) => status.every_minutes || 30

/** "about 30 minutes", from the server's redeem cadence. */
export const redeemWithin = (status) => `about ${redeemMinutes(status)} minutes`
