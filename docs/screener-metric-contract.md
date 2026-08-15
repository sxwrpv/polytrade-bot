# Screener metric contract

This document describes the metrics currently exposed by the wallet screener. The canonical machine-readable version is `backend.core.trader_stats.SCREENER_METRIC_CONTRACT`; tests require every entry to carry its formula, source, window, limits, cadence, null/partial behavior, provenance, sort/filter safety, label, and tooltip.

## Shared provenance and availability rules

Reconstructed metrics use these public Polymarket Data API reads:

- `GET https://data-api.polymarket.com/activity?user={address}&type=TRADE`
- `GET https://data-api.polymarket.com/activity?user={address}&type=REDEEM`
- `GET https://data-api.polymarket.com/positions?user={address}`

Each activity page requests 1,000 rows. Refreshes fetch at most four TRADE pages (4,000 rows), two REDEEM pages (2,000 rows), and one 500-row positions snapshot. The target history is 90 days, but the final page can include older rows. `history_days` detects a TRADE page-budget shortfall only; it does **not** certify REDEEM or positions completeness. A positions response of 500 rows may be truncated.

The scheduled refresh loop is enabled by default, runs immediately at startup, and then defaults to one pass every 900 seconds. Each pass refreshes only the 200 stalest wallets by default. Set `STATS_REFRESH_AUTOSTART=0` to disable this scheduled loop. Both budget values are configurable, and stale-first population rotation means there is no per-wallet 15-minute freshness guarantee. Independently of that schedule and toggle, authenticated `GET /traders/{address}` performs an immediate on-demand refresh before returning the wallet profile.

Cache storage uses `null` for metrics that have not been fetched or cannot be computed, except for the schema's legacy `open_positions DEFAULT 0`; that default is unavailable while `stats_refreshed_at` is null. After a complete fetch, a computed zero can be a genuine observation (for example, zero SELL rows). Page-budget shortfalls are partial observations, not zeroes. A failed refresh leaves the prior cache row stale rather than manufacturing a new value.

**Current unsafe consumer limitation:** `TraderCard` still coerces several missing values to numeric zero, and backend tier assignment coerces a missing `consistency_score` to zero/bronze. Thus cache semantics say “unavailable,” but current rendering/tier behavior does not always preserve that distinction. Task 2.1 documents this factual mismatch; the full consumer cleanup belongs to Task 2.4.

**Legacy transition:** cache rows written before nullable ratio metrics may still contain numeric zero sentinels indistinguishable from genuine observed zero until each wallet is refreshed. Stale-first rotation has no per-wallet completion SLA, so those legacy rows remain a temporary sorting/filtering limitation and a stored zero alone must not be interpreted as a confirmed observation.

Realized closing events are reconstructed by `realized_closings` with these exact rules:

- **SELL:** only when basis is known (`held shares > 0`); sold shares are `min(SELL size, held shares)`, PnL is `(SELL price - average cost) * sold shares`, and the event is a win only when `SELL price > average cost`.
- **REDEEM:** only with known cost `> 0`; PnL is `payout - cost`, and the event is a win only when `payout > cost`.
- **Resolved held position:** only when `size > 0.01` and `redeemable`; PnL is API `cash_pnl`, and the event is a win when `cur_price >= 0.5`.
- **Expired-away position:** a loss of residual cost only when `shares > 0.01`, `cost > 0.005`, the asset is not still held, and the positions list is not truncated. This truncation guard prevents absence from being treated as proof when the 500-row snapshot may be incomplete.

Basis that predates fetched activity can be unknown, so these figures are estimates.

## Official totals and fallback estimates

### `total_pnl` — **Total PnL**

- **Formula:** use `pnl` from the first row of `GET https://data-api.polymarket.com/v1/leaderboard?category=OVERALL&timePeriod=ALL&orderBy=PNL&user={address}`. Only an explicit successful no-row result permits fallback: if the cached field is null or absent, use `sum(fetched reconstructed realized closings) + current open non-redeemable position cash_pnl`; if it is already non-null, preserve it.
- **Failure and cached provenance:** if the official lookup fails with an exception, the refresh preserves the cached totals and does not reconstruct replacements. Because there is no source/provenance column, any existing non-null cached value preserved after no-row has **unknown/legacy provenance**; it must not be represented as official. It may have originated in an official ALL seed, an earlier reconstructed fallback, or legacy data.
- **Window/provenance:** a value returned by the successful leaderboard request is official `ALL`-period data. A newly written fallback is reconstructed and fetch-bounded; it has no lifetime claim.
- **Sources:** the fallback uses the exact TRADE, REDEEM, and positions endpoints listed above. Its truncation risks are therefore TRADE (four 1,000-row pages), REDEEM (two 1,000-row pages), and positions (500 rows).
- **Limits/partial:** the user-filtered official request sends no explicit row limit, and only the first returned row is used. The reconstructed fallback is partial.
- **Sort/filter:** safe and currently supported for all stored provenance states.
- **Tooltip:** “Polymarket's official ALL-period PnL when returned. Only an explicit no-row result may fill a missing value with a fetch-bounded estimate; lookup failure preserves the cache.”

