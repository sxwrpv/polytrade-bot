import Icon from './Icon'

/* Every data region in the Mini App answers the same three questions the same
   way: is it loading, is there nothing yet (and what to do about it), or did
   the read fail (and how to retry). A failed read must never render as an
   empty list — "no open positions" after a timeout is a false statement about
   real money. */

export function StateBlock({ icon = 'info', title, children, action, tone }) {
  return (
    <div className={`state${tone === 'error' ? ' is-error' : ''}`} role={tone === 'error' ? 'alert' : undefined}>
      <span className="state-icon"><Icon name={icon} /></span>
      {title && <h3>{title}</h3>}
      {children && <p>{children}</p>}
      {action}
    </div>
  )
}

export function ErrorState({ title = 'Couldn’t load this', error, onRetry, retrying = false }) {
  return (
    <StateBlock
      icon="alert"
      tone="error"
      title={title}
      action={onRetry && (
        <button type="button" className="btn btn-ghost" onClick={onRetry} disabled={retrying}>
          <Icon name="refresh" />
          {retrying ? 'Retrying…' : 'Try again'}
        </button>
      )}
    >
      {describeError(error)}
    </StateBlock>
  )
}

/** A short, plain explanation of a failed request: what failed, not a stack. */
export function describeError(error) {
  if (!error) return 'Check your connection, then try again.'
  if (error.status === 401) return 'Your session has expired. Reopen PolyTrade from the Telegram bot.'
  const detail = String(error.message || '').trim()
  if (!detail || /failed to fetch|networkerror|load failed/i.test(detail)) {
    return 'PolyTrade could not be reached. Check your connection, then try again.'
  }
  return `${detail.replace(/\.$/, '')}.`
}

export function SkeletonCards({ count = 3, label = 'Loading' }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true">
      <span className="visually-hidden">{label}…</span>
      {Array.from({ length: count }, (_, i) => (
        <div className="sk-card" key={i} aria-hidden="true">
          <div className="sk sk-title" />
          <div className="sk sk-line sk-short" />
          <div className="sk sk-line" />
        </div>
      ))}
    </div>
  )
}

export function Notice({ tone, icon, children, action }) {
  return (
    <div className={`notice${tone ? ` is-${tone}` : ''}`} role={tone === 'error' ? 'alert' : undefined}>
      <Icon name={icon || (tone === 'error' ? 'alert' : tone === 'ok' ? 'check' : 'info')} />
      <div>{children}</div>
      {action && <span className="notice-action">{action}</span>}
    </div>
  )
}
