/* Thin client over Polymarket's public read APIs — the same unauthenticated
 * endpoints the Polymarket web app uses. No key, no session, read only.
 *
 * Every read goes to Data API v2: v1 answers 410 since its retirement on
 * 2026-11-02, and so does lb-api.polymarket.com, which served wallet ranks
 * and now points to /v2/leaderboard. A v2 response wraps its payload in
 * `data`; a feed pages with an opaque `pagination.next_cursor` (an `offset`
 * is a 400); fields are snake_case. activityRow() and positionRow() below
 * rename each row's fields to the camelCase names lib/metrics.mjs and the
 * wallet page read, the names v1 served.
 *
 * Two things this file exists to encode, both learned the hard way:
 *
 *   /v2/positions at its default status (OPEN) lists what the wallet still
 *   holds, settled-but-unredeemed tokens included. Once a market resolves and
 *   the position is redeemed its size goes to zero and it leaves that list.
 *   Finished trades are reconstructed from /v2/activity (see lib/metrics.mjs).
 *
 *   /v2/trades rows carry `size` and `price` but no `usdc_size`, while
 *   /v2/activity rows carry all three. /v2/activity is therefore the primary
 *   fill source; it also carries the REDEEM, REWARD and REBATE rows the
 *   return math needs.
 */
import { createRateGate } from './serviceGuards.mjs';

const DATA = 'https://data-api.polymarket.com/v2';
const RPC = process.env.POLYGON_RPC || 'https://polygon-bor-rpc.publicnode.com';
const USDC_BRIDGED = '0x2791bca1f2de4661ed88a30c99a7a9449aa84174';
const USDC_NATIVE = '0x3c499c542cef5e3811e1192ce70d8cc03d5c3359';
/** pUSD, Polymarket's collateral: deposits mint it and trades settle in it. */
const PUSD = '0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb';

/** A v2 page holds at most 1,000 rows; a larger `limit` is a 400. The first
 *  page of a walk names `limit`, and the cursor carries it from there. */
const PAGE = 1000;
/** The most activity rows a profile reads. v1 refused any /activity offset
 *  past 5000, which made 5,500 rows the most history any wallet could yield.
 *  v2's cursor goes back further, and the profile keeps the same budget: a
 *  busy wallet's tape is its most recent 5,500 rows, and when `complete` is
 *  false every caller says its totals cover that window only. */
export const ACTIVITY_CEILING = 5500;
/** The row types lib/metrics.mjs reads: fills, redemptions, the inventory
 *  moves that make an episode unpriceable, and the rewards and rebates a
 *  copier does not earn. Naming them keeps v2's other types (migrations,
 *  range-event moves, referral rewards) out of the row budget; deposits,
 *  withdrawals and tips stay out by v2's own default as well. */
const TAPE_TYPES = 'TRADE,REDEEM,SPLIT,MERGE,CONVERSION,REWARD,YIELD,MAKER_REBATE,TAKER_REBATE';
const HEADERS = { accept: 'application/json', 'user-agent': 'polytrade-screener/1.0' };

/* One budget for everything this service asks Polymarket. Polymarket limits
 * requests per IP, and the copy engine on the same server needs most of that
 * budget for the trades it copies, so the screener as a whole sends at most
 * SCREENER_POLYMARKET_RPS requests a second (default 5, in bursts of up to
 * 10): a quarter of the 200 per 10 s Polymarket publishes for /v2/positions
 * and /v2/activity, its tightest data endpoints. Boards and wallet pages wait
 * here in arrival order; last-trade lookups take what they leave. The Polygon
 * RPC is a separate service and does not count. */
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
 * for every nested request. One board request fanned out well past ten
 * concurrent upstream calls -- ranks() alone issued eight at the time (four
 * windows x two rank types), plus activity, positions, portfolioValue and
 * three balanceOf calls -- so the parent crossed Node's ten-listener
 * threshold and emitted MaxListenersExceededWarning for days in production.
 * Cleanup was correct; the peak was the problem, and raising the cap would
 * only have hidden it.
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

/** One page of a v2 feed. The first names `limit`; every later one sends the
 *  previous page's `next_cursor` with the same anchor and filters, which v2
 *  wants restated on each page. */
function pageUrl(path, params, cursor) {
  const qs = new URLSearchParams(params);
  if (cursor) qs.set('cursor', cursor);
  else qs.set('limit', String(PAGE));
  return `${DATA}${path}?${qs}`;
}