### `volume_usd` — **Total Volume**

- **Formula:** use `vol` from the same first official user-filtered `ALL` leaderboard row. Only an explicit successful no-row result permits fallback: preserve an existing non-null cached value, or if missing use `sum(usd_size)` over fetched TRADE rows.
- **Failure and cached provenance:** the same exception-preservation and unknown/legacy provenance rules as `total_pnl` apply; a cached non-null value is not claimed official without an explicit source field.
- **Window/provenance:** a freshly returned leaderboard value is official `ALL`-period data. A newly written fallback is a clearly partial, fetch-bounded reconstruction.
- **Sources/limits/partial:** the fallback depends only on TRADE, so only the four-page TRADE budget can truncate it. It does not depend on REDEEM or positions. The official request has no explicit row limit and uses only the first returned row.
- **Sort/filter:** safe and currently supported for all stored provenance states.
- **Tooltip:** “Polymarket's official ALL-period volume when returned; after explicit no-row, a missing value may use a clearly partial sum of fetched TRADE rows.”

## Fetch-bounded summary metrics

All metrics in this section are reconstructed, use the shared limits/cadence, become partial when a source truncates, and must be unavailable rather than zero before enrichment.

### `win_rate` — **Observed Win Rate**

- **Formula:** winning reconstructed closing-event count divided by all reconstructed closing-event count in the fetched walk; null when there are no closing events.
- **Window:** fetched history used by the 90-day walk. A whole boundary page can add older events; this is not a lifetime rate.
- **Sort/filter:** safe and supported for both.
- **Tooltip:** “Share of reconstructed fetched closing events that won; coverage is fetch-bounded.”

### `open_positions` — **Open Positions**

- **Formula:** count positions where `size > 0` and `redeemable == false`.
- **Window:** current positions snapshot; at the 500-row cap the count is only a lower bound.
- **Sort/filter:** filtering is supported; sorting is not.
- **Tooltip:** “Open non-redeemable positions in the fetched snapshot; 500 is a lower-bound truncation risk.”

### `consistency_score` — **Consistency**

- **Formula:** with fewer than seven observed PnL days, `0`; otherwise `0.4 × positive_day_fraction + 0.4 × clamp((mean / sample_stdev) / 3, 0, 1) + 0.2 × clamp(mean / 100, 0, 1)`, rounded to four decimals. When standard deviation is zero, its component is zero.
- **Window:** observed realized-PnL dates in the fetched walk. Calendar dates with no closing event are omitted, not inserted as zeroes.
- **Sort/filter:** safe and supported for both.
- **Tooltip:** “Reconstructed steadiness score over observed PnL days; 0 can mean fewer than 7 observed days, not poor performance.”

### `pnl_quality` — **PnL Quality**

- **Formula:** `sum(reconstructed realized closings in fetched history) - sum(cash_pnl for current open non-redeemable positions)`.
- **Window:** mixed horizons: fetch-bounded realized events and a current unrealized snapshot. It is not an official performance total.
- **Sort/filter:** safe and supported for both.
- **Tooltip:** “Reconstructed fetched realized PnL minus current open-position PnL; realized and snapshot terms use different horizons.”

## Windowed metrics

Every metric below exists for `7d`, `30d`, and `90d`. TRADE rows use an exact rolling-seconds cutoff (`refresh_time - days × 86,400`). Closing events use a UTC date-string cutoff, so the boundary is calendar-day based. All are reconstructed and can be partial under the shared fetch budgets.

### `winrate_7d`, `winrate_30d`, `winrate_90d`

- **Labels:** **7d/30d/90d Win Rate**.
- **Formula:** winning reconstructed closing-event count in the window divided by all reconstructed closing-event count in the window; null when there are no closing events.
- **Sort/filter:** supported for both.
- **Tooltip:** “Share of reconstructed closing events that won. Partial when source page budgets truncate coverage.”

### `pnl_7d`, `pnl_30d`, `pnl_90d`

- **Labels:** **7d/30d/90d Realized PnL**.
- **Formula:** sum reconstructed realized PnL for closing events in the window.
- **Sort/filter:** supported for both.
- **Tooltip:** “Reconstructed realized PnL from fetched closing events. Partial when source page budgets truncate coverage.”

### `volume_7d`, `volume_30d`, `volume_90d`

