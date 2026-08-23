import { useEffect, useState } from 'react'
import { api } from '../api'
import PnLChart from './PnLChart'

/* Balance and the equity curve, at the top of Home.
 *
 * This is the first thing the account sees on opening the Mini App, so it owns
 * its own fetches rather than depending on a parent: Home should not have to
 * know how a balance is read, and the User tab should not have to stay mounted
 * for Home to work.
 *
 * The two-step `me()` call is deliberate and matches how the User tab did it.
 * The cheap read paints the card immediately; the second one costs a
 * server-side CLOB client build on first call, so the balance arrives late.
 * Until it does the figure shows a dash, never a zero — a balance we have not
 * read yet is not a balance of nothing.
 */
export default function AccountSummary() {
  const [me, setMe] = useState(null)
  const [period, setPeriod] = useState('7d')
  const [metric, setMetric] = useState('equity')   // 'equity' | 'pnl'
  const [series, setSeries] = useState([])

  useEffect(() => {
    api.me().then((m) => setMe((prev) => prev ?? m)).catch(() => {})
    api.me(true).then(setMe).catch(() => {})
  }, [])

  useEffect(() => {
    api.equitySeries(period).then(setSeries).catch(() => setSeries([]))
  }, [period])

  const money = (v) => (v == null ? '—' : `$${v.toFixed(2)}`)

  return (
    <div className="card account-summary">
      <div className="section-header">BALANCE</div>

      <div className="stat-grid">
        <div className="stat-cell">
          <div className="label">BALANCE (CASH)</div>
          <div className="value">{money(me?.balance)}</div>
        </div>
        <div className="stat-cell">
          <div className="label">IN POSITIONS</div>
          <div className="value">{money(me?.positions_value)}</div>
        </div>
        <div className="stat-cell">
          <div className="label" title="resolved wins not yet redeemed — claim on polymarket.com">CLAIMABLE</div>
          <div className="value">{money(me?.claimable)}</div>
        </div>
        <div className="stat-cell">
          <div className="label">EQUITY (TOTAL)</div>
          <div className="value">{money(me?.equity)}</div>
        </div>
      </div>

      {me?.balance == null && (
        <div className="muted small" style={{ marginTop: 6 }}>fund wallet to trade</div>
      )}
      {me?.claimable > 0 && (
        <div className="warn-box" style={{ marginTop: 8 }}>
          ${me.claimable.toFixed(2)} in resolved winnings isn&apos;t auto-claimed yet —
          redeem it on polymarket.com to turn it into spendable cash.
        </div>
      )}

      <div className="sort-row" style={{ justifyContent: 'space-between' }}>
        <div className="sort-row" style={{ margin: 0 }}>
          {['7d', '30d', 'all'].map((p) => (
            <button
              key={p}
              className={`chip ${period === p ? 'active' : ''}`}
              aria-pressed={period === p}
              onClick={() => setPeriod(p)}
            >{p.toUpperCase()}</button>
          ))}
        </div>
        <div className="sort-row" style={{ margin: 0 }}>
          {[['equity', 'EQUITY'], ['pnl', 'PNL']].map(([k, l]) => (
            <button
              key={k}
              className={`chip ${metric === k ? 'active' : ''}`}
              aria-pressed={metric === k}
              onClick={() => setMetric(k)}
            >{l}</button>
          ))}
        </div>
      </div>

      <PnLChart
        data={series.map((s) => ({ t: s.ts, y: metric === 'equity' ? s.equity : s.pnl }))}
        kind={metric}
      />
    </div>
  )
}
