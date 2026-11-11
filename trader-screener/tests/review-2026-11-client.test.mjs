/* Browser-side rules from the 2026-11 review. Run: node --test tests/ */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as M from '../public/lib/screenerModel.js';
import { createStore, saved } from '../public/lib/saved.js';
import { liveButtons, saveButton, savedCsv } from '../public/lib/savedUi.js';

const wallet = (digit) => '0x' + String(digit).repeat(40);

const trader = (over = {}) => ({
  w: wallet(1), name: 'w', cats: ['Sports'], lastTradeDay: '2026-08-20',
  pnl: 1000, d7: 100, d30: 500, vol: 200000, d7Vol: 50000, d30Vol: 100000,
  winRate: 0.5, openVal: 0, copyClass: 'strong', copyNet: 12, activeDays: 40, ...over,
});

function fakeStorage(seed = {}) {
  const d = { ...seed };
  return {
    d,
    getItem: (k) => (k in d ? d[k] : null),
    setItem: (k, v) => { d[k] = v; },
  };
}

/** One CSV line split into cells, with quoted cells unwrapped. */
function cells(line) {
  const out = [];
  const re = /"((?:[^"]|"")*)"|([^,]*)/y;
  for (let at = 0; at <= line.length; at = re.lastIndex + 1) {
    re.lastIndex = at;
    const m = re.exec(line);
    out.push(m[1] != null ? m[1].replace(/""/g, '"') : m[2]);
  }
  return out;
}

// --- CSV export -------------------------------------------------------------

const FORMULA_LEADS = { equals: '=', plus: '+', minus: '-', at: '@', tab: '\t', 'carriage return': '\r' };

for (const [label, lead] of Object.entries(FORMULA_LEADS)) {
  test(`a CSV cell that opens with ${label} is written as text`, () => {
    const name = `${lead}SUM(A1:A9)`;
    assert.deepEqual(cells(M.csvCell(name)), [`'${name}`]);

    const [head, line] = M.toCsv([trader({ name })], 'd7').split('\n');
    assert.equal(cells(line)[head.split(',').indexOf('name')], `'${name}`, 'the board CSV');

    const rec = { w: wallet(2), name, copyClass: 'strong', copyNet: 4, pnl: 10, savedAt: 0 };
    const [savedHead, savedLine] = savedCsv([rec], new Map()).split('\n');
    assert.equal(cells(savedLine)[savedHead.split(',').indexOf('name')], `'${name}`, 'the saved-list CSV');
  });
}

test('a negative number stays numeric in a CSV cell', () => {
  assert.equal(M.csvCell(-12.5), '-12.5');
  assert.equal(M.csvCell(0), '0');

  const [head, line] = M.toCsv([trader({ d7: -250, copyNet: -4 })], 'd7').split('\n');
  const cols = head.split(',');
  const row = cells(line);
  assert.equal(row[cols.indexOf('pnl_usd')], '-250');
  assert.equal(row[cols.indexOf('copy_score')], '-4');
  assert.equal(row[cols.indexOf('roi_pct')], '-0.5');

  const rec = { w: wallet(2), name: 'w', copyClass: 'loss_making', copyNet: -4, pnl: -42, savedAt: 0 };
  const [savedHead, savedLine] = savedCsv([rec], new Map()).split('\n');
  const savedRow = cells(savedLine);
  assert.equal(savedRow[savedHead.split(',').indexOf('pnl_usd')], '-42');
  assert.equal(savedRow[savedHead.split(',').indexOf('copy_score')], '-4');
});

test('a CSV cell holding a carriage return is quoted', () => {
  assert.equal(M.csvCell('a\rb'), '"a\rb"');
  assert.equal(M.csvCell('a\nb'), '"a\nb"');
  assert.equal(M.csvCell('a,b "c"'), '"a,b ""c"""');
  assert.equal(M.csvCell('plain'), 'plain');
});

test('a formula-shaped CSV cell keeps its quoting', () => {
  assert.equal(M.csvCell('=1,2'), `"'=1,2"`);
  assert.equal(M.csvCell('@a "b"'), `"'@a ""b"""`);
});

test('a formula-shaped value that is no string is written as text too', () => {
  assert.equal(M.csvCell(['=1+1']), `'=1+1`);
  assert.equal(M.csvCell(null), '');
  assert.equal(M.csvCell(NaN), '');
  assert.equal(M.csvCell(true), 'true');
});

