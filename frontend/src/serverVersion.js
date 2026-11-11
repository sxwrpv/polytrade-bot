import { api } from './api.js'

/* What this server runs, read from /api/version once and shared by everything
   that asks. A read that fails is forgotten, so the next caller asks again. */
let pending = null

export function loadVersion() {
  if (!pending) {
    const read = api.version()
    pending = read
    read.catch(() => { if (pending === read) pending = null })
  }
  return pending
}
