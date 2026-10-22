import { useEffect, useRef, useState } from 'react'
import { api, haptic } from '../api'
import Icon from './Icon'
import Modal from './Modal'
import { Notice, describeError } from './UiState'
import { dollars } from '../money'
import {
  STEP_UP_WARN_AGE, clearDraft, describeStatus, groupAddress, isAddressShape, isSettled,
  lastFourMatches, launchAge, loadDraft, newIdempotencyKey, normalizeAmount, saveDraft,
  shortAddress, stageState,
} from '../withdrawForm'

const POLL_MS = 3000
const STAGES = ['Sending from your wallet', 'Converting on the bridge', 'Delivered']

function storage() {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

const when = (iso) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

// Multi-state status pill (motion kit: buttons/status-badge) on PolyTrade's
// tokens: the label changes in place, the pill eases to its new width, and a
// failure shakes it once.
export function WithdrawalStatus({ withdrawal }) {
  const info = describeStatus(withdrawal)
  const ref = useRef(null)
  const last = useRef({ width: null, tone: info.tone })

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    const width = el.getBoundingClientRect().width
    const from = last.current.width
    if (!reduce && from != null && Math.abs(from - width) > 1 && el.animate) {
      el.animate([{ width: `${from}px` }, { width: `${width}px` }],
        { duration: 420, easing: 'cubic-bezier(.2, .9, .25, 1.1)' })
    }
    if (!reduce && info.tone === 'error' && last.current.tone !== 'error') {
      el.classList.remove('is-shake')
      void el.offsetWidth
      el.classList.add('is-shake')
    }
    last.current = { width, tone: info.tone }
  }, [info.label, info.tone])

  return (
    <span ref={ref} className="wd-status" data-tone={info.tone}>
      <span className="wd-status-icon" aria-hidden="true">
        <i className="wd-status-dot" />
        <i className="wd-status-spin" />
        <Icon name="check" className="icon wd-status-ok" />
        <Icon name="close" className="icon wd-status-err" />
      </span>
      {info.label}
    </span>
  )
}

function PolygonscanLink({ href }) {
  if (!href) return null
  return (
    <a className="withdraw-link" href={href} target="_blank" rel="noreferrer noopener">
      View on Polygonscan<Icon name="external" className="icon icon-sm" />
    </a>
  )
}

function Progress({ withdrawal, onClose }) {
  const info = describeStatus(withdrawal)
  return (
    <>
      <div className="withdraw-progress-head" role="status" aria-live="polite">
        <WithdrawalStatus withdrawal={withdrawal} />
        <span className="mono">${dollars(withdrawal.amount)}</span>
      </div>
      <ol className="withdraw-steps">
        {STAGES.map((label, i) => (
          <li key={label} data-state={stageState(i + 1, info)}>{label}</li>
        ))}
      </ol>
      <p className="modal-lede">{info.text}</p>
      <PolygonscanLink href={withdrawal.explorer_url} />
      {!isSettled(withdrawal.status) && (
        <p className="small muted">You can close this. Telegram messages you when it arrives.</p>
      )}
      <div className="modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onClose}>
          {isSettled(withdrawal.status) ? 'Done' : 'Close'}
        </button>
      </div>
    </>
  )
}

