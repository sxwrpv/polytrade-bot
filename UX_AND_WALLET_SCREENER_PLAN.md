# UI/UX Restructure + Wallet Screener — Plan

> Extends `BUILD_PLAN.md`. Scope: (1) restructure the existing frontend into a navigable,
> metric-rich, nested-folder product without changing the cyber-brutalism visual language,
> and (2) design the wallet parser / screener with simultaneous multi-filter support
> (win rate 7d/30d/90d, PnL, consistency, volume). Spec only — no code written yet.

---

## 0. Locked decisions

| Decision | Choice |
|---|---|
| Visual style | Keep cyber-brutalism as-is (matrix green, sharp corners, monospace, no shadows/gradients). Fix is information architecture and data density, not the theme. |
| Navigation | Keep the 3 bottom tabs (HOME / POSITIONS / USER). No new top-level tabs — the screener lives inside HOME. Depth comes from nested `Folder` components. |
| New feature | Wallet Screener: multi-filter, all filters combinable (AND), backed by precomputed per-trader stats so filtering stays instant. |
| Consistency metric | Additive, not a replacement. Existing `consistency_score` (sharpe-based, drives tier badges) stays untouched. New "green days vs red days" + "rpnl − upnl" are separate, transparent, filterable fields. |

---

## 1. Current-state audit

- `Home.jsx` sort is 4 mutually-exclusive chips (pick one dimension, re-fetch) — not filtering, and nothing is combinable.
- `trader_cache` stores only all-time `win_rate`, `total_pnl`, `volume_usd`, `consistency_score`. No 7d/30d/90d windows anywhere.
- `consistency_score` is one opaque 0–1 number (win rate × 0.4 + sharpe × 0.4 + mean × 0.2). It doesn't expose green/red day counts or realized-vs-unrealized quality — which is specifically what you asked to filter on.
- `Folder.jsx` already supports nesting structurally (it just renders `children`, which can themselves be `Folder`s), but the only place it's actually nested is `CopiedWallets → WalletRiskCard`. Everywhere else is flat.
- No glanceable KPIs outside the USER tab — balance/PnL/open-count aren't visible from HOME or POSITIONS.
- Charts exist in exactly one place (`User > Performance > PnLChart`). Nothing on HOME or POSITIONS despite both having chart-worthy data (green/red days, exposure over time).
- Loading states are plain `"loading…"` text; most `.catch(() => {})` calls silently swallow errors — fine for an MVP, not for a "mass product."

---

## 2. Information architecture (nested folder tree)

```
HOME
 ├─ [persistent, non-collapsible] KPI strip: balance · today's PnL · open positions · # copied wallets
 ├─ Folder: MARKET PULSE          (marquee + hot markets grid; auto-collapses after first visit, via localStorage)
 ├─ Folder: WALLET SCREENER       (NEW — §3)
 │   ├─ Folder (nested): FILTERS   (open on desktop, collapsed by default <480px)
 │   └─ Results: card grid ⇄ dense table toggle
 └─ Folder: COPIED WALLETS        (existing CopiedWallets)
     └─ per wallet → Folder (nested, existing WalletRiskCard pattern)
         extend with: green/red day dot-strip + mini sparkline

POSITIONS
 ├─ StatGrid summary strip
 │    open tab: total exposure · unrealized PnL · open count
 │    closed tab: realized PnL · win rate · best/worst trade
 ├─ Folder: OPEN
 └─ Folder: CLOSED               (default-collapsed rows beyond 20, paginate)

USER
 ├─ Folder: ACCOUNT              (existing, unchanged)
 ├─ Folder: PERFORMANCE
 │    ├─ StatGrid                (existing)
 │    ├─ PnL line chart          (existing)
 │    ├─ NEW: green/red day heatmap (13-week calendar strip, GitHub-contributions style)
 │    └─ Folder (nested): BREAKDOWN BY COPIED WALLET — per-wallet PnL contribution, small bar chart
 ├─ Folder: SECURITY             (existing)
 ├─ Folder: REFERRAL             (existing)
 └─ Folder: LEGAL                (existing)
```