/** Follow a feed's cursor until it ends or `max` rows are read. Stops on the
 *  first failure, and on an empty page that still names a cursor, and marks
 *  the rows incomplete then, so callers can tell a truncated tape from a
 *  whole one. */
async function readFeed(path, params, { max, signal }) {
  const rows = [];
  let complete = true;
  let cursor = null;
  do {
    let page;
    try {
      page = await j(pageUrl(path, params, cursor), { signal });
    } catch {
      complete = false;
      break;
    }
    if (!Array.isArray(page?.data)) { complete = false; break; }
    rows.push(...page.data);
    cursor = page.pagination?.next_cursor || null;
    if (!page.data.length) break;
  } while (cursor && rows.length < max);
  const out = rows.slice(0, max);
  // Complete means "this is the whole tape": every page fetched cleanly, the
  // cursor ran out, and nothing past `max` was cut off.
  out.complete = complete && !cursor && rows.length <= max;
  return out;
}

/** One /v2/activity row under the names lib/metrics.mjs and the wallet page read. */
const activityRow = (r) => ({
  proxyWallet: r.proxy_wallet,
  timestamp: r.timestamp,
  conditionId: r.condition_id,
  type: r.type,
  size: r.size,
  usdcSize: r.usdc_size,
  transactionHash: r.transaction_hash,
  price: r.price,
  asset: r.token_id,
  side: r.side,
  outcomeIndex: r.outcome_index,
  title: r.title,
  slug: r.slug,
  icon: r.icon,
  eventSlug: r.event_slug,
  outcome: r.outcome,
  name: r.name,
  pseudonym: r.pseudonym,
  bio: r.bio,
  profileImage: r.profile_image,
  profileImageOptimized: r.profile_image_optimized,
});

/** Full account tape: fills plus redemptions, rewards and rebates, newest
 *  first. `start=1` asks for the whole history; left out, v2 starts three
 *  years back.
 *
 *  v2 also lists Combo rows here (`is_combo`), keyed by a combo condition id.
 *  Combos have their own routes (/v2/activity/combos, /v2/positions/combos),
 *  and /v2/positions lists single-market holdings only, so a held combo would
 *  read as a finished, lost bet. The tape drops them.
 *
 *  Reads at most ACTIVITY_CEILING rows; `rows.complete` is false when the
 *  wallet has more history than that, or a page failed. */
export async function activity(user, { signal } = {}) {
  const rows = await readFeed('/activity', { user, type: TAPE_TYPES, start: '1' },
    { max: ACTIVITY_CEILING, signal });
  const tape = rows.filter((row) => !row.is_combo).map(activityRow);
  tape.complete = rows.complete;
  return tape;
}

/** Below this many rows a wallet's open positions are read in full. */
const POSITIONS_READ_IN_FULL = 3000;
/** The most open positions a profile reads. */
export const POSITIONS_CEILING = 10_000;

/** One /v2/positions row under the names lib/metrics.mjs and the wallet page
 *  read. `size` is the current holding and `cashPnl` the unrealized PnL. */
const positionRow = (r) => ({
  proxyWallet: r.proxy_wallet,
  asset: r.token_id,
  conditionId: r.condition_id,
  size: r.current_size,
  avgPrice: r.avg_price,
  initialValue: r.entry_cost_usdc,
  currentValue: r.current_value,
  cashPnl: r.unrealized_pnl,
  percentPnl: r.percent_pnl,
  totalBought: r.total_size,
  realizedPnl: r.realized_pnl,
  percentRealizedPnl: r.percent_realized_pnl,
  curPrice: r.current_price,
  redeemable: r.redeemable,
  mergeable: r.mergeable,
  title: r.title,
  slug: r.slug,
  icon: r.icon,
  eventId: r.event_id,
  eventSlug: r.event_slug,
  outcome: r.outcome,
  outcomeIndex: r.outcome_index,
  oppositeOutcome: r.opposite_outcome,
  oppositeAsset: r.opposite_token_id,
  endDate: r.end_date,
  negativeRisk: r.negative_risk,
  grossInitialValue: r.total_cost_usdc,
  entryFeesUsdc: r.entry_fees_usdc,
});

