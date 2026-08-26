# Trader Screener

A Polymarket wallet screener in the PolyTrade surface: rank wallets by what
survives being copied, not by bankroll. Two data layers behind one board —

* **the board** reads a cached snapshot of a ranked cohort (3,766 wallets) for
  the derived signals that need a full-chain indexer: Copy Score, the
  market-maker / arbitrage / frequency classifiers, weekly sparklines, and the
  235 per-slice "angle" boards;
* **every wallet page** is computed at request time from Polymarket's own
  public read APIs — no key, no session, read only.

No build step. Node 20+, zero runtime dependencies.

```bash
node scripts/ingest.mjs   # refresh the cohort snapshot into data/
node server.mjs           # http://localhost:4310
node --test tests/*.test.mjs
```

## Using it

**The board.** Order by Copy Score, PnL, ROI or Volume — either from the sidebar
or by clicking a column header, which toggles direction on the active column and
starts at descending on a new one. Seven numeric sliders narrow the cohort: PnL,
volume, ROI, win rate, Copy Score, active days, and *average fill at most* —
that last one is a ceiling rather than a floor, because a wallet whose average
fill is $220K is one you cannot mirror at a retail budget however good its
score. A checkbox drops market makers, arbitrage and very-high-frequency
wallets in one move.

Every active filter appears as a chip you can click to clear, and the
non-clearable ones (the volume floor the ordering imposes, the band filter, the
30-day freshness cut) are shown too, dashed, so nothing is narrowing the board
invisibly.

**Sharing.** The whole view lives in the query string, so a link reproduces the
board it was copied from. Only non-defaults are written, so a bare `/` is always
the default board and a shared link says exactly what it changed. **Copy link**
and **CSV** sit beside the wallet count; the CSV is the board exactly as
filtered and ordered, and an absent metric exports as an empty cell, never a
zero.

**Saved wallets.** The heart on any row — or in a wallet page's header — keeps
that wallet in a saved list, and the **Saved** pill in the nav opens a drawer
holding it. The list is kept in `localStorage` on that browser only: it is not
an account, nothing is sent anywhere, and clearing site data clears it. The
drawer says so rather than letting it read as synced.

Each saved record snapshots the wallet's name and score at save time, because
the cohort behind the board turns over — a wallet saved last week may not be in
this week's ranked set at all, and without the snapshot the drawer would show a
bare address. Where the wallet *is* still in the cohort the drawer shows the
current figure; where it is not, it shows the snapshot tagged **as saved**. The
sidebar's Saved view and the drawer read the same store, so they cannot drift.
Anything in the older "Following" list is carried over on first load.

**Staleness.** The cohort snapshot's age is stated under the board, and past two
days — upstream regenerates daily — a banner says so outright rather than
letting stale rankings read as current. Wallet pages are unaffected; they read
Polymarket live.

**The charts are interactive.** The wallet page's series chart takes a crosshair
on hover or arrow keys on focus, with a readout showing the date, the value and
the change since the start of the visible window; shape (cumulative, daily,
drawdown, volume, trades) and window (7D/30D/90D/ALL) are separate switches, so
changing one does not reset the other. The trading clock is a real heat-map:
hover any cell for its exact buy/sell split and notional, click to pin one,
arrow keys to walk the grid, and a switch between fills, buys, sells and
notional that recomputes the row and column totals. Intensity rides on a square
root so a single very busy hour cannot flatten every other hour to the same
tint.

---

## What was reverse-engineered, and how confident each part is

The board's behaviour was recovered from the shipped client bundle and from the
published dataset. Constants are labelled in the source so nothing inherited is
mistaken for something derived:

**VERBATIM** — lifted from the deobfuscated bundle, exact:

| Rule | Value |
|---|---|
| Window volume floor for rate-based orders | `{ d7: 25_000, d30: 60_000, all: 100_000 }` |
| Lifetime board hides wallets stale for | 30 days |
| Copy Score ordering | band first, window PnL only as tie-break |
| Default band filter | `strong` ∪ `marginal` |
| Class order | strong → marginal → uneconomic → loss_making → not_measurable → unproven → none |
| Score display | `+6` / `−4` / `0` standalone; `11%` / `−4%` in the board column |
| Score move suppressed below | 5 points |
| Cohort baseline ("typical trader") | −2.05% |
| ROI | `pnl / volume * 100`, **null** when volume is 0 |
| Sparkline / area-chart geometry | reproduced point for point |
| Angle board weighting | `1e12 * structureRank - topPnl`, round-robin across group and cut |
| Cohort percentile marks | `[0, 10, 25, 50, 75, 90, 95, 99, 100]` |