Cross-cutting additions:
- **Collapse-all / expand-all** control (top-right) on any page with 3+ folders (USER, HOME screener).
- **Persisted folder state** — `Folder.jsx` gets a required `id` prop; open/closed state read/written to `localStorage` (`folder:<id>`) so the UI remembers what the user had open across reloads. Currently every folder resets to its hardcoded `open` default on every mount.

---

## 3. Wallet Screener — core feature

### 3.1 Filters and exact formulas

| Metric | Windows | Formula | New stored column(s) |
|---|---|---|---|
| Win rate | 7d / 30d / 90d | wins ÷ closing trades in window, avg-cost basis (existing `_avg_cost_walk`, just date-filtered) | `winrate_7d`, `winrate_30d`, `winrate_90d` |
| PnL | 7d / 30d / 90d | sum of realized PnL in window (existing `daily_realized_pnl`, summed over cutoff) | `pnl_7d`, `pnl_30d`, `pnl_90d` |
| Volume | 7d / 30d / 90d | sum of trade `usd_size` in window | `volume_7d`, `volume_30d`, `volume_90d` |
| Consistency (green/red days) | 7d / 30d / 90d | `green_days` = # days with realized PnL > 0; `red_days` = # days with realized PnL < 0; `consistency_ratio` = green ÷ (green + red) | `green_days_Xd`, `red_days_Xd`, `consistency_ratio_Xd` |
| Consistency (PnL quality) | snapshot (unrealized is inherently "now") | `pnl_quality = realized_pnl_all_time − unrealized_pnl_now`. Large positive = gains are banked. Very negative = trader is sitting on big unrealized winners that haven't been proven closeable — a real risk signal for a copier, since you inherit that same unclosed exposure. | `pnl_quality` |
| (unchanged) `consistency_score` | all-time | existing sharpe formula — stays as the tier-badge / default-sort metric | *(no change)* |

Why keep both consistency metrics: `consistency_score` is a good single-number ranking signal but hides its reasoning. You explicitly asked to see and filter on green-vs-red day counts and realized-vs-unrealized quality — those need to stay as separate, legible fields rather than being folded into one score.

### 3.2 Why "simultaneous filters" requires precomputation

Computing win rate/PnL/volume live, per request, across every leaderboard trader means N calls to `get_trade_history` per screener query — too slow to feel instant when someone is stacking multiple filters at once. So:

- A background refresh job (the schedule already implied by "`trader_cache` is populated from the leaderboard endpoint on a schedule" in `BUILD_PLAN.md`) walks the cached trader set and computes all window columns above, writing them into `trader_cache`.
- The screener endpoint becomes a single indexed SQL `SELECT ... WHERE col1 >= ? AND col2 >= ? ...` — flat latency no matter how many filters are combined, because it never calls the Polymarket API at request time.
- Refresh cadence: every ~15 min for the cached leaderboard set (start with top 100–200 by volume). The existing on-demand `GET /traders/{address}` stays as-is for the single-trader detail view (still live).

### 3.3 API