/** Currently held positions only — see the note at the top of this file.
 *
 *  Holdings under 1 share are left out: 1 share was v1's default size
 *  threshold, and v2's default floor of 0.1 would count dust left after a
 *  sell-out as a position still held.
 *
 *  The feed is sorted by current value. Some wallets hold thousands of
 *  positions, nearly all worth $0: on 2026-10-21, 32 of the 181 wallets on the
 *  7D board held more than 3,000, and the old cap at 3,000 failed every one of
 *  their profiles. Up to 3,000 rows the list is still read in full. Past that,
 *  reading stops at the first page that ends on a $0 position, since every
 *  later one is worth $0 too; `rows.worthlessSkipped` records it, and the
 *  profile counts those positions as lost bets. Each page needs the cursor of
 *  the one before it, so pages go out one after another, 1,000 rows each.
 *  `rows.complete` is false when a read failed, and `rows.overCeiling` holds
 *  POSITIONS_CEILING when the list still had value there. */
export async function positions(user, { signal } = {}) {
  const params = {
    user, sort_by: 'CURRENT_VALUE', sort_direction: 'DESC', filter_type: 'TOKENS', filter_amount: '1',
  };
  const out = [];
  let complete = true;
  let worthlessSkipped = false;
  let overCeiling = null;
  try {
    let cursor = null;
    do {
      const page = await j(pageUrl('/positions', params, cursor), { signal });
      if (!Array.isArray(page?.data)) throw new Error('positions page is not a list');
      const rows = page.data.map(positionRow);
      out.push(...rows);
      cursor = page.pagination?.next_cursor || null;
      if (cursor && !rows.length) throw new Error('positions page is empty mid-feed');
      if (cursor && out.length >= POSITIONS_READ_IN_FULL && Number(rows.at(-1).currentValue) <= 0) {
        worthlessSkipped = true;
        break;
      }
      if (cursor && out.length >= POSITIONS_CEILING) {
        complete = false;
        overCeiling = POSITIONS_CEILING;
        break;
      }
    } while (cursor);
  } catch {
    complete = false;
  }
  out.complete = complete;
  out.worthlessSkipped = worthlessSkipped;
  out.overCeiling = overCeiling;
  return out;
}

/** Holdings marked to market. v2 adds unresolved Combo positions at cost. */
export async function portfolioValue(user, { signal } = {}) {
  try {
    const r = await j(`${DATA}/value?user=${user}`, { signal });
    return r?.data?.value ?? null;
  } catch { return null; }
}

/** Polymarket's own leaderboard rank, per window, plus the display identity.
 *  One /v2/leaderboard?user= read per window carries both ranks; lb-api,
 *  which served them one at a time, is retired. A rank of 0 or null means
 *  unranked on that board, and stays null here. Volume counts shares. */
export async function ranks(address, { signal } = {}) {
  const out = {};
  const standing = (amount, rank) => ({ amount, rank: rank > 0 ? rank : null });
  await Promise.all(['day', 'week', 'month', 'all'].map(async (w) => {
    try {
      const r = await j(`${DATA}/leaderboard?${new URLSearchParams({ time_period: w, user: address })}`, { signal });
      const row = r?.data ?? null;
      out[w] = {
        pnl: row ? standing(row.pnl, row.rank_pnl) : null,
        volume: row ? standing(row.volume, row.rank_volume) : null,
      };
      if (row?.user_name) out.name = row.user_name;
      if (row?.profile_image) out.image = row.profile_image;
    } catch { /* this window has no row for the wallet */ }
  }));
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

/** Spendable cash on Polygon: pUSD plus bridged and native USDC. Leaders hold
 *  nearly all of theirs as pUSD (22 top 7D/30D PnL wallets on 2026-10-22), so
 *  the two USDCs alone read about $0 for them. A failed read of any of the
 *  three gives usd: null. */
export async function cashBalance(wallet, { signal } = {}) {
  try {
    const [pusd, bridged, native] = await Promise.all([
      balanceOf(PUSD, wallet, { signal }),
      balanceOf(USDC_BRIDGED, wallet, { signal }),
      balanceOf(USDC_NATIVE, wallet, { signal }),
    ]);
    return { usd: pusd + bridged + native, asOf: new Date().toISOString(), source: 'polygon rpc' };
  } catch {
    return { usd: null, asOf: null, source: 'unavailable' };
  }
}
