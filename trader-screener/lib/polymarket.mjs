/* Thin client over Polymarket's public read APIs — the same unauthenticated
 * endpoints the Polymarket web app uses. No key, no session, read only.
 *
 * Two things this file exists to encode, both learned the hard way:
 *
 *   /positions returns only CURRENTLY HELD positions. The `closed=true` flag
 *   returns the identical rows, so it cannot be used to find finished trades —
 *   once a market resolves and the position is redeemed its size goes to zero
 *   and it leaves that endpoint entirely. Finished trades are reconstructed
 *   from /activity instead (see lib/metrics.mjs).
 *
 *   /trades rows carry `size` and `price` but no `usdcSize`, while /activity
 *   rows carry all three. /activity is therefore the primary fill source; it
 *   also carries the REDEEM, REWARD and REBATE rows the return math needs.
 */
import { createRateGate } from './serviceGuards.mjs';

const DATA = 'https://data-api.polymarket.com';
const LB = 'https://lb-api.polymarket.com';
const RPC = process.env.POLYGON_RPC || 'https://polygon-bor-rpc.publicnode.com';
const USDC_BRIDGED = '0x2791bca1f2de4661ed88a30c99a7a9449aa84174';
const USDC_NATIVE = '0x3c499c542cef5e3811e1192ce70d8cc03d5c3359';

/** The API caps a page at 500 rows regardless of what `limit` asks for. */
const PAGE = 500;
/** And it refuses any /activity offset past 5000 outright:
 *    {"error":"max historical activity offset of 5000 exceeded"}
 *  So 5500 rows is the most public history any wallet can yield. A busy
 *  wallet's tape is therefore its most recent 5500 rows, not all of it, and
 *  every caller has to say so rather than presenting a partial total as a
 *  lifetime one. */
export const ACTIVITY_CEILING = 5500;
const HEADERS = { accept: 'application/json', 'user-agent': 'polytrade-screener/1.0' };

/* One budget for everything this service asks Polymarket. Polymarket limits
 * requests per IP, and the copy engine on the same server needs most of that
 * budget for the trades it copies, so the screener as a whole sends at most
 * SCREENER_POLYMARKET_RPS requests a second (default 5, in bursts of up to
 * 10): a third of the 150 per 10 s Polymarket publishes for /positions, its
 * tightest data endpoint. Boards and wallet pages wait here in arrival order;
 * last-trade lookups take what they leave. The Polygon RPC is a separate
 * service and does not count. */
export const polymarketGate = createRateGate({
  perSecond: Number(process.env.SCREENER_POLYMARKET_RPS) || 5,
  burst: 10,
});

/** fetch for Polymarket's hosts, through the shared gate. `background: true`
 *  marks work no visitor is waiting on; it uses only idle capacity. */
export async function polymarketFetch(url, { background = false, ...init } = {}) {
  await polymarketGate.take(init.signal, { background });
  return fetch(url, init);
}

/* Per-request deadline, composed rather than wired by hand.
 *
 * The previous version attached its own 'abort' listener to the PARENT signal
 * for every nested request. One board request fans out well past ten
 * concurrent upstream calls -- ranks() alone issues eight (four windows x two
 * rank types), plus activity, positions, portfolioValue and two balanceOf
 * calls -- so the parent crossed Node's ten-listener threshold and emitted
 * MaxListenersExceededWarning for days in production. Cleanup was correct;
 * the peak was the problem, and raising the cap would only have hidden it.
 *
 * AbortSignal.any() composes without registering a public listener on the
 * parent, so the fan-out is no longer bounded by a warning threshold.
 *
 * The timeout stays a cancellable AbortController rather than
 * AbortSignal.timeout(): a timer that cannot be cleared early would stay
 * pending for its full duration on every completed request, which at this
 * request rate is a slow accumulation for no benefit. cleanup() clears it.
 */
export function deadlineSignal(timeout, parent) {
  const timer = new AbortController();
  const handle = setTimeout(
    () => timer.abort(new Error('upstream timeout')), timeout);
  const signal = parent
    ? AbortSignal.any([parent, timer.signal])
    : timer.signal;
  return {
    signal,
    cleanup() { clearTimeout(handle); },
  };
}

/* Polymarket answers a burst with 429 and a bad moment with 5xx. One such
 * answer used to end a whole paged read, and with it the wallet's profile, so
 * those two statuses get two more tries, one and then two seconds apart. */
const RETRY_PAUSES_MS = [1000, 2000];

async function j(url, { timeout = 25000, signal } = {}) {
  for (let attempt = 0; ; attempt += 1) {
    const deadline = deadlineSignal(timeout, signal);
    let status;
    try {
      const res = await polymarketFetch(url, { headers: HEADERS, signal: deadline.signal });
      if (res.ok) return await res.json();
      status = res.status;
    } finally {
      deadline.cleanup();
    }
    const retryable = status === 429 || status >= 500;
    if (!retryable || attempt >= RETRY_PAUSES_MS.length || signal?.aborted) throw new Error(`${status} ${url}`);
    await new Promise((resolve) => { setTimeout(resolve, RETRY_PAUSES_MS[attempt]); });
  }
}

/** Walk offsets until a short page. Stops on the first failure rather than
 *  silently returning a truncated tape as if it were complete. */
