#!/usr/bin/env node
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { join, dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as pm from './lib/polymarket.mjs';
import { buildProfile } from './lib/metrics.mjs';
import * as screener from './lib/publicScreener.mjs';
import { fetchLiveUniverse } from './lib/liveLeaderboard.mjs';
import {
  canonicalClientIdentity, ConcurrencyGate, createCoalescer, createRateLimiter, TtlLruCache,
} from './lib/serviceGuards.mjs';

const ROOT = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 4310);

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.ico': 'image/x-icon',
};

// --- cached snapshot -------------------------------------------------------
/* Stamped at build time (Dockerfile ARG GIT_REVISION). "unknown" means this
 * image was not built through scripts/deploy.sh, which is itself a finding. */
const BUILD_REVISION = process.env.GIT_REVISION || 'unknown';
const BUILD_TIME = process.env.BUILD_TIME || '';
/* "live" means board rows are fetched from Polymarket on request; "snapshot"
 * means only the committed dataset is being served. A deploy that silently
 * fell back to snapshot looks identical from the outside otherwise. */
const DATA_MODE = process.env.SCREENER_DATA_MODE || 'live';

let dataset = null, smi = null;
async function loadSnapshot() {
  try {
    dataset = JSON.parse(await readFile(join(ROOT, 'data/dataset.json'), 'utf8'));
    smi = JSON.parse(await readFile(join(ROOT, "data/smi.json"), "utf8"));
  } catch (e) {
    console.error('\n  No snapshot found. Run:  node scripts/ingest.mjs\n');
    process.exit(1);
  }
}

// --- live trader profile, with bounded cache and upstream work -------------
const profileCache = new TtlLruCache({ max: 16, ttlMs: 5 * 60 * 1000 });
const profileInflight = createCoalescer();
const profileGate = new ConcurrencyGate(2);
const LIVE_PROFILE_DEADLINE_MS = 30_000;
const liveLeaderboardCache = new TtlLruCache({ max: 3, ttlMs: 60 * 1000 });
const liveLeaderboardInflight = createCoalescer();

async function liveLeaderboard(period) {
  const hit = liveLeaderboardCache.get(period);
  if (hit !== undefined) return hit;
  return liveLeaderboardInflight.run(period, async () => {
    const cached = liveLeaderboardCache.get(period);
    if (cached !== undefined) return cached;
    const deadline = new AbortController();
    const timer = setTimeout(() => deadline.abort(new Error('live leaderboard deadline exceeded')), 15_000);
    try {
      const rows = await fetchLiveUniverse({ period, snapshot: dataset, signal: deadline.signal });
      liveLeaderboardCache.set(period, rows);
      return rows;
    } finally {
      clearTimeout(timer);
    }
  });
}

async function traderProfile(wallet) {
  const hit = profileCache.get(wallet);
  if (hit !== undefined) return hit;

  // Concurrent requests for the same wallet share one upstream fan-out. At
  // most two distinct live profiles run at once; excess work fails fast.
  return profileInflight.run(wallet, () => profileGate.run(async () => {
    const cached = profileCache.get(wallet);
    if (cached !== undefined) return cached;

    const deadline = new AbortController();
    const timer = setTimeout(() => deadline.abort(new Error('live profile deadline exceeded')), LIVE_PROFILE_DEADLINE_MS);
    try {
      // /activity is the primary source: it is the only endpoint that carries a
      // USDC amount on every row, and the only one that shows redemptions.
      const options = { signal: deadline.signal };
      const [tape, open, rankRows, value, cash] = await Promise.all([
        pm.activity(wallet, options),
        pm.positions(wallet, options),
        pm.ranks(wallet, options).catch(() => ({})),
        pm.portfolioValue(wallet, options),
        pm.cashBalance(wallet, options),
      ]);

      // buildProfile fails closed if the currently-held positions feed is
      // incomplete. Without that feed, resolved PnL and Copy Score are unsafe.
      const data = buildProfile({ wallet, tape, open, rankRows, value, cash });

      const row = dataset.traders.find((t) => t.w.toLowerCase() === wallet);
      data.board = row
        ? { ...row, spark: dataset.spark[row.w] || null, copyDelta: dataset.copyDelta[row.w] || null,
            wow: dataset.wow[row.w] || null }
        : null;
      data.fetchedAt = new Date().toISOString();

      profileCache.set(wallet, data);
      return data;
    } finally {
      clearTimeout(timer);
    }
  }));
}

function searchLocal(q) {
  const t = q.trim().toLowerCase();
  if (!t) return [];
  const hits = [];
  for (const tr of dataset.traders) {
    if (tr.w.toLowerCase().includes(t) || (tr.name || '').toLowerCase().includes(t)) {
      hits.push({ wallet: tr.w, name: tr.name, img: tr.img, followers: tr.followers,
                  pnl: tr.pnl, copyClass: tr.copyClass, copyNet: tr.copyNet, ranked: true });
      if (hits.length >= 8) break;
    }
  }
  return hits;
}

const json = (res, code, body) => {
  const s = JSON.stringify(body);
  res.writeHead(code, { 'content-type': MIME['.json'], 'content-length': Buffer.byteLength(s) });
  res.end(s);
};

