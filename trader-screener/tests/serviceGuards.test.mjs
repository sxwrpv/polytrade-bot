import test from 'node:test';
import assert from 'node:assert/strict';
import {
  canonicalClientIdentity, ConcurrencyGate, createCoalescer, createRateLimiter, TtlLruCache,
} from '../lib/serviceGuards.mjs';
import { deadlineSignal } from '../lib/polymarket.mjs';


test('forwarded client identity is trusted only from the private proxy hop', () => {
  assert.equal(canonicalClientIdentity({
    socket: { remoteAddress: '172.20.0.4' },
    headers: { 'x-forwarded-for': '203.0.113.9, 172.20.0.4' },
  }), '203.0.113.9');
  assert.equal(canonicalClientIdentity({
    socket: { remoteAddress: '198.51.100.7' },
    headers: { 'x-forwarded-for': '203.0.113.9' },
  }), '198.51.100.7', 'a directly connected public peer cannot spoof forwarded identity');
});

test('TTL/LRU profile cache expires and evicts without growing unbounded', () => {
  let now = 0;
  const cache = new TtlLruCache({ max: 2, ttlMs: 10, now: () => now });
  cache.set('a', 1); cache.set('b', 2);
  assert.equal(cache.get('a'), 1, 'reading a makes it most recently used');
  cache.set('c', 3);
  assert.equal(cache.get('b'), undefined);
  assert.equal(cache.size, 2);
  now = 11;
  assert.equal(cache.get('a'), undefined, 'expired entries disappear on read');
});

test('same-key live profile requests coalesce onto one promise', async () => {
  const coalescer = createCoalescer();
  let runs = 0;
  let release;
  const work = () => { runs += 1; return new Promise((resolve) => { release = resolve; }); };
  const first = coalescer.run('wallet', work);
  const second = coalescer.run('wallet', work);
  assert.equal(first, second);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(runs, 1);
  release('ok');
  assert.equal(await second, 'ok');
  assert.equal(coalescer.size, 0);
});

test('live profile concurrency fails fast instead of queueing unbounded work', async () => {
  const gate = new ConcurrencyGate(1);
  let release;
  const first = gate.run(() => new Promise((resolve) => { release = resolve; }));
  await assert.rejects(() => gate.run(async () => 'second'), (error) => error.status === 429 && error.retryAfter === 2);
  release('first');
  assert.equal(await first, 'first');
});

test('endpoint limiter is per client, bounded, and reports retry timing', () => {
  let now = 0;
  const limiter = createRateLimiter({ limit: 2, windowMs: 1000, maxClients: 2, now: () => now });
  limiter.enforce('a'); limiter.enforce('a');
  assert.throws(() => limiter.enforce('a'), (error) => error.status === 429 && error.retryAfter === 1);
  assert.doesNotThrow(() => limiter.enforce('b'));
  now = 1000;
  assert.doesNotThrow(() => limiter.enforce('a'));
  limiter.enforce('c');
  assert.ok(limiter.size <= 2);
});

test('upstream deadlines inherit parent cancellation and clean up safely', () => {
  const parent = new AbortController();
  const deadline = deadlineSignal(60_000, parent.signal);
  assert.equal(deadline.signal.aborted, false);
  parent.abort(new Error('profile deadline'));
  assert.equal(deadline.signal.aborted, true);
  assert.match(String(deadline.signal.reason), /profile deadline/);
  assert.doesNotThrow(() => deadline.cleanup());
});