function ConfirmWithdrawal({ preview, onClose, onSubmitted }) {
  const [typed, setTyped] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [row, setRow] = useState(null)
  const key = useRef(newIdempotencyKey())
  const matches = lastFourMatches(preview.destination, typed)
  const groups = groupAddress(preview.destination)

  useEffect(() => {
    if (!row || isSettled(row.status)) return undefined
    const timer = setTimeout(async () => {
      try {
        setRow(await api.withdrawal(row.id))
      } catch {
        setRow((current) => ({ ...current }))      // try again next tick
      }
    }, POLL_MS)
    return () => clearTimeout(timer)
  }, [row])

  async function send() {
    // Step-up: the backend re-verifies the live Telegram identity, so a stolen
    // session alone cannot move money out.
    const initData = window.Telegram?.WebApp?.initData || ''
    if (!initData) {
      setError('Open PolyTrade from the Telegram bot to withdraw.')
      return
    }
    setSending(true)
    setError('')
    try {
      const result = await api.createWithdrawal({
        amount: preview.amount,
        destination: preview.destination,
        confirm_last4: typed.trim(),
        idempotency_key: key.current,
      }, initData)
      setRow(result)
      haptic(result.status === 'failed' ? 'error' : 'success')
      onSubmitted(result)
    } catch (err) {
      setError(describeError(err))
      haptic('error')
    } finally {
      setSending(false)
    }
  }

  return (
    <Modal title={row ? 'Withdrawal' : 'Confirm withdrawal'} accent="red" canClose={!sending}
      onClose={onClose}>
      {row ? (
        <Progress withdrawal={row} onClose={onClose} />
      ) : (
        <>
          <dl className="withdraw-summary">
            <div><dt>You send</dt><dd className="mono">${dollars(preview.amount)}</dd></div>
            <div>
              <dt>Arrives as</dt>
              <dd className="mono">
                about {preview.receive_estimate} USDC
                <small>at least {preview.receive_minimum} USDC</small>
              </dd>
            </div>
            <div><dt>Network</dt><dd>Polygon</dd></div>
            <div><dt>Takes</dt><dd>about {Math.max(10, Math.round(preview.seconds / 10) * 10)} seconds</dd></div>
          </dl>
          <span className="account-label">To this address</span>
          <div className="addr withdraw-address">
            {groups.map((group, i) => (
              <span key={i}>{i === groups.length - 1 ? <b>{group}</b> : group}{' '}</span>
            ))}
          </div>
          <label className="fld">
            Type the last 4 characters of the address
            <input
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              maxLength={4}
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              aria-invalid={typed.trim().length === 4 && !matches ? 'true' : undefined}
            />
            <span className="fld-hint">Compare the whole address with your wallet first. A transfer that reaches the chain cannot be reversed.</span>
          </label>
          {error && <p className="neg modal-lede" role="alert">{error}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-danger is-strong" onClick={send}
              disabled={!matches || sending}>
              {sending ? 'Sending…' : `Withdraw $${dollars(preview.amount)}`}
            </button>
            <button type="button" className="btn btn-ghost" onClick={onClose} disabled={sending}>
              Cancel
            </button>
          </div>
        </>
      )}
    </Modal>
  )
}

function Recent({ rows }) {
  return (
    <div className="withdraw-recent">
      <div className="section-header">Recent withdrawals</div>
      {rows.map((w) => (
        <div className="breakdown-row" key={w.id}>
          <span>
            <b>${dollars(w.amount)} to {shortAddress(w.destination)}</b>
            <small>{when(w.requested_at)}</small>
          </span>
          <span className="withdraw-recent-end">
            <WithdrawalStatus withdrawal={w} />
            {w.explorer_url && (
              <a className="icon-btn" href={w.explorer_url} target="_blank" rel="noreferrer noopener"
                aria-label={`View the $${w.amount} withdrawal on Polygonscan`}>
                <Icon name="external" />
              </a>
            )}
          </span>
        </div>
      ))}
    </div>
  )
}

export default function WithdrawPanel() {
  const [info, setInfo] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [reload, setReload] = useState(0)
  const draft = useRef(loadDraft(storage()))
  const [destination, setDestination] = useState(draft.current?.destination || '')
  const [amount, setAmount] = useState(draft.current?.amount || '')
  const [formError, setFormError] = useState('')
  const [checking, setChecking] = useState(false)
  const [preview, setPreview] = useState(null)
  const webApp = typeof window !== 'undefined' ? window.Telegram?.WebApp : null
  const age = launchAge(webApp)

  useEffect(() => {
    let live = true
    setLoadError(null)
    api.withdrawals()
      .then((r) => { if (live) setInfo(r) })
      .catch((e) => { if (live) setLoadError(e) })
    return () => { live = false }
  }, [reload])

  useEffect(() => { saveDraft(storage(), { destination, amount }) }, [destination, amount])

  // Follow the open withdrawal until it settles, then refresh the balances.
  useEffect(() => {
    const open = info?.open
    if (!open || isSettled(open.status) || preview) return undefined
    const timer = setTimeout(async () => {
      try {
        const row = await api.withdrawal(open.id)
        if (isSettled(row.status)) setReload((n) => n + 1)
        else setInfo((current) => (current ? { ...current, open: row } : current))
      } catch {
        setInfo((current) => (current ? { ...current } : current))
      }
    }, POLL_MS)
    return () => clearTimeout(timer)
  }, [info, preview])

  if (loadError) {
    return (
      <Notice
        tone="error"
        action={<button type="button" className="btn btn-ghost btn-sm" onClick={() => setReload((n) => n + 1)}>Retry</button>}
      >
        Withdrawals couldn’t be loaded. {describeError(loadError)}
      </Notice>
    )
  }
  if (!info) {
    return <div className="sk-card" aria-busy="true"><div className="sk sk-title" /><div className="sk sk-line" /></div>
  }
  if (!info.enabled) {
    return <p className="small muted">Withdrawals are not available for this account.</p>
  }

  const { balance, limits, open } = info
  const busy = Boolean(open && !isSettled(open.status)) || open?.status === 'attention'
  const outsideTelegram = age == null
  const stale = age != null && age > STEP_UP_WARN_AGE
  const cleanAmount = normalizeAmount(amount)
  const cleanAddress = destination.trim()
  const addressOk = isAddressShape(cleanAddress)
  const canReview = !busy && !info.paused && !outsideTelegram && !stale && !checking
    && Boolean(cleanAmount) && addressOk

  async function review(event) {
    event.preventDefault()
    setFormError('')
    if (!addressOk) {
      setFormError('Enter a Polygon address: 0x followed by 40 letters and digits.')
      return
    }
    if (!cleanAmount) {
      setFormError('Enter an amount in dollars, for example 25.50.')
      return
    }
    setChecking(true)
    try {
      setPreview(await api.previewWithdrawal({ amount: cleanAmount, destination: cleanAddress }))
    } catch (err) {
      setFormError(describeError(err))
    } finally {
      setChecking(false)
    }
  }

  function submitted(row) {
    if (row.status !== 'failed') {
      clearDraft(storage())
      setDestination('')
      setAmount('')
    }
  }

  function closeConfirm() {
    setPreview(null)
    setReload((n) => n + 1)
  }

  return (
    <div className="card withdraw">
      {open && (
        <div className="withdraw-open">
          <div className="withdraw-open-head">
            <span className="account-label">${dollars(open.amount)} to {shortAddress(open.destination)}</span>
            <WithdrawalStatus withdrawal={open} />
          </div>
          <p className="small">{describeStatus(open).text}</p>
          <PolygonscanLink href={open.explorer_url} />
        </div>
      )}
      {info.paused && <Notice tone="error">{info.paused}</Notice>}

      <div className="withdraw-available">
        <span className="account-label">Available to withdraw</span>
        <span className="withdraw-figure mono">{balance ? `$${dollars(balance.available)}` : '—'}</span>
        {balance && (
          <span className="fld-hint">
            Cash ${dollars(balance.cash)}, minus ${dollars(balance.reserved_for_orders)} held for open copy orders
            {Number(balance.settling) > 0 && `, $${dollars(balance.settling)} still settling`}
            {Number(balance.in_progress) > 0 && `, $${dollars(balance.in_progress)} already on its way`}.
          </span>
        )}
        {info.balance_error && <span className="fld-hint neg">{info.balance_error}</span>}
      </div>

      {outsideTelegram && <Notice>Open PolyTrade from the Telegram bot to withdraw.</Notice>}
      {stale && (
        <Notice action={<button type="button" className="btn btn-ghost btn-sm" onClick={() => webApp?.close?.()}>Close</button>}>
          A withdrawal needs a fresh Telegram check. Close PolyTrade and reopen it from the bot; your address and amount stay filled in.
        </Notice>
      )}

      <form onSubmit={review} noValidate>
        <label className="fld">
          Polygon address
          <input
            value={destination}
            onChange={(e) => { setDestination(e.target.value); setFormError('') }}
            placeholder="0x…"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            aria-invalid={cleanAddress && !addressOk ? 'true' : undefined}
          />
          <span className="fld-hint">It arrives as USDC on Polygon. Use a Polygon address you control.</span>
        </label>
        <label className="fld">
          Amount (USD)
          <span className="withdraw-amount-row">
            <input
              value={amount}
              onChange={(e) => { setAmount(e.target.value); setFormError('') }}
              inputMode="decimal"
              placeholder="0.00"
              aria-invalid={amount.trim() && !cleanAmount ? 'true' : undefined}
            />
            <button type="button" className="btn btn-ghost btn-sm" disabled={!balance}
              onClick={() => balance && setAmount(Number(balance.available).toFixed(2))}>
              Max
            </button>
          </span>
          {limits && (
            <span className="fld-hint">
              ${limits.min} to ${limits.max} per withdrawal. ${dollars(limits.left_today)} and {limits.count_left_today} withdrawal{limits.count_left_today === 1 ? '' : 's'} left in the next 24 hours.
            </span>
          )}
        </label>
        <Notice>
          Exchanges list it as USDC on the Polygon network. Check that network before you paste an exchange deposit address.
        </Notice>
        {formError && <div className="warn-box withdraw-error" role="alert">{formError}</div>}
        <button type="submit" className="btn btn-block withdraw-submit" disabled={!canReview}>
          <Icon name="send" />{checking ? 'Checking…' : 'Review withdrawal'}
        </button>
      </form>

      {info.recent?.length > 0 && <Recent rows={info.recent} />}
      {preview && <ConfirmWithdrawal preview={preview} onClose={closeConfirm} onSubmitted={submitted} />}
    </div>
  )
}