**INFERRED** — recovered by fitting the published dataset, so stated as an
estimate wherever it reaches the screen:

| Rule | Value | How |
|---|---|---|
| Class cut-points | `strong ≥ +10`, `marginal ≥ 0` | min/max of `copyNet` per class across all 3,766 rows — the boundaries are clean |
| Spread cost | 1.48% of entry | one published wallet's cost decomposition |
| Fee cost | 3.14% of entry | same |
| Shrinkage prior | `shrunk = gross · n/(n+183)` | solved from one published wallet (81.37% gross, 6 resolved → 2.58% shrunk); a single data point, so treat the constant as approximate |
| "Not comparable" threshold | >50% of realised value taken by selling | the principled reading of the published wording; the real threshold is not published |

Verification: with the same snapshot, the default board reproduces the live
site's top ten row for row — same order, same Copy Score, same ROI, PnL, open
value, volume, and the same `▼ n pts` moves.

---

## Two facts about Polymarket's public API worth keeping

Both were found the hard way and are encoded in `lib/polymarket.mjs`:

1. **`/positions` returns only currently-held positions.** `closed=true`
   returns the *identical rows*. Once a market resolves and the position is
   redeemed, its size goes to zero and it leaves that endpoint entirely — so it
   cannot be used to find finished trades. Finished trades are reconstructed
   from `/activity` instead.
2. **`/activity` refuses any offset past 5000**
   (`max historical activity offset of 5000 exceeded`), so **5,500 rows is the
   most public history any wallet will yield.** A busy wallet's totals are
   therefore totals *over that window*, not lifetime ones. When the tape is
   truncated, the wallet page says so at the top rather than presenting a
   partial total as a complete one.

`/trades` also carries no `usdcSize` — only `/activity` has a USDC amount on
every row, and only `/activity` shows redemptions, rewards and rebates. It is
the primary source.

## How a wallet is measured

The unit is a **market episode**: everything one wallet did in one
`conditionId`. An episode is *finished* when nothing is still held in it, and a
finished episode is what "resolved trade" means everywhere in the app.

```
pnl    = sells + redemptions − buys
stake  = buys
gross  = Σ pnl / Σ stake     (finished, priceable episodes only)
```

Episodes whose inventory arrived through a `SPLIT`, `MERGE` or `CONVERSION` are
excluded from that return: they have no entry price, so including them would
overstate what a copier buying at market could have earned. Rewards and rebates
are counted separately and surfaced — they are income the wallet earns that a
copier does not, and on some wallets they are most of the profit.

Deliberately **not** computed: maker/taker split, Sharpe/Sortino, net deposited
and true ROI. They need order-level chain logs the public API does not expose.
They render as unavailable rather than being estimated from fills.

---

## PolyTrade integration

Built to be lifted into `polytrade/frontend/src/screener` rather than ported.

**Design.** `public/tokens.css` is a copy of `brutalism.css`'s token layer —
same paper, same three greens, same three type roles. Keep them identical; if
`brutalism.css` moves, move this, do not fork the palette. The shell reuses
polytrade's own class names (`.screener-shell`, `.screener-sidebar`,
`.screener-table`, `.chip`, `.control-label`, `.coverage`, …). Additions are
prefixed so they are easy to find: `.band-*`, `.cchip`, `.angle-*`,
`.index-*`, `.event-*`, `.trend-*`.

**Model split.** `public/lib/screenerModel.js` is pure — no DOM, no browser
globals — matching the existing `screenerModel.js` convention, so every rule is
testable without a browser. `public/board.js` only turns rows into DOM.

**The API contract already matches.** `lib/publicScreener.mjs` serves
`/api/public/screener/{wallets,wallets/:address,provenance}` with the same
field allowlist, the same period and sort validation, and the same per-client
rate limit as `backend/api/routes_public_screener.py`. Like the FastAPI router,
it publishes **no composite copyability score** — that field is null on this
surface by design.

