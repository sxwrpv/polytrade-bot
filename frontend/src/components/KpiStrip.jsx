// Persistent, non-collapsible glance strip. BALANCE (free cash) and EQUITY
// (cash + open-position value + unclaimed winnings) are split so the user can
// see that money sitting in positions or unredeemed wins isn't "missing" —
// it's just not spendable cash. Sticks under the app header.
export default function KpiStrip({ me, pnl, followingCount }) {
  const bal = me?.balance
  const equity = me?.equity
  const p7 = pnl?.pnl_7d
  const cls = (v) => (v == null ? '' : v >= 0 ? 'pos' : 'neg')
  const usd = (v) => (v == null ? '—' : `$${Number(v).toFixed(2)}`)
  const signed = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '-'}$${Math.abs(Number(v)).toFixed(2)}`)

  return (
    <div className="kpi-strip stat-grid">
      <div className="stat-cell">
        <div className="label">BALANCE (CASH)</div>
        <div className="value">{usd(bal)}</div>
      </div>
      <div className="stat-cell">
        <div className="label" title="cash + open positions + unclaimed winnings">EQUITY</div>
        <div className="value">{usd(equity)}</div>
      </div>
      <div className="stat-cell">
        <div className="label">PNL 7D</div>
        <div className={`value ${cls(p7)}`}>{signed(p7)}</div>
      </div>
      <div className="stat-cell">
        <div className="label">COPIED WALLETS</div>
        <div className="value">{followingCount ?? 0}</div>
      </div>
    </div>
  )
}
