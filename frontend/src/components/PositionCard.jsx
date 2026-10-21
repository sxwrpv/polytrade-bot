import Icon from './Icon'
import { dollars } from '../money'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')
const cents = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}¢`)
const usd = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}$${dollars(Math.abs(v))}`)

/* One position: what it is, what it is doing to your money, and the one action
   it allows. The P&L sits top-right where the eye lands after the title; the
   numbers behind it sit in one row underneath; status and the close control
   come last, and a position that cannot be sold says why instead of offering a
   button that would fail. */
export default function PositionCard({ p, closed, onRequestClose }) {
  const value = closed ? p.realized_pnl || 0 : p.unrealized_pnl
  const refPrice = closed ? p.exit_price : p.current_price
  const movePct = p.entry_price && refPrice != null
    ? ((refPrice - p.entry_price) / p.entry_price) * 100
    : null

  const manual = p.trader_address === 'manual'
  const needsReconciliation = p.reconciliation_required
    || p.status === 'closing'
    || p.status === 'reconciliation_required'
  const title = p.market_title || `token ${(p.token_id || '').slice(0, 16)}…`
  const tone = value == null ? '' : value >= 0 ? 'pos' : 'neg'

  let origin
  if (needsReconciliation) origin = null
  else if (manual) origin = 'Closed by you'
  else if (p.claimable) origin = 'Market settled — winnings not redeemed yet'
  else if (p.external) {
    origin = p.origin === 'bot_history'
      ? `Bot history links this token to ${short(p.trader_address)} — current shares need reconciliation`
      : 'Untracked wallet position — origin not confirmed'
  } else origin = `Copying ${short(p.trader_address)}`

  return (
    <article className="card pos-card">
      <div className="pc-top">
        <div>
          <h3 className="pc-title">
            {p.market_slug ? (
              <a
                href={`https://polymarket.com/event/${p.market_slug}`}
                target="_blank"
                rel="noopener noreferrer"
                title="View market on polymarket.com"
              >
                {title}
              </a>
            ) : title}
          </h3>
          <div className="pc-meta">
            <span className={`badge ${p.outcome === 'YES' ? 'pos' : 'neg'}`}>{p.outcome}</span>
            {origin && <span>{origin}</span>}
          </div>
        </div>
        <div className={`pc-pnl ${tone}`}>
          <b>{usd(value)}</b>
          {movePct != null && <span>{movePct >= 0 ? '+' : ''}{movePct.toFixed(1)}%</span>}
        </div>
      </div>

      <div className="pc-data">
        <div><small>Entry</small><b>{cents(p.entry_price)}</b></div>
        {closed
          ? <div><small>Exit</small><b>{cents(p.exit_price)}</b></div>
          : <div><small>Now</small><b>{cents(p.current_price)}</b></div>}
        <div><small>Shares</small><b>{Math.round(p.shares || 0).toLocaleString('en-US')}</b></div>
      </div>

      {needsReconciliation && (
        <div className="notice" role="note">
          <Icon name="alert" />
          <div>
            <b>Reconciliation required.</b>{' '}
            Claim {String(p.claim_state || p.status)}
            {p.claim_action ? ` · ${p.claim_action} BUY` : ''}
            {p.reserved_usd != null ? ` · $${dollars(p.reserved_usd)} reserved` : ''}
            {p.claim_id ? ` · ${short(p.claim_id)}` : ''}. Close and retry are disabled until it settles.
            {p.claim_error && <div className="neg">{p.claim_error}</div>}
          </div>
        </div>
      )}
      {closed && p.claimable && (
        <div className="notice" role="note">
          <Icon name="info" />
          <div><b>Claimable.</b> Redeem it on polymarket.com to turn it into spendable cash.</div>
        </div>
      )}
      {!closed && !needsReconciliation && p.redeemable && (
        <div className="notice" role="note">
          <Icon name="info" />
          <div><b>Resolved.</b> Winnings must be redeemed on polymarket.com — there is nothing to sell.</div>
        </div>
      )}
      {!closed && !needsReconciliation && !p.redeemable && (
        <div className="pc-foot">
          <button type="button" className="btn btn-danger" onClick={() => onRequestClose(p)}>
            Close position
          </button>
        </div>
      )}
    </article>
  )
}
