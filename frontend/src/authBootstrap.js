export async function bootstrapSession({
  cachedSession,
  initData,
  api,
  saveSession,
  clearSession,
}) {
  if (cachedSession) {
    try {
      await api.me()
      return cachedSession
    } catch (error) {
      // A cached wallet address is public UI state, not proof that the HttpOnly
      // session cookie is still valid. Only discard it for an auth failure;
      // retain it across transient server/network errors.
      if (error?.status !== 401) return cachedSession
      clearSession()
    }
  }

  if (!initData) return null

  try {
    const result = await api.telegramAuth(initData)
    if (!result.address) return null
    const session = { address: result.address }
    saveSession(session)
    return session
  } catch {
    return null
  }
}
