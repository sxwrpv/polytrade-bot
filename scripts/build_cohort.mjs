#!/usr/bin/env node
/* Build the screener's Copy Score overlay from a Polycopy cohort snapshot.
 *
 * WHAT THIS IS, AND WHY IT IS A SEPARATE ASSET
 *
 * PolyTrade's own screener API deliberately publishes no composite score: its
 * inputs are partial by construction and one number would hide that. That
 * promise is documented in backend/api/routes_public_screener.py and pinned by
 * tests/test_public_screener.py. Nothing here changes it.
 *
 * Copy Score is a THIRD-PARTY figure. It comes from polycopy.app's public
 * discover dataset, it needs a full-chain indexer PolyTrade does not run, and
 * it describes a cohort that is a subset of PolyTrade's cache. So it ships as
 * its own asset, with its own provenance and its own generation date, joined
 * onto the live board in the browser — never mixed into PolyTrade's API
 * response, and never presented as PolyTrade's own measurement.
 *
 * REGENERATING
 *
 *   node scripts/ingest.mjs                     # in the polycopy-clone repo
 *   node scripts/build_cohort.mjs <dataset.json>
 *
 * The output is committed, because the deploy path is `git pull` on the VPS
 * and production must not depend on a third-party endpoint being reachable at
 * build time. It is dated in `meta.generatedAt`; the board states that date and
 * warns once it is more than two days old.
 */
import { readFile, writeFile } from 'node:fs/promises'
import { gzipSync, brotliCompressSync } from 'node:zlib'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const OUT = join(ROOT, 'frontend', 'public', 'screener-cohort.json')

const source = process.argv[2]
  ? resolve(process.argv[2])
  : join(process.env.HOME || '', 'polycopy-clone', 'data', 'dataset.json')

/** Round, but keep an absent value absent — a null here is not a zero. */
const round = (value, places = 2) =>
  (value == null || !Number.isFinite(value) ? null : Number(value.toFixed(places)))

/* The projection is an explicit allowlist, the same discipline the public API
 * route uses: a field added upstream cannot start riding along by accident. */
function projectTrader(t) {
  return {
    w: String(t.w).toLowerCase(),
    name: t.name ?? null,
    followers: t.followers ?? null,
    pnl: round(t.pnl, 0),
    d7: round(t.d7, 0),
    d30: round(t.d30, 0),
    winRate: round(t.winRate, 4),
    vol: round(t.vol, 0),
    d7Vol: round(t.d7Vol, 0),
    d30Vol: round(t.d30Vol, 0),
    openVal: round(t.openVal, 0),
    copyClass: t.copyClass ?? null,
    copyNet: round(t.copyNet, 2),
    mm: t.mm ?? null,
    arb: t.arb ?? null,
    freq: t.freq ?? null,
    cats: Array.isArray(t.cats) ? t.cats : [],
    lastTradeDay: t.lastTradeDay ?? null,
    fills: t.fills ?? null,
    activeDays: t.activeDays ?? null,
    maker: round(t.maker, 3),
    hold: round(t.hold, 2),
    weeksUp: round(t.weeksUp, 3),
    vola: round(t.vola, 0),
    avgSize: round(t.avgSize, 0),
    maxDD: round(t.maxDD, 0),
    niche: t.niche ?? null,
  }
}

const raw = JSON.parse(await readFile(source, 'utf8'))
if (!raw?.meta?.generatedAt || !Array.isArray(raw.traders)) {
  throw new Error(`${source} does not look like a Polycopy discover dataset`)
}

const cohort = {
  meta: {
    generatedAt: raw.meta.generatedAt,
    windowAnchor: raw.meta.windowAnchor ?? null,
    boardsAsOf: raw.meta.boardsAsOf ?? null,
    // Stated on the surface wherever a Copy Score is shown. The reader is
    // entitled to know this number is not PolyTrade's own measurement.
    source: 'polycopy.app public discover dataset',
    scoreOwner: 'Polycopy',
  },
  traders: raw.traders.map(projectTrader),
  // Weekly realized-PnL points behind the Trend column. Integers: sub-dollar
  // precision on a 60px sparkline is bytes for nothing.
  spark: Object.fromEntries(
    Object.entries(raw.spark ?? {}).map(([w, series]) => [
      w.toLowerCase(),
      (series ?? []).map((n) => Math.round(Number(n) || 0)),
    ]),
  ),
  // Week-over-week pairs and score moves, for the Trending strip and the
  // "▼ n pts" markers.
  wow: raw.wow ?? {},
  wowAnchor: raw.wowAnchor ?? null,
  copyDelta: raw.copyDelta ?? {},
  // The 235 per-slice "who is good at this one thing" boards.
  angles: raw.angles ?? [],
  groups: raw.groups ?? [],
  structures: raw.structures ?? [],
}

const json = JSON.stringify(cohort)
await writeFile(OUT, json)

const mb = (n) => `${(n / 1e6).toFixed(2)} MB`
process.stdout.write(
  `wrote ${OUT}\n` +
  `  traders     ${cohort.traders.length.toLocaleString()}\n` +
  `  sparklines  ${Object.keys(cohort.spark).length.toLocaleString()}\n` +
  `  angles      ${cohort.angles.length.toLocaleString()}\n` +
  `  generated   ${cohort.meta.generatedAt}\n` +
  `  size        ${mb(Buffer.byteLength(json))} raw · ` +
  `${mb(gzipSync(json).length)} gzip · ${mb(brotliCompressSync(json).length)} brotli\n`,
)