**To switch data source**, set two globals before the module loads:

```html
<script>
  window.__SCREENER_SOURCE__ = 'polytrade';   // 'snapshot' (default) | 'polytrade'
  window.__API_BASE__ = '/api';               // or https://polytradebot.live/api
</script>
```

`loadUniverse()` in `public/lib/dataSource.js` then calls
`/public/screener/wallets` and maps each row through `fromPolytradeWallet()`.
Anything PolyTrade's cache does not know stays null and the board falls back to
ordering by money — a missing metric is never rendered as a zero.

**Hand-off.** The Copy action opens the PolyTrade bot.
`SUPPORTS_WALLET_DEEP_LINK` is `false`, because nothing in the polytrade
repository reads a `start` payload yet — emitting `?start=wallet_<address>`
would produce a link that silently drops the wallet. The button title tells the
reader to bring the address with them, and there is a Copy address button next
to it. Flip the flag when the bot learns to resolve one; the link is already
the right shape. A test pins this so it cannot drift by accident.

### What to do to actually merge it

1. Move `public/lib/screenerModel.js` additions into the existing model, or keep
   it beside as `copyBoardModel.js`; the two do not overlap. The filter half
   already uses polytrade's own vocabulary (`pnlMin`, `volumeMin`, `winrateMin`,
   blank-stays-blank via `finiteFilter`), and the sidebar's slider is a direct
   port of `RangeFilter.jsx` including its off-end contract — so those two
   pieces should collapse into the existing ones rather than sit beside them.
2. Port `public/board.js` to JSX — the render is already a pure function of
   `(rows, state)`, so it is a mechanical translation.
3. Fold `screener.css`'s prefixed additions into `styles/screener.css` and drop
   `tokens.css` in favour of the real `brutalism.css`. `public/lib/chart.js` and
   `public/lib/savedUi.js` are framework-free and return plain nodes, so they
   drop in behind a thin `useEffect` wrapper without being rewritten. Decide
   deliberately whether `glass.css` comes with them — it is a considered
   exception to the token layer's no-glass rule, not an oversight, and the call
   belongs to whoever owns the design system.
4. Decide where Copy Score comes from. It needs a full-chain indexer; until
   PolyTrade has one, either keep reading the cached cohort or leave the column
   out. Publishing a score computed from a truncated 5,500-row tape would be the
   one dishonest move available here.

---

## Layout

```
server.mjs                  static + JSON, no framework
scripts/ingest.mjs          refresh the cohort snapshot
lib/polymarket.mjs          public read APIs, with their two hard limits encoded
lib/metrics.mjs             episode reconstruction and every wallet metric
lib/copyscore.mjs           the taxonomy (verbatim) and local recomputation
lib/publicScreener.mjs      PolyTrade-shaped /api/public/screener/*
public/lib/screenerModel.js pure board rules — no DOM
public/lib/dataSource.js    the source adapter: snapshot ↔ PolyTrade API
public/lib/render.js        chips, sparklines, cells
public/lib/chart.js         interactive series chart and trading-clock heat-map
public/lib/saved.js         saved-wallet store — no DOM, injectable storage
public/lib/savedUi.js       the save control, nav pill and glass drawer
public/board.js             screener render
public/wallet.js            wallet render
public/glass.css            the only glass in the app — scoped to saved chrome
tests/                      43 tests over the model, filters, URL state, saved
                            wallets, episodes, score and the public endpoint
```

## A note on the glass

`brutalism.css` deliberately dropped the frosted-panel skin — over the reading
surfaces it read as generic SaaS next to the documentation. So the liquid-glass
treatment here is scoped to the saved-wallets chrome only: the nav pill, the
drawer, and the save control. Tables, panels and prose keep the hairline
language. `public/glass.css` is self-contained — deleting that one file removes
the effect and touches nothing else. Text never sits on the thinnest part of the
glass, and browsers without `backdrop-filter` get an opaque tint rather than
unreadable text over whatever is behind it.

Not affiliated with Polymarket. Nothing here is financial advice, no order is
placed from this surface, and past wallet activity does not predict future
results.
