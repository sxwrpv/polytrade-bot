const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')
const cents = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}¢`)

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


  return (
    <div className="card pos-card">
      <div className="pc-title">
        {p.market_slug ? (
          <a
            href={`https://polymarket.com/event/${p.market_slug}`}
            target="_blank"
            rel="noopener noreferrer"
            title="view market on polymarket.com"
          >
            {p.market_title || `token ${(p.token_id || '').slice(0, 16)}…`}
          </a>
        ) : (
          p.market_title || `token ${(p.token_id || '').slice(0, 16)}…`
        )}
      </div>
      {needsReconciliation ? (
        <div className="muted">
          <strong>claim {String(p.claim_state || p.status).toUpperCase()}</strong>
          {p.claim_action ? ` · ${p.claim_action} BUY` : ''}
          {p.reserved_usd != null ? ` · $${Number(p.reserved_usd).toFixed(2)} reserved` : ''}
          {p.claim_id ? ` · ${short(p.claim_id)}` : ''}
          <div>reconciliation required — close and retry are disabled</div>
          {p.claim_error && <div className="neg">{p.claim_error}</div>}
        </div>
      ) : manual ? (
        <div className="muted">
          closed by you
        </div>
      ) : p.claimable ? (
        <div className="muted">
          market settled — winnings not redeemed yet
        </div>
      ) : p.external ? (
        <div className="muted">
          {p.origin === 'bot_history'
            ? `bot history links this token to ${short(p.trader_address)} — current shares need reconciliation`
            : 'untracked wallet position — origin not confirmed'}
        </div>
      ) : (
        <div className="muted">copying {short(p.trader_address)}</div>
      )}
      <div className="pc-row">
        <span className={`badge ${p.outcome === 'YES' ? 'pos' : 'neg'}`}>{p.outcome}</span>
        <span>entry {cents(p.entry_price)}</span>
        {!closed && <span>now {cents(p.current_price)}</span>}
        {closed && <span>exit {cents(p.exit_price)}</span>}
        {movePct != null && (
          <span className={movePct >= 0 ? 'pos' : 'neg'}>
            {movePct >= 0 ? '+' : ''}{movePct.toFixed(1)}%
          </span>
        )}
        <span className={value >= 0 ? 'pos' : 'neg'}>
          {value == null ? '—' : `${value >= 0 ? '+' : '-'}$${Math.abs(value).toFixed(2)}`}
        </span>
      </div>
      <div className="muted">{(p.shares || 0).toFixed(0)} shares</div>
      {closed && p.claimable && (
        <div className="muted" style={{ marginTop: 8 }}>
          <span className="badge pos">CLAIMABLE</span>{' '}
          redeem on polymarket.com to turn this into spendable cash
        </div>
      )}
      {!closed && (needsReconciliation ? (
        <div className="muted" style={{ marginTop: 8 }}>
          <span className="badge neg">RECONCILIATION REQUIRED</span>
        </div>
      ) : p.redeemable ? (
        <div className="muted" style={{ marginTop: 8 }}>
          <span className="badge pos">RESOLVED</span> winnings must be redeemed on polymarket.com — nothing to sell
        </div>
      ) : (
        <button className="btn btn-danger" style={{ marginTop: 8 }} onClick={() => onRequestClose(p)}>
          CLOSE
        </button>
      ))}
    </div>
  )
}