- **Labels:** **7d/30d/90d Volume**.
- **Formula:** sum `usd_size` for fetched TRADE rows in the rolling-seconds window.
- **Sort/filter:** supported for both.
- **Tooltip:** “Fetched traded notional; page truncation can undercount it.”

### `green_days_7d`, `green_days_30d`, `green_days_90d`

- **Labels:** **7d/30d/90d Green Days**.
- **Formula:** count UTC dates whose summed reconstructed realized PnL is greater than zero.
- **Sort/filter:** neither is currently supported.
- **Tooltip:** “Observed UTC closing days with positive reconstructed realized PnL.”

### `red_days_7d`, `red_days_30d`, `red_days_90d`

- **Labels:** **7d/30d/90d Red Days**.
- **Formula:** count UTC dates whose summed reconstructed realized PnL is less than zero.
- **Sort/filter:** neither is currently supported.
- **Tooltip:** “Observed UTC closing days with negative reconstructed realized PnL.”

### `consistency_ratio_7d`, `consistency_ratio_30d`, `consistency_ratio_90d`

- **Labels:** **7d/30d/90d Green-Day Ratio**.
- **Formula:** `green_days / (green_days + red_days)`; zero-PnL and absent dates are excluded. The ratio is null when there are no green or red days.
- **Sort/filter:** filtering is supported; sorting is not.
- **Tooltip:** “Green days as a share of observed non-zero PnL days.”

### `fills_7d`, `fills_30d`, `fills_90d`

- **Labels:** **7d/30d/90d Buy Fills**.
- **Formula:** count fetched TRADE activity rows in the window where `side == BUY`. This is an activity-row count, not a unique order or position count.
- **Sort/filter:** neither is currently supported.
- **Tooltip:** “Fetched BUY activity-row count, not order or position count.”

### `exits_7d`, `exits_30d`, `exits_90d`

- **Labels:** **7d/30d/90d Sell Exits**.
- **Formula:** count fetched TRADE activity rows in the window where `side == SELL`; REDEEM events are excluded.
- **Sort/filter:** neither is currently supported.
- **Tooltip:** “Fetched SELL activity-row count; redemptions are not included.”

### `fill_exit_ratio_7d`, `fill_exit_ratio_30d`, `fill_exit_ratio_90d`

- **Labels:** **7d/30d/90d Exit/Fill Ratio**.
- **Formula:** `round(SELL TRADE row count / BUY TRADE row count × 100, 2)`; null when there are no BUY rows.
- **Sort/filter:** supported for both.
- **Tooltip:** “Fetched SELL-to-BUY activity-row ratio as a percentage.”

## Sparkline and freshness metadata

### `daily_pnl_90d` — **90d Daily PnL**

- **Formula:** compact JSON object `{UTC date: sum(reconstructed realized PnL)}` retaining dates on or after (inclusive) the UTC date 90 days before refresh. Dates with no closing are omitted.
- **Source/window/provenance:** shared reconstructed sources and limits. The inclusive cutoff can span 91 UTC date labels (the cutoff date through the refresh date), although omitted no-closing dates usually produce fewer keys; it is therefore not exactly 90 date labels. A resolved holding with a missing fetched trade timestamp remains undated and is excluded from daily and rolling windows, though aggregate observed/fallback PnL and win rate include it. Other reconstructed outcomes use event or last-trade dates.
- **Sort/filter:** neither.
- **Tooltip:** “Reconstructed daily realized PnL points for the sparkline; missing dates are not zero-PnL claims.”

### `history_days` — **History Coverage**

- **Formula:** `90.0` when TRADE activity exhausts or crosses the 90-day cutoff; otherwise `round((refresh_time - oldest_fetched_trade_timestamp) / 86,400, 1)`.
- **Source/window/provenance:** reconstructed TRADE coverage indicator capped at 90. It says nothing definitive about REDEEM or positions completeness.
- **Sort/filter:** numeric minimum/maximum filtering is supported; sorting is not supported. Disabling partial-history inclusion applies `history_days >= selected period`, so null and shorter-coverage rows are excluded. This only checks fetched TRADE history and does not certify REDEEM or positions completeness.
- **Tooltip:** “Approximate fetched TRADE coverage used to flag partial windows; it does not prove every source is complete.”

### `stats_refreshed_at` — **Stats Updated**

- **Formula/source:** UTC ISO-8601 value from `backend.db.database.now_iso()` when reconstructed stats are computed, before cache upsert; no external endpoint.
- **Window/cadence:** computation point in time. It is null until first successful enrichment and is not changed by discovery-only upserts. The shared stale-first schedule applies, without a per-wallet SLA.
- **Sort/filter:** neither.
- **Tooltip:** “When reconstructed screener statistics were last computed; discovery-only cache writes do not change it.”
