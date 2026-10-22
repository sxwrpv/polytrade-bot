import Icon from './Icon'
import { dollars } from '../money'
import { redeemWithin } from '../redeemStatus'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')
const cents = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}¢`)
const usd = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}$${dollars(Math.abs(v))}`)

/* One position: what it is, what it is doing to your money, and the one action
   it allows. The P&L sits top-right where the eye lands after the title; the
   numbers behind it sit in one row underneath; status and the close control
   come last, and a position that cannot be sold says why instead of offering a
   button that would fail. */
export default function PositionCard({ p, closed, onRequestClose, redeem = { auto: false } }) {
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
  const within = redeemWithin(redeem)
  const tone = value == null ? '' : value >= 0 ? 'pos' : 'neg'

  let origin
  if (needsReconciliation) origin = null
  else if (manual) origin = 'Closed by you'
  else if (p.claimable) {
    origin = redeem.auto ? 'Market settled — winnings on their way to your balance'
      : 'Market settled — winnings not redeemed yet'
  }
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
            <b>Settling.</b>{' '}
            The last order on this position is being confirmed, so Close is paused until it settles.
          </div>
        </div>
      )}
      {closed && p.claimable && (
        <div className="notice" role="note">
          <Icon name="info" />
          {redeem.auto
            ? <div><b>Claimable.</b> It is redeemed to your balance automatically within {within}.</div>
            : <div><b>Claimable.</b> Redeem it on polymarket.com to turn it into spendable cash.</div>}
        </div>
      )}
      {!closed && !needsReconciliation && p.redeemable && (
        <div className="notice" role="note">
          <Icon name="info" />
          {Number(p.current_price) === 0
            ? <div><b>Resolved.</b> This bet lost and is worth $0.{redeem.auto && redeem.losses ? ' It clears from your wallet automatically.' : ''}</div>
            : redeem.auto
              ? <div><b>Resolved.</b> Winnings are redeemed to your balance automatically within {within}; there is nothing to sell.</div>
              : <div><b>Resolved.</b> Winnings must be redeemed on polymarket.com — there is nothing to sell.</div>}
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
