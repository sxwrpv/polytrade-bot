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

export function deadlineSignal(timeout, parent) {
  const controller = new AbortController();
  const abortFromParent = () => controller.abort(parent.reason);
  if (parent?.aborted) abortFromParent();
  else parent?.addEventListener('abort', abortFromParent, { once: true });
  const timer = setTimeout(() => controller.abort(new Error('upstream timeout')), timeout);
  return {
    signal: controller.signal,
    cleanup() {
      clearTimeout(timer);
      parent?.removeEventListener('abort', abortFromParent);
    },
  };
}

async function j(url, { timeout = 25000, signal } = {}) {
  const deadline = deadlineSignal(timeout, signal);
  try {
    const res = await fetch(url, { headers: HEADERS, signal: deadline.signal });
    if (!res.ok) throw new Error(`${res.status} ${url}`);
    return await res.json();
  } finally {
    deadline.cleanup();
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

/** Currently held positions only — see the note at the top of this file. */
export const positions = (user, { signal } = {}) =>
  paged('/positions', { user, sortBy: 'CURRENT', sortDirection: 'DESC' }, { max: 3000, signal });

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
