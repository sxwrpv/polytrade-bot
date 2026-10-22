/* Cash available on the wallet page is a live read of the wallet's balances on
 * Polygon. Polymarket's collateral is pUSD: deposits mint it, and on 2026-10-22
 * 22 top 7D/30D PnL wallets held their cash almost entirely in it, so the two
 * USDCs alone read about $0 for them. These tests pin three properties: pUSD
 * is counted, a failed read of any token gives usd: null, and the reads go
 * straight to the Polygon RPC, outside the Polymarket rate gate.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

// The module reads the RPC URL when it loads, so it is set before the import.
process.env.POLYGON_RPC = 'https://polygon-rpc.test/';
const { cashBalance, polymarketGate } = await import('../lib/polymarket.mjs');

// pUSD is the SDK's collateral_token (PUSD in backend/core/withdraw.py).
const PUSD = '0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb';
const USDC_E = '0x2791bca1f2de4661ed88a30c99a7a9449aa84174';
const USDC = '0x3c499c542cef5e3811e1192ce70d8cc03d5c3359';
const wallet = '0x' + 'ab'.repeat(20);

/** A Polygon JSON-RPC that answers balanceOf(wallet) from `balances` (token ->
 *  dollars) and returns a JSON-RPC error for any other call. */
function chain(balances) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    const { method, params: [{ to, data }] } = JSON.parse(init.body);
    const token = to.toLowerCase();
    calls.push({ url, token });
    const usd = balances[token];
    const answered = method === 'eth_call' && usd != null
      && data === `0x70a08231${'0'.repeat(24)}${wallet.slice(2)}`;
    const body = answered
      ? { jsonrpc: '2.0', id: 1, result: '0x' + BigInt(Math.round(usd * 1e6)).toString(16).padStart(64, '0') }
      : { jsonrpc: '2.0', id: 1, error: { code: -32000, message: 'execution reverted' } };
    return { ok: true, status: 200, json: async () => body };
  };
  return { calls, fetchImpl };
}

async function withFetch(fetchImpl, work) {
  const real = globalThis.fetch;
  globalThis.fetch = fetchImpl;
  try { return await work(); } finally { globalThis.fetch = real; }
}

test('pUSD is counted with bridged and native USDC', async () => {
  const rpc = chain({ [PUSD]: 1500.25, [USDC_E]: 20.5, [USDC]: 4.25 });
  const cash = await withFetch(rpc.fetchImpl, () => cashBalance(wallet));
  assert.equal(cash.usd, 1525);
  assert.equal(cash.source, 'polygon rpc');
  assert.match(cash.asOf, /^\d{4}-\d{2}-\d{2}T/);
  assert.deepEqual(rpc.calls.map((c) => c.token).sort(), [PUSD, USDC_E, USDC].sort());
  assert.ok(rpc.calls.every((c) => c.url === 'https://polygon-rpc.test/'), 'a read went to another host');
});

test('a failed read of any token gives usd: null', async () => {
  const unavailable = { usd: null, asOf: null, source: 'unavailable' };
  // The pUSD read fails while both USDC reads succeed: their sum alone would show about $0.
  const pusdFails = chain({ [USDC_E]: 20.5, [USDC]: 4.25 }).fetchImpl;
  const throttled = async () => ({
    ok: false, status: 429, json: async () => { throw new SyntaxError('Unexpected token <'); },
  });
  const offline = async () => { throw new TypeError('fetch failed'); };
  for (const fetchImpl of [pusdFails, throttled, offline]) {
    assert.deepEqual(await withFetch(fetchImpl, () => cashBalance(wallet)), unavailable);
  }
});

test('the reads go straight to the Polygon RPC, outside the Polymarket rate gate', async () => {
  // Spend the gate down until a request has to queue: anything sent through it now waits.
  const held = [];
  while (polymarketGate.queued === 0) held.push(polymarketGate.take());
  const queued = polymarketGate.queued;
  const rpc = chain({ [PUSD]: 7, [USDC_E]: 0, [USDC]: 0 });
  await withFetch(rpc.fetchImpl, async () => {
    const pending = cashBalance(wallet);
    assert.equal(rpc.calls.length, 3, 'a balance read waited before going out');
    assert.equal(polymarketGate.queued, queued, 'a balance read joined the Polymarket queue');
    assert.equal((await pending).usd, 7);
  });
  await Promise.all(held);
});
