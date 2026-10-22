import { useCallback, useEffect, useRef, useState } from 'react'
import { api, getWallet, clearWallet } from '../api'
import CopyText from '../components/CopyText'
import StatGrid from '../components/StatGrid'
import PnlHeatmap from '../components/PnlHeatmap'
import Modal from '../components/Modal'
import Folder from '../components/Folder'
import Icon from '../components/Icon'
import { FundingAccess } from '../components/DepositAddresses'
import WithdrawPanel from '../components/Withdraw'
import { Notice, StateBlock, describeError } from '../components/UiState'
import { performLogout } from '../authBootstrap'
import { redeemWithin, useRedeemStatus } from '../redeemStatus'
import { dollars } from '../money'

const usd = (v) => (v != null ? `$${dollars(v)}` : '—')
const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')

export default function User({ onLogout }) {
  const redeem = useRedeemStatus()
  const [profile, setProfile] = useState(null)   // quick read: name, gasless
  const [me, setMe] = useState(null)             // full read: balances
  const [meError, setMeError] = useState(null)
  const [pnl, setPnl] = useState(null)
  const [pnlError, setPnlError] = useState(null)
  const [byWallet, setByWallet] = useState(null)
  const [name, setName] = useState('')
  const [nameStatus, setNameStatus] = useState(null)   // null | 'saving' | 'saved' | Error
  const [exp, setExp] = useState(false)
  const [key, setKey] = useState('')
  const [expErr, setExpErr] = useState('')
  const [revealing, setRevealing] = useState(false)
  const [logoutErr, setLogoutErr] = useState('')
  const [loggingOut, setLoggingOut] = useState(false)
  const [reload, setReload] = useState(0)
  const savedName = useRef(null)
  const addr = getWallet()

  useEffect(() => {
    // paint the profile immediately, upgrade with balance when it lands (the
    // balance read builds the CLOB client server-side on first call — slow)
    // The quick read only fills the name field; balances come from the full
    // read alone, so a skeleton (not a dash) shows until it lands.
    api.me().then((m) => {
      setProfile(m)
      if (savedName.current == null) {
        savedName.current = m.display_name || ''
        setName(m.display_name || '')
      }
    }).catch(() => {})
    setMe(null)
    setMeError(null)
    api.me(true).then(setMe).catch((e) => setMeError(e))
    api.pnlByWallet().then(setByWallet).catch(() => setByWallet([]))
  }, [reload])

  useEffect(() => {
    // 30d: the only period-scoped thing left in this section is the heat-map's
    // calendar, and it is a fixed 30-day window.
    api.pnl('30d').then((p) => { setPnl(p); setPnlError(null) }).catch((e) => setPnlError(e))
  }, [reload])

  const saveName = useCallback(async () => {
    if (name === savedName.current) return
    setNameStatus('saving')
    try {
      await api.settings({ display_name: name })
      savedName.current = name
      setNameStatus('saved')
    } catch (e) {
      setNameStatus(e)
    }
  }, [name])

  async function reveal() {
    setExpErr('')
    // Step-up: the backend re-verifies the live Telegram identity, so the key
    // can only be revealed from inside Telegram — a stolen session isn't enough.
    const initData = window.Telegram?.WebApp?.initData || ''
    if (!initData) {
      setExpErr('Open the app from the Telegram bot to export your key.')
      return
    }
    setRevealing(true)
    try {
      const r = await api.exportKey(initData)
      setKey(r.private_key)
    } catch (e) {
      setExpErr(String(e.message || e))
    } finally {
      setRevealing(false)
    }
  }

  async function logOut() {
    setLoggingOut(true)
    setLogoutErr('')
    try {
      await performLogout({ api, clearSession: clearWallet, onLogout })
    } catch {
      setLogoutErr('Could not log out. Your session is still active; please retry.')
    } finally {
      setLoggingOut(false)
    }
  }

  const curve = pnl?.equity_curve || []
  const balancesRead = me?.balance != null

  return (
    <div>
      <Folder id="user-account" title="Account" open>
        <div className="card">
          <div className="account-row">
            <span className="account-label">Wallet</span>
            <CopyText value={addr} display={addr ? `${addr.slice(0, 10)}…${addr.slice(-8)}` : '—'} label="Copy wallet address" />
          </div>
          <label className="fld account-row">
            Display name
            <input
              value={name}
              autoComplete="nickname"
              onChange={(e) => { setName(e.target.value); setNameStatus(null) }}
              onBlur={saveName}
              onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }}
            />
            <span
              className={`fld-status ${nameStatus === 'saved' ? 'is-ok' : nameStatus instanceof Error ? 'is-error' : ''}`}
              role="status"
            >
              {nameStatus === 'saving' && 'Saving…'}
              {nameStatus === 'saved' && <><Icon name="check" className="icon icon-sm" />Saved</>}
              {nameStatus instanceof Error && `Not saved: ${describeError(nameStatus)}`}
              {nameStatus == null && 'Saved when you leave the field.'}
            </span>
          </label>

          {/* Home shows the total and the curve at a glance (see
              components/BalanceCard.jsx). The breakdown stays here, because
              "what is my money doing" is a different question from "what am I
              worth", and only one of them belongs on the glance surface. */}
          <div className="stat-grid two account-balances">
            <div className="stat-cell">
              <div className="label">Cash balance</div>
              <div className="value" style={{ '--len': usd(me?.balance).length }}>{me == null && !meError ? <span className="sk sk-line" /> : usd(me?.balance)}</div>
            </div>
            <div className="stat-cell">
              <div className="label">In positions</div>
              <div className="value" style={{ '--len': usd(me?.positions_value).length }}>{me == null && !meError ? <span className="sk sk-line" /> : usd(me?.positions_value)}</div>
            </div>
            <div className="stat-cell">
              <div className="label" title={redeem.auto ? 'Resolved wins, redeemed to your balance automatically' : 'Resolved wins not yet redeemed — claim on polymarket.com'}>Claimable</div>
              <div className="value" style={{ '--len': usd(me?.claimable).length }}>{me == null && !meError ? <span className="sk sk-line" /> : usd(me?.claimable)}</div>
            </div>
            <div className="stat-cell">
              <div className="label">Equity (total)</div>
              <div className="value" style={{ '--len': usd(me?.equity).length }}>{me == null && !meError ? <span className="sk sk-line" /> : usd(me?.equity)}</div>
            </div>
          </div>
          {meError && (
            <Notice
              tone="error"
              action={<button type="button" className="btn btn-ghost btn-sm" onClick={() => setReload((n) => n + 1)}>Retry</button>}
            >
              Balances couldn’t be read. {describeError(meError)}
            </Notice>
          )}
          {!meError && me && !balancesRead && (
            <Notice
              tone="error"
              action={<button type="button" className="btn btn-ghost btn-sm" onClick={() => setReload((n) => n + 1)}>Retry</button>}
            >
              Your live balance couldn’t be read right now.
            </Notice>
          )}
          {!meError && balancesRead && me.equity === 0 && (
            <p className="small muted">Fund the wallet to start trading.</p>
          )}
          {me?.claimable > 0 && (
            <Notice>
              <b>${dollars(me.claimable)} in resolved winnings</b>{' '}
              {redeem.auto
                ? `is being redeemed to your balance automatically and arrives within ${redeemWithin(redeem)}.`
                : <>isn&apos;t auto-claimed yet — redeem it on polymarket.com to turn it into spendable cash.</>}
            </Notice>
          )}
        </div>
      </Folder>

      <Folder id="user-fund" title="Fund wallet">
        <FundingAccess gasless={(me || profile)?.gasless} />
      </Folder>

      {/* Present only for accounts the server offers withdrawals to; the
          panel loads its balances when the folder opens. */}
      {(me || profile)?.withdrawals_enabled && (
        <Folder id="user-withdraw" title="Withdraw">
          <WithdrawPanel />
        </Folder>
      )}

      <Folder id="user-performance" title="Performance" open>
        {pnlError ? (
          <Notice
            tone="error"
            action={<button type="button" className="btn btn-ghost btn-sm" onClick={() => setReload((n) => n + 1)}>Retry</button>}
          >
            Performance couldn’t be loaded. {describeError(pnlError)}
          </Notice>
        ) : (
          <StatGrid pnl={pnl} />
        )}

        {/* The window buttons went with the chart they used to drive. Nothing
            left in this section is period-scoped: StatGrid already shows 7D and
            30D side by side, and the heat-map is a fixed 30-day calendar. */}
        <div className="section-header">Daily P&amp;L, last 30 days</div>
        <div className="card">
          {pnl == null && !pnlError ? <div className="sk sk-chart" /> : <PnlHeatmap curve={curve} />}
        </div>

        <Folder id="user-breakdown" title="Breakdown by copied wallet" count={byWallet?.length || undefined}>
          {byWallet == null ? (
            <div className="sk-card"><div className="sk sk-title" /><div className="sk sk-line" /></div>
          ) : byWallet.length === 0 ? (
            <StateBlock icon="positions" title="No closed positions yet">
              Realized results per copied wallet appear here once positions close.
            </StateBlock>
          ) : (
            <div className="card">
              {byWallet.map((w) => (
                <div className="breakdown-row" key={w.trader_address}>
                  <span>
                    <b>
                      {w.trader_address === 'manual'
                        ? 'Manual trades'
                        : w.display_name || short(w.trader_address)}
                    </b>
                    <small>{w.closed_trades} closed · {Math.round((w.win_rate || 0) * 100)}% won</small>
                  </span>
                  <span className={`mono ${w.realized_pnl >= 0 ? 'pos' : 'neg'}`}>
                    {w.realized_pnl >= 0 ? '+' : '−'}${dollars(Math.abs(w.realized_pnl))}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Folder>
      </Folder>

      <Folder id="user-security" title="Security">
        <div className="card">
          <p className="security-copy">
            Your private key gives full control of the wallet and everything in it. Export it only to
            back it up somewhere offline you trust.
          </p>
          <button type="button" className="btn btn-danger btn-block" onClick={() => setExp(true)}>
            <Icon name="key" />Export private key
          </button>
        </div>
      </Folder>

      <Folder id="user-legal" title="Legal">
        <div className="card legal-copy">
          Real trades execute on Polymarket. Prediction markets carry risk of total loss. You
          are solely responsible for the funds in your custodial wallet and for backing up your
          private key. Not available in restricted jurisdictions.
        </div>
      </Folder>

      <div className="logout-block">
        <p className="muted logout-note">Logging out ends this session. Reopening PolyTrade from Telegram signs you in automatically.</p>
        {logoutErr && <div className="warn-box" role="alert">{logoutErr}</div>}
        <button type="button" className="btn btn-ghost btn-block" disabled={loggingOut} onClick={logOut}>
          <Icon name="logout" />
          {loggingOut ? 'Logging out…' : 'Log out'}
        </button>
      </div>

      {exp && (
        <Modal
          title="Export private key"
          accent="red"
          onClose={() => { setExp(false); setKey(''); setExpErr('') }}
        >
          <Notice tone="error">
            <b>Your private key gives full access to your funds.</b> Never share it, never paste it
            into a website, and store it offline.
          </Notice>
          {!key ? (
            <>
              {expErr && <p className="neg modal-lede" role="alert">{expErr}</p>}
              <div className="modal-actions">
                <button type="button" className="btn btn-danger is-strong" onClick={reveal} disabled={revealing}>
                  {revealing ? 'Verifying with Telegram…' : 'Reveal key'}
                </button>
              </div>
            </>
          ) : (
            <div className="addr key-reveal" aria-label="Private key">{key}</div>
          )}
        </Modal>
      )}
    </div>
  )
}