test('the saved-list CSV writes the live name of a wallet still in the cohort as text', () => {
  const rec = { w: wallet(3), name: 'as saved', copyClass: 'strong', copyNet: 4, pnl: 10, savedAt: 0 };
  const cohort = new Map([[rec.w, { name: '=cmd', copyClass: 'marginal', copyNet: 1, pnl: -5 }]]);
  const [head, line] = savedCsv([rec], cohort).split('\n');
  const cols = head.split(',');
  const row = cells(line);
  assert.equal(row[cols.indexOf('name')], `'=cmd`);
  assert.equal(row[cols.indexOf('pnl_usd')], '-5');
  assert.equal(row[cols.indexOf('source')], 'current cohort');
});

// --- save buttons -----------------------------------------------------------

/** A stand-in for a button that counts its repaints. */
const fakeButton = (isConnected = true) => {
  const btn = { isConnected, paints: 0 };
  btn.paint = () => { btn.paints += 1; };
  return btn;
};

/** A store that counts its subscriptions, and a registry whose sweep the test
 *  runs by hand. */
function registry() {
  const store = createStore(fakeStorage(), () => 1);
  let subscriptions = 0;
  const subscribe = store.subscribe;
  store.subscribe = (fn) => { subscriptions += 1; return subscribe(fn); };
  const deferred = [];
  const buttons = liveButtons(store, (fn) => deferred.push(fn));
  return {
    store, buttons, deferred,
    subscriptions: () => subscriptions,
    flush: () => { while (deferred.length) deferred.shift()(); },
  };
}

test('save buttons share one store subscription', () => {
  const { store, buttons, subscriptions, flush } = registry();
  assert.equal(subscriptions(), 0, 'nothing is subscribed before the first button');
  const made = Array.from({ length: 30 }, fakeButton);
  for (const btn of made) buttons.track(btn, btn.paint);
  flush();
  assert.equal(subscriptions(), 1);
  assert.equal(buttons.size(), 30);

  store.save(wallet(1));
  assert.ok(made.every((btn) => btn.paints === 1), 'every button on the page is repainted once');
  store.remove(wallet(1));
  assert.ok(made.every((btn) => btn.paints === 2));
  assert.equal(subscriptions(), 1);
});

test('a save button that left the page is dropped from the repaint', () => {
  const { store, buttons, flush } = registry();
  const stays = fakeButton();
  const leaves = fakeButton();
  buttons.track(stays, stays.paint);
  buttons.track(leaves, leaves.paint);
  flush();

  leaves.isConnected = false;
  store.save(wallet(1));
  assert.equal(stays.paints, 1);
  assert.equal(leaves.paints, 0);
  assert.equal(buttons.size(), 1);

  // Coming back to the page later does not bring a dropped button back.
  leaves.isConnected = true;
  store.remove(wallet(1));
  assert.equal(leaves.paints, 0);
});

test('re-rendering a table keeps only the buttons on the page', () => {
  const { store, buttons, deferred, subscriptions, flush } = registry();
  let page = [];
  for (let render = 0; render < 50; render += 1) {
    for (const btn of page) btn.isConnected = false;
    // A row is built first and joins the page at the end of the render.
    page = Array.from({ length: 20 }, () => fakeButton(false));
    for (const btn of page) buttons.track(btn, btn.paint);
    assert.equal(deferred.length, 1, 'one sweep is queued for the whole batch');
    for (const btn of page) btn.isConnected = true;
    flush();
    assert.equal(buttons.size(), 20);
  }
  assert.equal(subscriptions(), 1);

  store.save(wallet(1));
  assert.ok(page.every((btn) => btn.paints === 1));
});

test('a button left off the page is dropped by the sweep', () => {
  const { buttons, flush } = registry();
  const orphan = fakeButton(false);
  buttons.track(orphan, orphan.paint);
  assert.equal(buttons.size(), 1);
  flush();
  assert.equal(buttons.size(), 0);
});

test('the sweep after a batch of new buttons runs by itself once the turn ends', async () => {
  const store = createStore(fakeStorage(), () => 1);
  const buttons = liveButtons(store);
  const gone = fakeButton(false);
  const kept = fakeButton();
  buttons.track(gone, gone.paint);
  buttons.track(kept, kept.paint);
  assert.equal(buttons.size(), 2);
  await null;
  assert.equal(buttons.size(), 1);
});