Extend `GET /api/traders/leaderboard` (don't fork a new endpoint — keep the existing simple call working with zero params, add optional filters that AND together):

```
GET /api/traders/leaderboard
  ?sort=consistency|pnl|winrate|volume|pnl_quality      (existing + new)
  &winrate_7d_min=&winrate_30d_min=&winrate_90d_min=     (0–1)
  &pnl_7d_min=&pnl_30d_min=&pnl_90d_min=                 (usd, may be negative)
  &volume_7d_min=&volume_30d_min=&volume_90d_min=        (usd)
  &consistency_period=7d|30d|90d (default 30d)
  &consistency_ratio_min=                                (0–1)
  &pnl_quality_min=
  &limit=&offset=
```

Implementation: mirror the existing `_SORT_COLS` whitelist pattern in `trader_stats.py` — map each query param to a real column via a fixed dict, build `WHERE` fragments dynamically from whatever params are present, always parametrized (never string-interpolated). This is the same defense already used for `sort`, just generalized to N optional clauses.

### 3.4 Schema delta (`db/models.py`)

Add to `trader_cache`, plus matching `MIGRATIONS` `ALTER TABLE` lines (same idempotent pattern already used there):

```
winrate_7d REAL, winrate_30d REAL, winrate_90d REAL,
pnl_7d REAL, pnl_30d REAL, pnl_90d REAL,
volume_7d REAL, volume_30d REAL, volume_90d REAL,
green_days_7d INTEGER, red_days_7d INTEGER,
green_days_30d INTEGER, red_days_30d INTEGER,
green_days_90d INTEGER, red_days_90d INTEGER,
consistency_ratio_7d REAL, consistency_ratio_30d REAL, consistency_ratio_90d REAL,
pnl_quality REAL
```

Add indexes on the columns most likely to be filtered (`winrate_30d`, `pnl_30d`, `volume_30d`, `consistency_ratio_30d`) — same as the existing `idx_trader_cache_consistency`.

### 3.5 Frontend

- `Folder: WALLET SCREENER` → nested `Folder: FILTERS`:
  - Period toggle (7D / 30D / 90D chips) scoping the win rate / PnL / volume inputs below it.
  - "At least" numeric inputs for win rate %, PnL $, volume $ (reuse existing `.fld`/`input` styling — no new CSS system needed).
  - Consistency: period toggle + ratio input, or 3 preset chips (`>50% green`, `>70% green`, `>85% green`) for a faster first pass.
  - "Show PnL quality" toggle → adds a `pnl_quality` badge to result cards when on.
  - Active-filter chip row, each dismissible individually, plus "CLEAR ALL."
  - Debounced (~300ms) auto-apply on every change — no explicit search button; this is what makes "simultaneous filters" feel simultaneous rather than staged.
- Result cards: extend `TraderCard` with the active period's win rate/PnL/volume, plus a compact 30-dot green/red/gray strip — the green-vs-red-days concept made visible per trader at a glance, not just filterable.
- View toggle: cards (default, mobile) vs. a dense sortable table (desktop) for anyone comparing many wallets at once.

---

## 4. Charts — where they actually earn their place

| Location | Chart | Why |
|---|---|---|
| User > Performance | PnL line (existing) | keep as-is |
| User > Performance | Green/red day heatmap (new) | direct visualization of the consistency metric you asked for |
| Screener results / TraderCard | 30-dot green/red/gray strip (new, not a full chart — cheaper, scannable at list density) | same concept, compressed for list view |
| User > Performance > Breakdown | Per-copied-wallet PnL bar chart (new) | answers "which of my copied wallets is actually making money" |
| Positions | *(no chart)* — StatGrid only | single numbers (exposure, win rate) read faster than a chart here; avoid chart-for-chart's-sake |

---

## 5. Polish pass

- Skeleton placeholders (pulsing bordered rectangles, CSS-only, stays brutalist) instead of `"loading…"` text.
- A single error toast/strip component instead of the current scattered `.catch(() => {})` silent failures.
- `Folder` open/closed state persisted to `localStorage`.
- Collapse-all / expand-all control.

---

## 6. Build sequence

| # | Phase | Output |
|---|---|---|
| 1 | Schema + windowed metrics | `models.py` columns/migrations, `trader_stats.py` windowed calc functions |
| 2 | Screener endpoint | extend `/traders/leaderboard` with whitelisted optional filter params |
| 3 | Background refresh wiring | periodic job populating the new columns for the cached leaderboard set |
| 4 | `Folder.jsx` upgrade | add `id` prop, localStorage persistence, collapse-all helper |
| 5 | Screener UI | filters panel (nested Folder) + results, card/table toggle |
| 6 | `TraderCard` upgrade | period stat row + green/red dot-strip |
| 7 | User > Performance | heatmap + per-wallet breakdown folder |
| 8 | Positions | StatGrid summary strip |
| 9 | Polish | skeletons, toasts, persistent KPI header on HOME |

---

## 7. Open items

- `get_trade_history(limit=500)` may not cover a full 90-day window for very active traders — either paginate or accept it as an approximation and surface a small tooltip ("based on last 500 trades") next to 90d filters.
- Should `pnl_quality_min` have a safer non-zero default (hide traders with very negative quality by default) or stay fully opt-in? Recommend opt-in for v1 — don't hide traders without the user asking.
- Cards-only vs. cards + dense table for v1 — table is more work for a feature only power users will reach for; fine to ship cards first and add table view as a fast follow.
