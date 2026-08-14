import { useEffect, useId, useMemo, useState } from 'react'
import { api } from '../api'
import Sparkline from './Sparkline'
import { formatMoney } from './traderCardModel'
import { analysisMetrics, positionPreview, selectCurrentEvidence, sliceDailyPnl } from './traderAnalysisModel'

const unavailable = '—'
const shortMoney = (value, signed = false) => formatMoney(value, { signed })
const cents = (value) => value == null || !Number.isFinite(Number(value))
  ? unavailable
  : `${(Number(value) * 100).toFixed(1)}¢`
const shares = (value) => value == null || !Number.isFinite(Number(value))
  ? unavailable
  : `${Math.round(Number(value)).toLocaleString('en-US')}sh`
const percent = (value) => value == null || !Number.isFinite(Number(value))
  ? unavailable
  : `${(Number(value) * 100).toFixed(0)}%`

function fetchedStamp(value) {
  if (!value) return 'FETCHED; REFRESH TIME UNAVAILABLE'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'FETCHED; REFRESH TIME UNAVAILABLE'
  return `FETCHED · STATS REFRESHED ${date.toISOString().slice(0, 16).replace('T', ' ')} UTC`
}

function activityAge(timestamp) {
  const numeric = Number(timestamp)
  if (!Number.isFinite(numeric)) return unavailable
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - numeric))
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ago`
  if (elapsed < 86400) return `${Math.floor(elapsed / 3600)}h ago`
  return `${Math.floor(elapsed / 86400)}d ago`
}

export default function TraderAnalysis({ address, trader, period }) {
  const sectionId = `trader-analysis-detail-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const [fetched, setFetched] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let current = true
    setFetched(null)
    setError(null)
    api.trader(address)
      .then((result) => {
        if (current) setFetched({ address, data: result })
      })
      .catch((reason) => {
        if (current) setError({ address, message: String(reason?.message || reason) })
      })
    return () => { current = false }
  }, [address])

  // State updates from effects occur after render. Address tags make the first
  // A→B render reject A's evidence synchronously, before cleanup/reset runs.
  const evidence = selectCurrentEvidence(address, trader, fetched)
  const currentFetched = selectCurrentEvidence(address, null, fetched)
  const hasCurrentFetch = currentFetched === fetched?.data
  const currentError = error?.address?.trim().toLowerCase() === address?.trim().toLowerCase()
    ? error.message
    : ''
  const metrics = analysisMetrics(evidence, period)
  const daily = useMemo(
    () => sliceDailyPnl(evidence.daily_pnl_90d, period, metrics.refreshedAt),
    [evidence.daily_pnl_90d, metrics.refreshedAt, period],
  )
  const positions = useMemo(() => positionPreview(currentFetched?.positions, 8), [currentFetched?.positions])
  const positionsAvailable = hasCurrentFetch && Array.isArray(currentFetched?.positions)
  const activityAvailable = hasCurrentFetch && Array.isArray(currentFetched?.recent_trades)
  const activity = activityAvailable ? currentFetched.recent_trades.slice(0, 10) : []
  const displayName = evidence.display_name && !evidence.display_name.startsWith('0x')
    ? evidence.display_name
    : 'UNNAMED TRADER'

  return (
    <div className="ta">
      <section className="ta-section" aria-labelledby={`${sectionId}-identity`}>
        <div className="section-header" id={`${sectionId}-identity`}>TRADER IDENTITY</div>
        <strong className="ta-name">{displayName}</strong>
        <span className="ta-address muted">{address || unavailable}</span>
        <div className="ta-fetch-state muted small" role="status">
          {!hasCurrentFetch && !currentError
            ? 'FETCHING LATEST STATS…'
            : hasCurrentFetch
              ? fetchedStamp(metrics.refreshedAt)
              : 'LATEST FETCH FAILED · SHOWING CACHED STATS WHERE AVAILABLE'}
        </div>
        {currentError && <div className="warn-box">Could not fetch latest trader evidence: {currentError}</div>}
      </section>

      <section className="ta-section" aria-labelledby={`${sectionId}-summary`}>
        <div className="section-header" id={`${sectionId}-summary`}>
          SELECTED PERIOD SUMMARY · {metrics.period.toUpperCase()}
        </div>
        <div className="ta-summary">
          <div><span>REALIZED PNL</span><strong className={metrics.realizedPnl == null ? '' : metrics.realizedPnl >= 0 ? 'pos' : 'neg'}>{shortMoney(metrics.realizedPnl, true)}</strong></div>
          <div><span>WIN RATE</span><strong>{percent(metrics.winRate)}</strong></div>
          <div><span>GROSS VOLUME</span><strong>{shortMoney(metrics.grossVolume)}</strong></div>
          <div><span>FETCHED TRADE HISTORY</span><strong>{metrics.historyDays == null ? unavailable : metrics.partial ? `~${metrics.historyDays}D OF ${metrics.days}D` : `~${metrics.days}D OBSERVED`}</strong></div>
        </div>
        {metrics.partial && (
          <p className="muted small">Fetched TRADE rows cover only ~{metrics.historyDays} days of this {metrics.days}-day period, so trade-derived volume is partial. PnL and win rate also use separately bounded REDEEM and positions closing evidence; those sources can be partial independently.</p>
        )}
      </section>

      <section className="ta-section" aria-labelledby={`${sectionId}-history`}>
        <div className="section-header" id={`${sectionId}-history`}>
          REALIZED PNL / CLOSE-DAY HISTORY · {metrics.period.toUpperCase()}
        </div>
        {daily == null ? (
          <div className="muted small">close-day history unavailable</div>
        ) : (
          <Sparkline daily={daily} />
        )}
        <p className="muted small">
          UTC close-day buckets are sliced to the selected period. PnL and win rate also use separately bounded REDEEM and positions closing evidence. TRADE, REDEEM, and positions sources can each be partial; days without fetched closings are omitted and the cutoff date can represent a partial day.
        </p>
      </section>

      <section className="ta-section" aria-labelledby={`${sectionId}-positions`}>
        <div className="section-header" id={`${sectionId}-positions`}>CURRENT POSITIONS</div>
        {!hasCurrentFetch ? (
          <div className="muted small">{currentError ? 'latest positions unavailable' : 'loading fetched positions…'}</div>
        ) : !positionsAvailable ? (
          <div className="muted small">fetched holdings unavailable</div>
        ) : (
          <>
            <div className="muted small ta-count">
              {positions.label} · {positions.liveCount} LIVE · {positions.redeemableCount} RESOLVED / REDEEMABLE
            </div>
            {positions.items.length === 0 ? (
              <div className="muted small">no fetched holdings with positive size</div>
            ) : positions.items.map((position, index) => (
              <div className="ta-row" key={position.asset || `${position.condition_id || 'position'}-${index}`}>
                <span className="ta-title">{position.title || 'Untitled market'}</span>
                <span className={`badge ${position.outcome === 'Yes' ? 'pos' : position.outcome === 'No' ? 'neg' : ''}`}>{position.outcome || unavailable}</span>
                {position.redeemable && <span className="badge" title="Resolved holding; redeemable rather than live">REDEEMABLE</span>}
                <span className="muted">{shares(position.size)}</span>
                <span className="muted">{cents(position.avg_price)} → {cents(position.cur_price)}</span>
                <span className={position.cash_pnl == null ? '' : position.cash_pnl >= 0 ? 'pos' : 'neg'}>{shortMoney(position.cash_pnl, true)}</span>
              </div>
            ))}
          </>
        )}
      </section>

      <section className="ta-section" aria-labelledby={`${sectionId}-activity`}>
        <div className="section-header" id={`${sectionId}-activity`}>RECENT ACTIVITY</div>
        {!hasCurrentFetch ? (
          <div className="muted small">{currentError ? 'recent activity unavailable' : 'loading recent activity…'}</div>
        ) : !activityAvailable ? (
          <div className="muted small">fetched recent activity unavailable</div>
        ) : activity.length === 0 ? (
          <div className="muted small">no fetched recent trades</div>
        ) : activity.map((trade, index) => (
          <div className="ta-row" key={trade.tx_hash || `${trade.asset || 'trade'}-${trade.timestamp || index}`}>
            <span className={trade.side === 'BUY' ? 'pos' : trade.side === 'SELL' ? 'neg' : ''}>{trade.side || unavailable}</span>
            <span className="ta-title">{trade.title || 'Untitled market'}</span>
            <span className="muted">{shares(trade.size)} @ {cents(trade.price)}</span>
            <span className="muted">{activityAge(trade.timestamp)}</span>
          </div>
        ))}
      </section>

      <aside className="ta-source-note muted small" aria-label="Data sources and limitations">
        <strong>DATA SOURCES / LIMITATIONS</strong>
        <span>PNL AND WIN RATE ARE RECONSTRUCTED FROM FETCHED CLOSING EVENTS.</span>
        <span>PNL AND WIN RATE ALSO USE SEPARATELY BOUNDED REDEEM AND POSITIONS CLOSING EVIDENCE.</span>
        <span>TRADE, REDEEM, AND POSITIONS SOURCES CAN EACH BE PARTIAL.</span>
        <span>GROSS VOLUME USES FETCHED TRADE ROWS.</span>
        <span>POSITIONS MAY TRUNCATE AT 500 ROWS; COUNTS AND PREVIEWS CAN THEREFORE BE LOWER BOUNDS.</span>
      </aside>
    </div>
  )
}