test('saveButton paints every button from the one subscription', async () => {
  const made = [];
  globalThis.document = {
    createElement: () => {
      const node = {
        isConnected: true, attrs: {},
        classList: { add() {}, remove() {} },
        setAttribute(k, v) { node.attrs[k] = v; },
      };
      made.push(node);
      return node;
    },
  };
  let subscriptions = 0;
  const subscribe = saved.subscribe;
  saved.subscribe = (fn) => { subscriptions += 1; return subscribe(fn); };
  try {
    const first = Array.from({ length: 20 }, () => saveButton(wallet(4)));
    await null;
    for (const btn of first) btn.isConnected = false;
    const second = Array.from({ length: 20 }, () => saveButton(wallet(4)));
    await null;
    assert.equal(subscriptions, 1, 'forty buttons, one listener on the store');
    assert.ok(second.every((btn) => btn.attrs['aria-pressed'] === 'false'));

    saved.save(wallet(4));
    assert.ok(second.every((btn) => btn.attrs['aria-pressed'] === 'true'), 'buttons on the page follow the store');
    assert.ok(first.every((btn) => btn.attrs['aria-pressed'] === 'false'), 'buttons that left are left alone');
    assert.equal(made.length, 40);
  } finally {
    saved.subscribe = subscribe;
    saved.clear();
    delete globalThis.document;
  }
});

// --- ROI --------------------------------------------------------------------

test('an unknown PnL gives an unknown ROI', () => {
  for (const d7 of [null, undefined, NaN]) {
    assert.equal(M.roiIn(trader({ d7, d7Vol: 50_000 }), 'd7'), null);
  }
  assert.equal(M.roiIn(trader({ d30: null, d30Vol: 100_000 }), 'd30'), null);
  assert.equal(M.roiIn(trader({ pnl: null, vol: 200_000 }), 'all'), null);
  assert.equal(M.roiIn(trader({ d7: 0, d7Vol: 50_000 }), 'd7'), 0, 'a known PnL of zero is a 0% wallet');
  assert.equal(M.roiIn(trader({ d7: -5000, d7Vol: 50_000 }), 'd7'), -10);
});

test('a wallet with an unknown PnL is treated like one with an unknown ROI everywhere ROI is read', () => {
  const gain = trader({ w: wallet(5), d7: 5000 });
  const loss = trader({ w: wallet(6), d7: -5000 });
  const unknown = trader({ w: wallet(7), d7: null });

  // The ROI order places it after every wallet with a figure.
  const order = M.buildBoard([unknown, loss, gain], { metric: 'roi', period: 'd7' });
  assert.deepEqual(order.map((t) => t.w), [gain.w, loss.w, unknown.w]);
  const watched = M.buildSaved([unknown, loss, gain], new Set([gain.w, loss.w, unknown.w]), { metric: 'roi', period: 'd7' });
  assert.deepEqual(watched.map((t) => t.w), [gain.w, loss.w, unknown.w]);

  // An ROI floor asks about a figure it does not have, so it fails the floor.
  const floor = { ...M.DEFAULT_FILTERS, roiMin: '-50' };
  assert.equal(M.passesFilters(unknown, floor, 'd7'), false);
  assert.equal(M.passesFilters(loss, floor, 'd7'), true);

  // The ROI slider counts it in no bin and is hidden when no wallet has a figure.
  const spec = M.filterSpec('roiMin');
  assert.equal(spec.value(unknown, 'd7'), null);
  assert.equal(M.filterAvailable(spec, [unknown], 'd7'), false);
  assert.deepEqual(M.histogram([spec.value(unknown, 'd7')], spec.stops()), new Array(spec.stops().length + 1).fill(0));

  // The table and the CSV show it as absent.
  assert.equal(M.signedPercent(M.roiIn(unknown, 'd7')), '—');
  const [head, line] = M.toCsv([unknown], 'd7').split('\n');
  const cols = head.split(',');
  assert.equal(cells(line)[cols.indexOf('roi_pct')], '');
  assert.equal(cells(line)[cols.indexOf('pnl_usd')], '');
});

// --- search -----------------------------------------------------------------

const boardSource = readFileSync(new URL('../public/board.js', import.meta.url), 'utf8');

/** board.js boots the page when it is imported, so the search wiring is lifted
 *  out of its source and run against stand-ins for the page. Timers fire when
 *  the test says so, and each lookup resolves when the test says so. */
function searchBox() {
  const start = boardSource.indexOf('function wireSearch() {');
  const end = boardSource.indexOf('\nfunction hitRow(', start);
  assert.ok(start >= 0 && end > start, 'wireSearch sits right before hitRow in board.js');

  const input = { value: '' };
  const hits = {
    hidden: true, rows: [],
    replaceChildren() { this.rows = []; },
    append(...nodes) { this.rows.push(...nodes); },
  };
  const timers = [];
  const lookups = [];
  const scope = {
    $: (selector) => (selector === '#q' ? input : hits),
    M,
    api: { search: (q) => new Promise((resolve, reject) => { lookups.push({ q, resolve, reject }); }) },
    hitRow: (h, note) => ({ wallet: h.wallet, note }),
    setTimeout: (fn) => timers.push(fn),
    clearTimeout: (id) => { if (id) timers[id - 1] = null; },
    document: { addEventListener() {} },
    location: {},
  };
  new Function(...Object.keys(scope), `${boardSource.slice(start, end)}\nwireSearch();`)(...Object.values(scope));

  return {
    hits, lookups,
    type(text) { input.value = text; input.oninput(); },
    /** Let the debounce run out; resolves once that lookup has been handled. */
    fire() {
      const fn = timers.at(-1);
      assert.ok(fn, 'a lookup is waiting on the debounce');
      timers[timers.length - 1] = null;
      return fn();
    },
  };
}