const apiLimiter = createRateLimiter({ limit: 60 });
const datasetLimiter = createRateLimiter({ limit: 8 });
const profileLimiter = createRateLimiter({ limit: 6 });
const apiError = (res, error, fallback = 500) => {
  const code = error.status || fallback;
  if (error.retryAfter) res.setHeader('Retry-After', String(error.retryAfter));
  return json(res, code, { detail: String(error.message || error) });
};

const PUBLIC_ROOT = resolve(ROOT, 'public');
async function serveStatic(res, rel) {
  const requested = String(rel || '').replace(/^[/\\]+/, '');
  const path = resolve(PUBLIC_ROOT, requested);
  if (path !== PUBLIC_ROOT && !path.startsWith(`${PUBLIC_ROOT}${sep}`)) return false;
  try {
    const s = await stat(path);
    if (!s.isFile()) throw new Error('not a file');
    const body = await readFile(path);
    res.writeHead(200, { 'content-type': MIME[extname(path)] || 'application/octet-stream', 'cache-control': 'no-cache' });
    res.end(body);
    return true;
  } catch { return false; }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const requestPath = url.pathname;
  // Production Caddy strips this prefix. Accept it directly too so the exact
  // production URLs can be exercised without a local reverse proxy.
  const p = requestPath === '/screener' || requestPath === '/screener/'
    ? '/'
    : requestPath.startsWith('/screener/') ? requestPath.slice('/screener'.length) : requestPath;
  try {
    if (p === '/api/health') {
      /* Build revision and data mode are here so a post-deploy check can
       * prove WHICH build answered, not merely that something did. The
       * Screener commit once reached GitHub without reaching production and
       * nothing in either service could say so. */
      return json(res, 200, {
        status: 'ok',
        revision: BUILD_REVISION,
        builtAt: BUILD_TIME || null,
        dataMode: DATA_MODE,
        generatedAt: dataset.meta.generatedAt,
        traders: dataset.traders.length,
      });
    }

    if (p.startsWith('/api/')) {
      const client = canonicalClientIdentity(req);
      try {
        apiLimiter.enforce(client);
        if (p === '/api/dataset') datasetLimiter.enforce(client);
        if (p.startsWith('/api/trader/')) profileLimiter.enforce(client);
      } catch (error) {
        return apiError(res, error, 429);
      }
    }

    if (p === '/api/live/leaderboard') {
      const period = url.searchParams.get('period') || 'd7';
      if (!['d7', 'd30', 'all'].includes(period)) return json(res, 422, { detail: 'period must be d7, d30, or all' });
      try { return json(res, 200, await liveLeaderboard(period)); }
      catch (error) { return apiError(res, error, 502); }
    }

    // PolyTrade-shaped read surface: the truthful subset this imported
    // snapshot can support. Unknown fields remain null rather than fabricated.
    if (p.startsWith('/api/public/screener/')) {
      try {
        if (p === '/api/public/screener/wallets') {
          return json(res, 200, screener.queryWallets(dataset.traders, url.searchParams));
        }
        if (p === '/api/public/screener/provenance') return json(res, 200, screener.PROVENANCE);
        const w = /^\/api\/public\/screener\/wallets\/(0x[a-fA-F0-9]{40})$/.exec(p);
        if (w) {
          const wallet = w[1].toLowerCase();
          const row = dataset.traders.find((t) => t.w.toLowerCase() === wallet);
          if (!row) return json(res, 404, { detail: 'wallet not in the screener cache' });
          const period = url.searchParams.get('period') || '30d';
          if (!(period in screener.PERIODS)) return json(res, 422, { detail: 'period must be one of 7d, 30d' });
          return json(res, 200, screener.project(row, period));
        }
      } catch (error) { return apiError(res, error); }
    }

    if (p === '/api/dataset') return json(res, 200, dataset);
    if (p === '/api/smi') return json(res, 200, smi);
    if (p === '/api/search') return json(res, 200, { hits: searchLocal(url.searchParams.get('q') || '') });

    const m = /^\/api\/trader\/(0x[a-fA-F0-9]{40})$/.exec(p);
    if (m) {
      const wallet = m[1].toLowerCase();
      try {
        return json(res, 200, await traderProfile(wallet));
      } catch (error) { return apiError(res, error, 502); }
    }

    if (p === '/' || p === '/index.html') {
      if (await serveStatic(res, 'index.html')) return;
    }
    if (/^\/trader\/0x[a-fA-F0-9]{40}\/?$/.test(p)) {
      if (await serveStatic(res, 'trader.html')) return;
    }
    if (await serveStatic(res, p)) return;

    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('404');
  } catch (error) {
    console.error('request failed', error);
    res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('Internal server error');
  }
});

await loadSnapshot();
server.listen(PORT, () => {
  console.log(`\n  Polycopy screener clone`);
  console.log(`  http://localhost:${PORT}`);
  console.log(`  snapshot ${dataset.meta.generatedAt}  ·  ${dataset.traders.length.toLocaleString()} traders`);
  console.log(`  trader pages pull live from data-api.polymarket.com\n`);
});
