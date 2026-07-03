import { useMemo, useState } from 'react'
import { api, haptic } from '../api'
import Modal from './Modal'
import Sparkline from './Sparkline'
import TraderProfile from './TraderProfile'

const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')
const PERIODS = ['7d', '30d', '90d']
const EXIT_FILL_HELP =
  'sells ÷ buys in the window, % — how much of what they open they actively ' +
  'close out. Low = mostly holds to market resolution (your copied capital ' +
  'sits until then); high = trades in and out, capital turns over faster.'

// daily_pnl_90d arrives as a JSON string {"YYYY-MM-DD": pnl}; slice to the
// selected window and shape for the sparkline.
function sliceDaily(raw, days) {
  let obj
  try {
    obj = JSON.parse(raw || '{}')
  } catch {
    return []
  }
  const cutoff = new Date(Date.now() - days * 86400e3).toISOString().slice(0, 10)
  return Object.entries(obj)
    .filter(([date]) => date >= cutoff)
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([date, pnl]) => ({ date, pnl }))
}

function money(v) {
  const n = v || 0
  return `${n >= 0 ? '+' : '-'}$${Math.abs(Math.round(n)).toLocaleString()}`
}

export default function TraderCard({ t, period = '30d', onFollowed, balance }) {
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [alloc, setAlloc] = useState(10)
  const [maxPos, setMaxPos] = useState(50)
  const [msg, setMsg] = useState('')
  const [copied, setCopied] = useState(false)
  // the screener's global period picks the default; each card can flip its own
  const [chartPeriod, setChartPeriod] = useState(period)
  const pnl = t.total_pnl || 0
  const tier = t.tier || 'bronze'

  const daily = useMemo(
    () => sliceDaily(t.daily_pnl_90d, { '7d': 7, '30d': 30, '90d': 90 }[chartPeriod] || 30),
    [t.daily_pnl_90d, chartPeriod],
  )

  function copyAddress() {
    navigator.clipboard?.writeText(t.address)
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

  const hasPeriodStats = t.winrate_30d != null

  async function follow() {
    setMsg('')
    try {
      await api.follow(t.address, {
        allocation_pct: Number(alloc),
        max_position_usd: Number(maxPos),
      })
      setMsg('COPYING ✓')
      haptic('success')
      onFollowed?.()
      setTimeout(() => setOpen(false), 800)
    } catch (e) {
      setMsg(String(e.message || e))
    }
  }

  return (
    <div className="card trader-card">
      <div className="tc-top">
        <div>
          <span className="tc-name">{t.display_name || short(t.address)}</span>
          <span className={`tier-badge tier-${tier}`}>{tier.toUpperCase()}</span>
        </div>
        <span className="muted addr-inline" onClick={copyAddress} title="click to copy">
          {copied ? 'COPIED ✓' : short(t.address)}
        </span>
      </div>
      <div className="tc-stats">
        <span title="consistency score (0-1): steady green days + risk-adjusted return">
          CONS {(t.consistency_score ?? 0).toFixed(2)}
        </span>
        <span className={pnl >= 0 ? 'pos' : 'neg'} title="lifetime PnL (official leaderboard)">
          {money(pnl)}
        </span>
        <span title="all-time win rate over recent closing trades">
          WR {((t.win_rate || 0) * 100).toFixed(0)}%
        </span>
        <span>OPEN {t.open_positions || 0}</span>
        {t.pnl_quality != null && (
          <span
            className={t.pnl_quality >= 0 ? 'pos' : 'neg'}
            title="realized minus unrealized PnL — positive = gains are banked, very negative = sitting on unproven paper winners"
          >
            QUAL {money(t.pnl_quality)}
          </span>
        )}
      </div>

      {hasPeriodStats && (
        <div className="tc-period">
          <table className="tc-table">
            <thead>
              <tr>
                <th />
                <th>WR</th>
                <th>PNL</th>
                <th>VOL</th>
                <th title={EXIT_FILL_HELP}>EXIT/FILL</th>
                <th title="days with positive vs negative realized pnl">G/R DAYS</th>
              </tr>
            </thead>
            <tbody>
              {PERIODS.map((p) => {
                const ppnl = t[`pnl_${p}`] || 0
                return (
                  <tr key={p} className={p === chartPeriod ? 'active' : ''}>
                    <td>
                      <button className="tc-period-btn" onClick={() => setChartPeriod(p)}>
                        {p.toUpperCase()}
                      </button>
                    </td>
                    <td>{((t[`winrate_${p}`] || 0) * 100).toFixed(0)}%</td>
                    <td className={ppnl >= 0 ? 'pos' : 'neg'}>{money(ppnl)}</td>
                    <td>${Math.round(t[`volume_${p}`] || 0).toLocaleString()}</td>
                    <td>{(t[`fill_exit_ratio_${p}`] ?? 0).toFixed(0)}%</td>
                    <td>
                      <span className="pos">{t[`green_days_${p}`] || 0}</span>
                      {'/'}
                      <span className="neg">{t[`red_days_${p}`] || 0}</span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div
            className="tc-chart"
            title={`cumulative realized pnl, last ${chartPeriod} (from recent trade history)`}
          >
            <Sparkline daily={daily} />
          </div>
        </div>
      )}

      <div className="tc-actions">
        <button className="btn" onClick={() => setOpen(true)}>COPY TRADER</button>
        <button className="btn btn-ghost" onClick={() => setExpanded((e) => !e)}>
          {expanded ? 'HIDE POSITIONS ▴' : 'VIEW POSITIONS ▾'}
        </button>
      </div>

      {expanded && <TraderProfile address={t.address} />}

      {open && (
        <Modal title="COPY ALLOCATION" accent="green" onClose={() => setOpen(false)}>
          <label className="fld">
            ALLOCATION %
            <input value={alloc} onChange={(e) => setAlloc(e.target.value)} />
          </label>
          <label className="fld">
            MAX / POSITION (pUSD)
            <input value={maxPos} onChange={(e) => setMaxPos(e.target.value)} />
          </label>
          {balance != null && balance <= 0 && (
            <div className="warn-box">
              YOUR BALANCE IS $0 — COPYING WILL BE SET UP, BUT NO TRADES CAN
              EXECUTE UNTIL YOU FUND YOUR WALLET (USER &gt; FUND WALLET).
            </div>
          )}
          {msg && <div className="muted">{msg}</div>}
          <button className="btn" onClick={follow}>CONFIRM COPY</button>
        </Modal>
      )}
    </div>
  )
}