async function paged(path, params, { max = 20000, signal } = {}) {
  const out = [];
  let complete = true;
  for (let offset = 0; out.length < max; offset += PAGE) {
    const qs = new URLSearchParams({ ...params, limit: String(PAGE), offset: String(offset) });
    let page;
    try {
      page = await j(`${DATA}${path}?${qs}`, { signal });
    } catch {
      complete = false;
      break;
    }
    if (!Array.isArray(page)) { complete = false; break; }
    out.push(...page);
    if (page.length < PAGE) break;
  }
  const rows = out.slice(0, max);
  // Complete means "this is the whole tape": every page fetched cleanly AND we
  // did not stop because we hit the cap.
  rows.complete = complete && out.length < max;
  return rows;
}

/** Full account tape: fills plus redemptions, rewards and rebates.
 *  Returns at most ACTIVITY_CEILING rows; `rows.complete` is false when the
 *  wallet has more history than the endpoint will serve. */
export const activity = (user, { signal } = {}) =>
  paged('/activity', { user }, { max: ACTIVITY_CEILING, signal });

/** Below this many rows a wallet's open positions are read in full. */
const POSITIONS_READ_IN_FULL = 3000;
/** The most open positions a profile reads. */
export const POSITIONS_CEILING = 10_000;
const POSITIONS_BATCH = 4;

/** Currently held positions only — see the note at the top of this file.
 *
 *  The feed is sorted by current value. Some wallets hold thousands of
 *  positions, nearly all worth $0: on 2026-10-21, 32 of the 181 wallets on the
 *  7D board held more than 3,000, and the old cap at 3,000 failed every one of
 *  their profiles. Up to 3,000 rows the list is still read in full. Past that,
 *  reading stops at the first page that ends on a $0 position, since every
 *  later one is worth $0 too; `rows.worthlessSkipped` records it, and the
 *  profile counts those positions as lost bets. Pages after the first go out
 *  four at a time. `rows.complete` is false when a read failed, and
 *  `rows.overCeiling` holds the ceiling when the list still had value there:
 *  past about 10,000 the feed repeats its last row for every offset. */
export async function positions(user, { signal } = {}) {
  const page = async (offset) => {
    const qs = new URLSearchParams({
      user, sortBy: 'CURRENT', sortDirection: 'DESC', limit: String(PAGE), offset: String(offset),
    });
    const rows = await j(`${DATA}/positions?${qs}`, { signal });
    if (!Array.isArray(rows)) throw new Error('positions page is not a list');
    return rows;
  };
  const out = [];
  let complete = true;
  let worthlessSkipped = false;
  let overCeiling = null;
  try {
    const first = await page(0);
    out.push(...first);
    let done = first.length < PAGE;
    let offset = PAGE;
    while (!done) {
      if (offset >= POSITIONS_CEILING) { complete = false; overCeiling = POSITIONS_CEILING; break; }
      const offsets = [];
      for (let o = offset; o < POSITIONS_CEILING && offsets.length < POSITIONS_BATCH; o += PAGE) offsets.push(o);
      offset += offsets.length * PAGE;
      for (const rows of await Promise.all(offsets.map(page))) {
        out.push(...rows);
        if (rows.length < PAGE) { done = true; break; }
        if (out.length >= POSITIONS_READ_IN_FULL && Number(rows.at(-1)?.currentValue) <= 0) {
          worthlessSkipped = true;
          done = true;
          break;
        }
      }
    }
  } catch {
    complete = false;
  }
  out.complete = complete;
  out.worthlessSkipped = worthlessSkipped;
  out.overCeiling = overCeiling;
  return out;
}

export async function portfolioValue(user, { signal } = {}) {
  try {
    const r = await j(`${DATA}/value?user=${user}`, { signal });
    return Array.isArray(r) && r[0] ? r[0].value : null;
  } catch { return null; }
}

/** Polymarket's own leaderboard rank, per window, plus the display identity. */
export async function ranks(address, { signal } = {}) {
  const out = {};
  await Promise.all(['day', 'week', 'month', 'all'].flatMap((w) =>
    ['pnl', 'vol'].map(async (rankType) => {
      try {
        const r = await j(`${LB}/rank?address=${address}&window=${w}&rankType=${rankType}`, { signal });
        const row = Array.isArray(r) ? r[0] : null;
        out[w] ??= {};
        out[w][rankType === 'pnl' ? 'pnl' : 'volume'] = row ? { amount: row.amount, rank: row.rank } : null;
        if (row?.name) out.name = row.name;
        if (row?.profileImage) out.image = row.profileImage;
        if (row?.bio) out.bio = row.bio;
      } catch { /* this window has no row for the wallet */ }
    })));
  return out;
}

async function balanceOf(token, wallet, { signal } = {}) {
  const data = `0x70a08231${'0'.repeat(24)}${wallet.slice(2).toLowerCase()}`;
  const deadline = deadlineSignal(12000, signal);
  try {
    const res = await fetch(RPC, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'eth_call', params: [{ to: token, data }, 'latest'] }),
      signal: deadline.signal,
    });
    const out = await res.json();
    if (!out?.result || out.error) throw new Error(out?.error?.message || 'rpc');
    return Number(BigInt(out.result)) / 1e6;
  } finally {
    deadline.cleanup();
  }
}

/** Spendable USDC on Polygon. Unavailable is reported as null, not as zero. */
export async function cashBalance(wallet, { signal } = {}) {
  try {
    const [bridged, native] = await Promise.all([
      balanceOf(USDC_BRIDGED, wallet, { signal }),
      balanceOf(USDC_NATIVE, wallet, { signal }),
    ]);
    return { usd: bridged + native, asOf: new Date().toISOString(), source: 'polygon rpc' };
  } catch {
    return { usd: null, asOf: null, source: 'unavailable' };
  }
}
