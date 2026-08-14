function authenticated(address, saveSession) {
  if (!address) return null
  const session = { address }
  saveSession(session)
  return { mode: 'authenticated', session }
}

/** Resolve the launch surface without relying on React or browser globals. */
export async function bootstrapLaunch({
  cachedSession,
  initData,
  api,
  saveSession,
  clearSession,
}) {
  // Fresh signed Telegram identity takes precedence over any cookie/local cache
  // that may belong to another account in this browser.
  if (initData) {
    try {
      const result = await api.telegramAuth(initData)
      const linked = authenticated(result.address, saveSession)
      if (linked) return linked
      // An unlinked Telegram identity must not immediately mint a new wallet
      // when this browser still has an authenticated, funded legacy account.
      if (typeof api.me === 'function') {
        try {
          const legacy = await api.me()
          if (legacy.address && legacy.telegram_linked === false) {
            const session = { address: legacy.address }
            saveSession(session)
            return { mode: 'legacy-link', session, initData }
          }
        } catch (error) {
          if (error?.status !== 401) {
            return {
              mode: 'session-error',
              session: cachedSession,
              message: 'We could not safely check for an existing wallet. Retry before onboarding.',
            }
          }
        }
      }
      return { mode: 'telegram-onboarding', session: null }
    } catch (error) {
      return {
        mode: 'telegram-error',
        session: null,
        message: error?.status === 401
          ? 'Telegram sign-in expired or could not be verified.'
          : 'Telegram sign-in is temporarily unavailable.',
      }
    }
  }

  if (cachedSession) {
    try {
      const me = await api.me()
      // `/me` is trusted; localStorage is public mutable display state. Always
      // rebuild it so a stale/malicious cached address is never returned.
      return authenticated(me.address, saveSession) || { mode: 'public', session: null }
    } catch (error) {
      if (error?.status === 401) {
        clearSession()
        return { mode: 'public', session: null }
      }
      return {
        mode: 'session-error',
        session: cachedSession,
        message: 'We could not verify your session. Check your connection and retry.',
      }
    }
  }

  // A browser can have a valid HttpOnly cookie even when localStorage was
  // cleared. Probe exactly once before deciding to show the public site.
  try {
    const me = await api.me()
    return authenticated(me.address, saveSession) || { mode: 'public', session: null }
  } catch {
    return { mode: 'public', session: null }
  }
}

/** Change client state only after the server confirms exact-session revocation. */
export async function performLogout({ api, clearSession, onLogout }) {
  await api.logout()
  clearSession()
  onLogout?.()
}

// Backwards-compatible session-only helper for callers/tests that predate the
// explicit launch modes.
export async function bootstrapSession(options) {
  const result = await bootstrapLaunch(options)
  return result.session
}
