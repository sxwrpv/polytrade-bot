import { useEffect, useState } from 'react'
import { api, getWallet, clearWallet } from '../api'
import CopyText from '../components/CopyText'
import StatGrid from '../components/StatGrid'
import PnLChart from '../components/PnLChart'
import DotStrip from '../components/DotStrip'
import Modal from '../components/Modal'
import Folder from '../components/Folder'
import DepositAddresses from '../components/DepositAddresses'

export default function User({ onLogout }) {
  const [me, setMe] = useState(null)
  const [pnl, setPnl] = useState(null)
  const [period, setPeriod] = useState('7d')
  const [series, setSeries] = useState([])
  const [metric, setMetric] = useState('equity')   // 'equity' | 'pnl'
  const [byWallet, setByWallet] = useState([])
  const [name, setName] = useState('')
  const [exp, setExp] = useState(false)
  const [key, setKey] = useState('')
  const [expErr, setExpErr] = useState('')
  const addr = getWallet()

  useEffect(() => {
    // paint the profile immediately, upgrade with balance when it lands (the
    // balance read builds the CLOB client server-side on first call — slow)
    api.me().then((m) => {
      setMe((prev) => prev ?? m)
      setName(m.display_name || '')
    })
    api.me(true).then(setMe).catch(() => {})
    api.pnlByWallet().then(setByWallet).catch(() => {})
  }, [])

  useEffect(() => {
    api.pnl(period).then(setPnl).catch(() => {})
    api.equitySeries(period).then(setSeries).catch(() => setSeries([]))
  }, [period])

  async function saveName() {
    try {
      await api.settings({ display_name: name })
    } catch {
      /* ignore */
    }
  }

  async function reveal() {
    setExpErr('')
    try {
      const r = await api.exportKey()
      setKey(r.private_key)
    } catch (e) {
      setExpErr(String(e.message || e))
    }
  }

  const curve = pnl?.equity_curve || []
  const dayValues = curve.map((d) => d.pnl)
  const dayTitles = curve.map((d) => `${d.date}: ${d.pnl >= 0 ? '+' : '-'}$${Math.abs(d.pnl).toFixed(2)}`)

  return (
    <div>
      <Folder id="user-account" title="ACCOUNT" open>
        <div className="card">
          <div className="muted">WALLET (click to copy)</div>
          <CopyText value={addr} />
          <label className="fld">
            DISPLAY NAME
            <input value={name} onChange={(e) => setName(e.target.value)} onBlur={saveName} />
          </label>
          <div className="stat-grid" style={{ marginTop: 10 }}>
            <div className="stat-cell">
              <div className="label">BALANCE (CASH)</div>
              <div className="value">{me?.balance != null ? `$${me.balance.toFixed(2)}` : '—'}</div>
            </div>
            <div className="stat-cell">
              <div className="label">IN POSITIONS</div>
              <div className="value">{me?.positions_value != null ? `$${me.positions_value.toFixed(2)}` : '—'}</div>
            </div>
            <div className="stat-cell">
              <div className="label" title="resolved wins not yet redeemed — claim on polymarket.com">CLAIMABLE</div>
              <div className="value">{me?.claimable != null ? `$${me.claimable.toFixed(2)}` : '—'}</div>
            </div>
            <div className="stat-cell">
              <div className="label">EQUITY (TOTAL)</div>
              <div className="value">{me?.equity != null ? `$${me.equity.toFixed(2)}` : '—'}</div>
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
        </div>
      </Folder>

      <Folder id="user-fund" title="FUND WALLET">
        <DepositAddresses gasless={me?.gasless} />
      </Folder>

      <Folder id="user-performance" title="PERFORMANCE" open>
        <StatGrid pnl={pnl} />
        <div className="sort-row" style={{ justifyContent: 'space-between' }}>
          <div className="sort-row" style={{ margin: 0 }}>
            {['7d', '30d', 'all'].map((p) => (
              <button key={p} className={`chip ${period === p ? 'active' : ''}`} onClick={() => setPeriod(p)}>
                {p.toUpperCase()}
              </button>
            ))}
          </div>
          <div className="sort-row" style={{ margin: 0 }}>
            {[['equity', 'EQUITY'], ['pnl', 'PNL']].map(([k, l]) => (
              <button key={k} className={`chip ${metric === k ? 'active' : ''}`} onClick={() => setMetric(k)}>
                {l}
              </button>
            ))}
          </div>
        </div>
        <PnLChart
          data={series.map((s) => ({ t: s.ts, y: metric === 'equity' ? s.equity : s.pnl }))}
          kind={metric}
        />

        <div className="section-header">DAILY PNL ({period.toUpperCase()})</div>
        <div className="card">
          <DotStrip values={dayValues} titles={dayTitles} max={90} />
        </div>

        <Folder id="user-breakdown" title="BREAKDOWN BY COPIED WALLET">
          {byWallet.length === 0 ? (
            <div className="muted">no closed positions yet</div>
          ) : (
            byWallet.map((w) => (
              <div className="card" key={w.trader_address}>
                <div className="tc-top">
                  <span className="tc-name">
                    {w.trader_address === 'manual'
                      ? 'MANUAL TRADES'
                      : w.display_name || `${w.trader_address.slice(0, 6)}…${w.trader_address.slice(-4)}`}
                  </span>
                  <span className={w.realized_pnl >= 0 ? 'pos' : 'neg'}>
                    {w.realized_pnl >= 0 ? '+' : '-'}${Math.abs(w.realized_pnl).toFixed(2)}
                  </span>
                </div>
                <div className="tc-stats">
                  <span>CLOSED {w.closed_trades}</span>
                  <span>WR {Math.round((w.win_rate || 0) * 100)}%</span>
                </div>
              </div>
            ))
          )}
        </Folder>
      </Folder>

      <Folder id="user-security" title="SECURITY">
        <div className="card">
          <button className="btn btn-danger" onClick={() => setExp(true)}>EXPORT PRIVATE KEY</button>
        </div>
      </Folder>

      <Folder id="user-legal" title="LEGAL">
        <div className="card muted small">
          Real trades execute on Polymarket. Prediction markets carry risk of total loss. You
          are solely responsible for the funds in your custodial wallet and for backing up your
          private key. Not available in restricted jurisdictions.
        </div>
      </Folder>

      <button className="btn" style={{ marginTop: 16 }} onClick={() => { clearWallet(); onLogout?.() }}>
        LOG OUT
      </button>

      {exp && (
        <Modal
          title="EXPORT PRIVATE KEY"
          accent="red"
          onClose={() => { setExp(false); setKey(''); setExpErr('') }}
        >
          <div className="warn-box">
            WARNING: YOUR PRIVATE KEY GIVES FULL ACCESS TO YOUR FUNDS. NEVER SHARE IT. STORE
            OFFLINE.
          </div>
          {!key ? (
            <>
              {expErr && <div className="neg">{expErr}</div>}
              <button className="btn btn-danger" onClick={reveal}>REVEAL KEY</button>
            </>
          ) : (
            <div className="addr key-reveal">{key}</div>
          )}
        </Modal>
      )}
    </div>
  )
}
