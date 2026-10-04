/* Fan-out deadlines must not trip Node's listener threshold.
 *
 * Production emitted MaxListenersExceededWarning ("11 abort listeners added to
 * [AbortSignal]") for days: one board request fans out past ten concurrent
 * upstream calls, and each nested request used to attach its own listener to
 * the shared parent signal. These tests pin the four properties that matter --
 * no warning, cancellation still reaches everything, timers are released, and
 * a timed-out request does not linger in the coalescer's in-flight map.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { deadlineSignal } from '../lib/polymarket.mjs';
import { createCoalescer } from '../lib/serviceGuards.mjs';

const FANOUT = 24;            // comfortably past Node's default of 10

/** Collect MaxListenersExceededWarning emitted while `fn` runs. */
async function warningsDuring(fn) {
  const seen = [];
  const onWarning = (w) => { if (w.name === 'MaxListenersExceededWarning') seen.push(w); };
  process.on('warning', onWarning);
  try {
    await fn();
    // process warnings are delivered on the next tick
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
  } finally {
    process.off('warning', onWarning);
  }
  return seen;
}

test('many concurrent deadlines on one parent emit no listener warning', async () => {
  const parent = new AbortController();
  const warnings = await warningsDuring(async () => {
    const deadlines = Array.from({ length: FANOUT },
      () => deadlineSignal(60_000, parent.signal));
    assert.equal(deadlines.length, FANOUT);
    for (const d of deadlines) d.cleanup();
  });
  assert.deepEqual(warnings.map((w) => w.message), [],
    'composing deadlines still warns about listener count');
  parent.abort();
});

test('parent cancellation reaches every composed request', () => {
  const parent = new AbortController();
  const deadlines = Array.from({ length: FANOUT },
    () => deadlineSignal(60_000, parent.signal));

  assert.ok(deadlines.every((d) => !d.signal.aborted), 'aborted before cancel');
  parent.abort(new Error('client went away'));

  assert.ok(deadlines.every((d) => d.signal.aborted),
    'a nested request survived parent cancellation');
  assert.equal(deadlines[0].signal.reason.message, 'client went away',
    'abort reason was not propagated from the parent');
  for (const d of deadlines) d.cleanup();
});

test('a parent that is already aborted aborts the child immediately', () => {
  const parent = new AbortController();
  parent.abort(new Error('too late'));
  const d = deadlineSignal(60_000, parent.signal);
  assert.ok(d.signal.aborted);
  assert.equal(d.signal.reason.message, 'too late');
  d.cleanup();
});

test('the deadline fires on its own when the parent never aborts', async () => {
  const parent = new AbortController();
  const d = deadlineSignal(5, parent.signal);
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(d.signal.aborted, 'timeout did not fire');
  assert.equal(d.signal.reason.message, 'upstream timeout');
  d.cleanup();
});

test('cleanup releases the timer so a finished request holds nothing', async () => {
  const parent = new AbortController();
  const d = deadlineSignal(20, parent.signal);
  d.cleanup();
  await new Promise((r) => setTimeout(r, 60));
  assert.equal(d.signal.aborted, false,
    'cleared timer still aborted — cleanup did not release it');
});

test('a deadline works with no parent at all', async () => {
  const d = deadlineSignal(5);
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(d.signal.aborted);
  d.cleanup();
});

test('timed-out work does not stay in the in-flight map', async () => {
  const coalescer = createCoalescer();
  const parent = new AbortController();

  const failing = coalescer.run('wallet-a', async () => {
    const d = deadlineSignal(5, parent.signal);
    try {
      await new Promise((resolve, reject) => {
        d.signal.addEventListener('abort', () => reject(d.signal.reason), { once: true });
      });
    } finally {
      d.cleanup();
    }
  });

  await assert.rejects(failing, /upstream timeout/);

  // The key property: the failed key is gone, so the next caller does a fresh
  // attempt instead of being handed the rejected promise forever.
  let ran = false;
  await coalescer.run('wallet-a', async () => { ran = true; return 'ok'; });
  assert.ok(ran, 'a timed-out key was never released from the in-flight map');
});

test('concurrent fan-out through the coalescer stays warning-free end to end', async () => {
  const parent = new AbortController();
  const coalescer = createCoalescer();
  const warnings = await warningsDuring(async () => {
    await Promise.all(Array.from({ length: FANOUT }, (_, i) =>
      coalescer.run(`key-${i}`, async () => {
        const d = deadlineSignal(60_000, parent.signal);
        try { return await Promise.resolve(i); } finally { d.cleanup(); }
      })));
  });
  assert.deepEqual(warnings.map((w) => w.message), []);
  parent.abort();
});