test('a search reply that lands after a newer one is dropped', async () => {
  const box = searchBox();
  box.type('ab');
  const older = box.fire();
  box.type('abc');
  const newer = box.fire();
  assert.deepEqual(box.lookups.map((l) => l.q), ['ab', 'abc']);

  box.lookups[1].resolve({ hits: [{ wallet: wallet(2) }] });
  await newer;
  assert.deepEqual(box.hits.rows.map((r) => r.wallet), [wallet(2)]);

  box.lookups[0].resolve({ hits: [{ wallet: wallet(1) }, { wallet: wallet(3) }] });
  await older;
  assert.deepEqual(box.hits.rows.map((r) => r.wallet), [wallet(2)], 'the list still answers "abc"');
  assert.equal(box.hits.hidden, false);
});

test('a search reply that lands while the next keystroke is still debouncing is dropped', async () => {
  const box = searchBox();
  box.type('ab');
  const older = box.fire();
  box.type('abc');

  box.lookups[0].resolve({ hits: [{ wallet: wallet(1) }] });
  await older;
  assert.deepEqual(box.hits.rows, []);

  const newer = box.fire();
  box.lookups[1].resolve({ hits: [{ wallet: wallet(2) }] });
  await newer;
  assert.deepEqual(box.hits.rows.map((r) => r.wallet), [wallet(2)]);
});

test('a search reply that lands after an address was pasted leaves the address row in place', async () => {
  const box = searchBox();
  box.type('ab');
  const older = box.fire();
  box.type(wallet(9));
  assert.deepEqual(box.hits.rows, [{ wallet: wallet(9), note: 'Open this wallet' }]);

  box.lookups[0].resolve({ hits: [{ wallet: wallet(1) }] });
  await older;
  assert.deepEqual(box.hits.rows, [{ wallet: wallet(9), note: 'Open this wallet' }]);
  assert.equal(box.hits.hidden, false);
});

test('a search reply that lands after the box was cleared stays hidden', async () => {
  const box = searchBox();
  box.type('ab');
  const older = box.fire();
  box.type('a');
  assert.equal(box.hits.hidden, true);

  box.lookups[0].resolve({ hits: [{ wallet: wallet(1) }] });
  await older;
  assert.equal(box.hits.hidden, true);
  assert.deepEqual(box.hits.rows, []);
});

test('the newest search reply paints, and a failed lookup hides the list', async () => {
  const box = searchBox();
  box.type('ab');
  const first = box.fire();
  box.lookups[0].resolve({ hits: [{ wallet: wallet(1) }, { wallet: wallet(2) }] });
  await first;
  assert.deepEqual(box.hits.rows.map((r) => r.wallet), [wallet(1), wallet(2)]);
  assert.equal(box.hits.hidden, false);

  box.type('abc');
  const second = box.fire();
  box.lookups[1].reject(new Error('busy'));
  await second;
  assert.deepEqual(box.hits.rows, []);
  assert.equal(box.hits.hidden, true);
});

// --- saved store ------------------------------------------------------------

test('a stored list is read back as it was written', () => {
  const mem = fakeStorage();
  const first = createStore(mem, () => 7);
  first.save(wallet(1), { name: 'w', pnl: 12 });
  const again = createStore(mem, () => 8);
  assert.deepEqual(again.list(), [{
    w: wallet(1), name: 'w', img: null, copyClass: null, copyNet: null, pnl: 12, savedAt: 7,
  }]);
});

test('an empty stored list reads as empty and leaves the old Following list alone', () => {
  const mem = fakeStorage({
    'polytrade.saved': '[]',
    'polytrade.following': JSON.stringify([wallet(1)]),
  });
  const store = createStore(mem, () => 1);
  assert.equal(store.count(), 0);
  assert.equal(mem.d['polytrade.saved'], '[]', 'nothing is written back');
});

test('a stored value that is no list falls back to the old Following list', () => {
  for (const raw of ['{"w":1}', '"text"', 'not json']) {
    const mem = fakeStorage({
      'polytrade.saved': raw,
      'polytrade.following': JSON.stringify([wallet(1)]),
    });
    const store = createStore(mem, () => 1);
    assert.deepEqual(store.list().map((r) => r.w), [wallet(1)], raw);
  }
});
