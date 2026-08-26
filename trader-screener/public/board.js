/* Trader screener — rendering.
 *
 * All selection and ordering lives in lib/screenerModel.js; this file only
 * turns rows into DOM. Keeping the split means the board's rules can be
 * checked without a browser, which is how the polytrade screener is built.
 */
import * as M from './lib/screenerModel.js';
import * as R from './lib/render.js';
import { loadUniverse, api, SOURCE } from './lib/dataSource.js';
import { saved } from './lib/saved.js';
import { saveButton, savedPill, savedDrawer } from './lib/savedUi.js';

const { el } = R;
const $ = (s) => document.querySelector(s);

const DEFAULTS = {
  view: 'all',
  metric: M.DEFAULT_SORT,
  period: M.DEFAULT_PERIOD,
  cat: 'all',
  direction: 'desc',
};

const state = {
  ...DEFAULTS,
  bands: new Set(M.RECOMMENDED),
  filters: { ...M.DEFAULT_FILTERS },
  visible: 20,
  angleCat: 'all',
  angleDim: 'all',
  boardsShown: 8,
  eventsShown: 6,
};

// A shared link must reproduce the board it was copied from, so the controls
// are hydrated from the query string before the first render.
Object.assign(state, M.decodeState(location.search, state));

/** Push the current view into the URL without adding a history entry per click. */
function syncUrl() {
  const q = M.encodeState(state, DEFAULTS);
  history.replaceState(null, '', q ? `?${q}` : location.pathname);
}

/** Everything that changes which rows are shown funnels through here. */
function update({ resetPage = true } = {}) {
  if (resetPage) state.visible = 20;
  syncUrl();
  renderSidebar();
  renderBoard();
}

let ds = null, smi = null;

async function boot() {
  try {
    [ds, smi] = await Promise.all([
      loadUniverse({ period: state.period }),
      api.smi().catch(() => null),
    ]);
  } catch (e) {
    $('#results').replaceChildren(el('p', 'screener-state', `Could not load the cohort: ${e.message}`));
    return;
  }
  renderProvenance();
  renderSidebar();
  renderAll();
  const drawer = savedDrawer({ lookup: () => new Map(ds.traders.map((t) => [t.w.toLowerCase(), t])) });
  $('#saved-slot').append(savedPill(drawer.open));
  // A save made anywhere re-renders the board: the row highlight and the Saved
  // view both read the store, so they must not be allowed to go stale.
  saved.subscribe(() => renderBoard());

  wireSearch();
}

/* The board is a snapshot, and how old it is changes what it means. Saying so
 * costs one line; discovering it from a stale figure costs trust. */
function renderProvenance() {
  const age = M.snapshotAge(ds.meta.generatedAt);
  const host = $('#provenance');
  host.replaceChildren();
  host.append(SOURCE === 'polytrade'
    ? 'Rows from the PolyTrade screener cache.'
    : `Cohort snapshot of ${ds.traders.length.toLocaleString('en-US')} wallets, ${age.label}.`);

  const banner = $('#staleness');
  const warnings = [];
  if (SOURCE !== 'polytrade' && age.stale) {
    warnings.push(`The wallet cohort is ${age.label} and upstream normally regenerates it daily, so its rankings may have moved. Re-run node scripts/ingest.mjs to refresh it.`);
  }
  if (SOURCE !== 'polytrade' && ds.meta.boardsFrozen) {
    const date = ds.meta.boardsAsOf || 'an unknown date';
    warnings.push(`Category and event rankings are an archived snapshot frozen as of ${date}; they are historical research, not current rankings.`);
  }
  if (warnings.length) {
    banner.hidden = false;
    banner.replaceChildren();
    banner.append(el('strong', null, 'Snapshot limitations'));
    banner.append(el('p', null, warnings.join(' ')));
  } else {
    banner.hidden = true;
  }
}

const asOf = () => ds.meta.windowAnchor ?? ds.meta.generatedAt?.slice(0, 10) ?? null;
const archivedBoardsNote = () => ds.meta.boardsFrozen
  ? ` Archived snapshot: these rankings were frozen as of ${ds.meta.boardsAsOf || 'an unknown date'} and are not current.`
  : '';

const savedSet = () => new Set(saved.list().map((r) => r.w));

const rows = () => (state.view === 'saved'
  ? M.buildSaved(ds.traders, savedSet(), {
      metric: state.metric, period: state.period,
      filters: state.filters, direction: state.direction,
    })
  : M.buildBoard(ds.traders, {
      metric: state.metric, period: state.period, cat: state.cat, asOf: asOf(),
      bands: state.bands, filters: state.filters, direction: state.direction,
    }));

function renderAll() {
  renderBoard();
  renderTrending();
  renderIndex();
  renderAngles();
  renderEvents();
}

// --- sidebar controls -------------------------------------------------------

function segControl(label, options, current, onPick) {
  const wrap = el('div', 'control');
  wrap.append(el('div', 'control-label', label));
  const seg = el('div', 'seg');
  for (const [value, text] of options) {
    const b = el('button', null, text);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(value === current));
    b.onclick = () => { onPick(value); };
    seg.append(b);
  }
  wrap.append(seg);
  return wrap;
}

/* One numeric filter as a slider — the same contract as polytrade's
 * RangeFilter.jsx: parked at the "off" end means no filter at all, mirrored as
 * the empty string the model already treats as inactive. That distinction
 * matters: a threshold of zero is a real filter that hides everything below
 * zero, which is not what an untouched control should do. */
function rangeFilter({ key, label, min, max, step = 1, off = 'min', format }) {
  const value = state.filters[key];
  const numeric = Number(value);
  const active = value !== '' && value != null && Number.isFinite(numeric);
  const offValue = off === 'min' ? min : max;
  const position = active ? Math.min(max, Math.max(min, numeric)) : offValue;

  const wrap = el('div', `range-filter${active ? ' is-active' : ''}`);
  const lab = el('label');
  lab.htmlFor = `f-${key}`;
  lab.append(el('span', null, label), el('b', null, active ? format(numeric) : 'off'));

  const input = document.createElement('input');
  input.id = `f-${key}`;
  input.type = 'range';
  input.min = min; input.max = max; input.step = step; input.value = position;
  input.oninput = () => {
    const next = Number(input.value);
    state.filters[key] = next === offValue ? '' : String(next);
    update();
  };
  // Scrolling the page must never drag a filter.
  input.onwheel = (e) => e.currentTarget.blur();

  wrap.append(lab, input);
  return wrap;
}

function renderSidebar() {
  const host = $('#sidebar');
  host.replaceChildren();
  host.append(el('div', 'rail-heading', 'Controls'));

  host.append(segControl('View', [['all', 'All wallets'], ['saved', 'Saved']], state.view,
    (v) => { state.view = v; update(); }));

  host.append(segControl('Order by', M.SORTS, state.metric,
    (v) => { state.metric = v; update(); }));

  host.append(segControl('Period', M.PERIODS.map((p) => [p, M.PERIOD_LABEL[p]]), state.period,
    (v) => { state.period = v; update(); }));

  if (ds.groups?.length) {
    host.append(segControl('Category', [['all', 'All'], ...ds.groups.map((g) => [g, g])], state.cat,
      (v) => { state.cat = v; update(); }));
  }

  // Band filter is only meaningful while Copy Score is the ordering key.
  if (state.metric === 'copy') {
    const wrap = el('div', 'control');
    wrap.append(el('div', 'control-label', 'Copy Score bands'));
    const seg = el('div', 'seg');
    for (const band of M.CLASS_ORDER.filter((b) => b !== 'none')) {
      const b = el('button', null, M.classDef(band).chip);
      b.type = 'button';
      b.title = M.classDef(band).line;
      b.setAttribute('aria-pressed', String(state.bands.has(band)));
      b.onclick = () => {
        state.bands.has(band) ? state.bands.delete(band) : state.bands.add(band);
        if (!state.bands.size) state.bands = new Set(M.RECOMMENDED);
        update();
      };
      seg.append(b);
    }
    wrap.append(seg);
    wrap.append(el('p', 'rail-hint', 'Defaults to the two bands that clear copy costs. The negative bands are reachable; none is on by default.'));
    host.append(wrap);
  }

  host.append(el('div', 'rail-heading', 'Filters'));

  // Ceilings are set from the cohort itself rather than hard-coded, so the
  // slider range still means something after a snapshot refresh.
  const ceil = filterCeilings();
  host.append(rangeFilter({
    key: 'pnlMin', label: `PnL ${M.PERIOD_LABEL[state.period]} at least`,
    min: 0, max: ceil.pnl, step: Math.max(1, Math.round(ceil.pnl / 200)), format: M.money,
  }));
  host.append(rangeFilter({
    key: 'volumeMin', label: `Volume ${M.PERIOD_LABEL[state.period]} at least`,
    min: 0, max: ceil.volume, step: Math.max(1, Math.round(ceil.volume / 200)), format: M.money,
  }));
  host.append(rangeFilter({
    key: 'roiMin', label: `ROI ${M.PERIOD_LABEL[state.period]} at least`,
    min: -50, max: 100, step: 1, format: (v) => `${v}%`,
  }));
  host.append(rangeFilter({
    key: 'winrateMin', label: 'Win rate at least',
    min: 0, max: 0.9, step: 0.05, format: (v) => `${Math.round(v * 100)}%`,
  }));
  host.append(rangeFilter({
    key: 'copyNetMin', label: 'Copy Score at least',
    min: -20, max: 40, step: 1, format: (v) => M.scoreSigned(v),
  }));
  host.append(rangeFilter({
    key: 'activeDaysMin', label: 'Active days at least',
    min: 0, max: 365, step: 5, format: (v) => `${v}d`,
  }));
  // The ceiling filter, not a floor: a wallet whose average fill is $220K is
  // one you cannot mirror at a retail budget, however good its score.
  host.append(rangeFilter({
    key: 'avgSizeMax', label: 'Average fill at most',
    min: 50, max: ceil.avgSize, step: 50, off: 'max', format: M.money,
  }));

  const check = el('label', 'filter-check');
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = Boolean(state.filters.excludeHardToMirror);
  box.onchange = () => { state.filters.excludeHardToMirror = box.checked; update(); };
  const text = el('span');
  text.append(el('strong', null, 'Exclude hard to mirror'),
              el('small', null, 'Drops market makers, arbitrage and very-high-frequency wallets — their edge lives somewhere a copy cannot follow.'));
  check.append(box, text);
  host.append(check);

  const chips = M.activeFilterChips(state);
  if (chips.length) {
    const row = el('div', 'chip-row');
    for (const chip of chips) {
      if (!chip.clearable) {
        const c = el('span', 'chip chip-static', chip.text);
        c.title = 'Applied by the current ordering; not separately clearable.';
        row.append(c);
        continue;
      }
      const b = el('button', 'chip', `${chip.text}  ×`);
      b.type = 'button';
      b.title = 'Clear this filter';
      b.onclick = () => { clearFilter(chip.key); update(); };
      row.append(b);
    }
    if (chips.some((c) => c.clearable)) {
      const clear = el('button', 'chip chip-clear', 'Clear all');
      clear.type = 'button';
      clear.onclick = () => {
        state.filters = { ...M.DEFAULT_FILTERS };
        state.cat = 'all';
        update();
      };
      row.append(clear);
    }
    host.append(row);
  }
}

function clearFilter(key) {
  if (key === 'cat') { state.cat = 'all'; return; }
  if (key in M.DEFAULT_FILTERS) state.filters[key] = M.DEFAULT_FILTERS[key];
}

/** Slider ceilings taken from the cohort, rounded up to something readable. */
let ceilingCache = null;
function filterCeilings() {
  if (ceilingCache) return ceilingCache;
  const top = (pick) => {
    const vals = ds.traders.map(pick).filter((v) => Number.isFinite(v) && v > 0).sort((a, b) => a - b);
    if (!vals.length) return 1000;
    // 95th percentile, not the max: one outlier wallet should not make the
    // whole slider useless for everyone else.
    const v = vals[Math.floor(vals.length * 0.95)];
    const mag = 10 ** Math.floor(Math.log10(v));
    return Math.ceil(v / mag) * mag;
  };
  ceilingCache = {
    pnl: top((t) => t.pnl),
    volume: top((t) => t.vol),
    avgSize: top((t) => t.avgSize),
  };
  return ceilingCache;
}

// --- the board --------------------------------------------------------------

// `sort` names the ordering a header click selects; headers without one are
// not sortable and must not look like they are.
const COLUMNS = [
  { label: '#', cls: 'rank' },
  { label: 'Wallet', cls: '' },
  { label: 'Copy Score', cls: '', sort: 'copy',
    help: "Great traders aren't always great to copy. Copy Score is what was left on their finished trades after the spread you pay for filling behind them and the fees on the way in." },
  { label: 'ROI', cls: 'num', sort: 'roi' },
  { label: 'PnL', cls: 'num', sort: 'pnl' },
  { label: 'Open value', cls: 'num' },
  { label: 'Trend', cls: '' },
  { label: 'Volume', cls: 'num', sort: 'vol' },
  { label: 'Coverage', cls: '' },
  { label: '', cls: 'num' },
];

function renderBoard() {
  const list = rows();
  const host = $('#results');
  host.replaceChildren();

  const wrap = el('div', 'screener-tablewrap');
  const table = el('table', 'screener-table');
  const thead = el('thead');
  const hr = el('tr');
  for (const col of COLUMNS) {
    const th = el('th', col.cls);
    if (col.help) th.title = col.help;
    if (!col.sort) {
      th.textContent = col.label;
      hr.append(th);
      continue;
    }
    const active = state.metric === col.sort;
    const btn = el('button', `th-sort${active ? ' is-active' : ''}`);
    btn.type = 'button';
    btn.append(el('span', null, col.label));
    btn.append(el('span', 'th-arrow', active ? (state.direction === 'desc' ? '\u25be' : '\u25b4') : '\u2195'));
    // Clicking the active column flips direction; clicking another switches to it,
    // starting at descending because that is what a leaderboard means by "top".
    btn.onclick = () => {
      if (active) state.direction = state.direction === 'desc' ? 'asc' : 'desc';
      else { state.metric = col.sort; state.direction = 'desc'; }
      update();
    };
    th.setAttribute('aria-sort', active ? (state.direction === 'desc' ? 'descending' : 'ascending') : 'none');
    th.append(btn);
    hr.append(th);
  }
  thead.append(hr);
  table.append(thead);

  const tbody = el('tbody');
  const runs = new Map(M.bandRuns(list, state.metric).map((r) => [r.index, r]));
  list.slice(0, state.visible).forEach((t, i) => {
    const run = runs.get(i);
    if (run) tbody.append(bandRow(run));
    tbody.append(walletRow(t, i));
  });
  table.append(tbody);
  wrap.append(table);
  host.append(wrap);

  if (!list.length) {
    host.append(el('p', 'screener-state', state.view === 'saved'
      ? 'Nothing saved yet. Tap the heart on any wallet to keep it here, or paste an address into the search box.'
      : state.metric === 'copy'
        ? `No ${[...state.bands].map((b) => M.classDef(b).chip).join(' or ')} wallet in ${state.cat === 'all' ? 'this cohort' : state.cat} traded enough in this window. Try a longer period.`
        : `No wallet in ${state.cat} clears the bar for this measure.`));
  }

  const held = M.staleHeldBack(ds.traders, { cat: state.cat, period: state.period, asOf: asOf() });
  if (state.view === 'all' && held > 0) {
    host.append(el('p', 'screener-state',
      `Lifetime totals never decay, so ${held.toLocaleString('en-US')} ${held === 1 ? 'wallet that has' : 'wallets that have'} not traded in ${M.STALE_DAYS} days ${held === 1 ? 'is' : 'are'} held back here. They still rank on Polymarket's all-time board. This one answers who you could copy today.`));
  }

  if (list.length > state.visible) {
    const more = el('div', 'screener-more');
    const b = el('button', 'btn', `Show ${Math.min(20, list.length - state.visible)} more`);
    b.type = 'button';
    b.onclick = () => { state.visible += 20; renderBoard(); };
    more.append(b);
    host.append(more);
  }

  const count = $('#count');
  count.replaceChildren();
  count.append(el('span', null, list.length
    ? `${list.length.toLocaleString('en-US')} wallet${list.length === 1 ? '' : 's'}`
    : 'no wallets'));

  if (list.length) {
    const csv = el('button', 'btn btn-sm', 'CSV');
    csv.type = 'button';
    csv.title = 'Download the board exactly as filtered and ordered here';
    csv.onclick = () => downloadCsv(list);
    count.append(csv);
  }
  const link = el('button', 'btn btn-sm', 'Copy link');
  link.type = 'button';
  link.title = 'Copy a link that reproduces this exact view';
  link.onclick = async () => {
    await navigator.clipboard.writeText(location.href).catch(() => {});
    link.textContent = 'Copied';
    setTimeout(() => { link.textContent = 'Copy link'; }, 1400);
  };
  count.append(link);
}

function downloadCsv(rows) {
  const csv = M.toCsv(rows, state.period);
  const stamp = (ds.meta.windowAnchor || ds.meta.generatedAt || '').slice(0, 10);
  const name = `screener-${state.metric}-${state.period}${state.cat === 'all' ? '' : `-${state.cat.toLowerCase()}`}-${stamp}.csv`;
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function bandRow({ band, total }) {
  const tr = el('tr', 'band-row');
  const td = el('td');
  td.colSpan = COLUMNS.length;
  const box = el('div', `band band-${band}`);
  box.append(
    el('span', 'band-dot'),
    el('span', 'band-name', M.classDef(band).chip),
    el('span', 'band-n', `${total.toLocaleString('en-US')} ${total === 1 ? 'wallet' : 'wallets'}`),
    el('span', 'band-line', M.classDef(band).line),
  );
  td.append(box);
  tr.append(td);
  return tr;
}

function walletRow(t, i) {
  const roi = M.roiIn(t, state.period);
  const pnl = M.pnlIn(t, state.period);
  const tr = el('tr');
  if (saved.has(t.w)) tr.classList.add('selected');

  tr.append(el('th', 'rank', String(i + 1)));

  const who = el('td');
  const box = el('div', 'who');
  box.append(R.avatar(t.name, t.w, t.img, 28));
  const names = el('div');
  names.style.minWidth = '0';
  const short = M.shortAddress(t.w);
  const label = t.name || short;
  const a = el('a', 'who-name', label);
  a.href = M.traderPath(t.w);
  names.append(a);
  // Wallets with no display name are already labelled by their address; a
  // second copy of it under the first is noise, not information.
  if (label !== short) names.append(el('span', 'who-addr', short));
  box.append(names);
  const mirror = R.mirrorChip(t);
  if (mirror) box.append(mirror);
  who.append(box);
  tr.append(who);

  const score = el('td');
  score.append(R.copyChip(t.copyClass, t.copyNet));
  const move = R.scoreMove(ds.copyDelta?.[t.w]);
  if (move) score.append(move);
  tr.append(score);

  tr.append(el('td', `num ${roi == null ? '' : roi > 0 ? 'pos' : roi < 0 ? 'neg' : ''}`, M.signedPercent(roi)));
  tr.append(R.moneyCell(pnl));
  tr.append(el('td', 'num', M.money(t.openVal)));

  const trend = el('td');
  trend.append(R.sparkline(ds.spark?.[t.w], 84, 28));
  tr.append(trend);

  tr.append(el('td', 'num', M.money(M.volumeIn(t, state.period))));
  const cov = el('td');
  cov.append(coverageCell(t));
  tr.append(cov);
  tr.append(actionCell(t));
  return tr;
}

function coverageCell(t) {
  const n = el('span', 'coverage', M.coverageLabel(t, state.period));
  n.title = t.lastTradeDay ? `Last trade ${t.lastTradeDay}` : 'No trade day recorded';
  return n;
}

function actionCell(trader) {
  const td = el('td', 'num');
  const acts = el('div', 'rowacts');
  // The snapshot is read at save time, not at render time, so a wallet saved
  // today keeps the figures it had today even after the cohort turns over.
  acts.append(saveButton(trader.w, () => ({
    name: trader.name, img: trader.img,
    copyClass: trader.copyClass, copyNet: trader.copyNet, pnl: trader.pnl,
  })));
  const copy = el('a', 'btn btn-sm btn-primary', 'Copy');
  copy.href = M.botDeepLink(trader.w);
  copy.target = '_blank';
  copy.rel = 'noreferrer';
  copy.title = M.SUPPORTS_WALLET_DEEP_LINK
    ? 'Open PolyTrade with this wallet'
    : 'Opens PolyTrade — bring the address with you, the link does not carry it yet';
  acts.append(copy);
  td.append(acts);
  return td;
}

// --- trending ---------------------------------------------------------------

function renderTrending() {
  const host = $('#trending');
  const picks = ds.wow ? M.trendingTraders(ds, 4) : [];
  if (picks.length < 4) { host.hidden = true; return; }
  host.hidden = false;
  host.replaceChildren();

  host.append(sectionHead('Momentum', 'Who got hot this week',
    `Wallets that were already making money and made more of it this week, ranked by how much they added. Everyone here had a profitable week before this one, so the jump is acceleration rather than a first week on the board, and everyone here clears copy costs on their own tape. Week ending ${ds.wowAnchor}.`));

  const grid = el('div', 'trend-grid');
  for (const { trader: t, last7, prev7, diff } of picks) {
    const card = el('div', 'trend-card');
    const top = el('div', 'who');
    top.append(R.avatar(t.name, t.w, t.img, 30));
    const a = el('a', 'who-name', t.name || M.shortAddress(t.w));
    a.href = M.traderPath(t.w);
    top.append(a);
    const trendChip = R.copyChip(t.copyClass, t.copyNet, { variant: 'signed', showState: false });
    if (trendChip) top.append(trendChip);
    card.append(top);

    const hero = el('div', 'trend-hero');
    hero.append(el('span', diff >= 0 ? 'pos' : 'neg', M.signedMoney(diff)));
    if (prev7 > 0) hero.append(el('span', 'trend-pct', `+${Math.round((diff / prev7) * 100)}%`));
    card.append(hero, el('div', 'trend-sub', 'Added vs the week before'));
    card.append(el('div', 'trend-val', `${M.money(last7)} this week, up from ${M.money(prev7)}`));

    const spark = el('div', 'trend-spark');
    spark.append(R.sparkline(ds.spark[t.w], 250, 40));
    card.append(spark);

    const foot = el('div', 'trend-foot');
    foot.append(saveButton(t.w, () => ({
      name: t.name, img: t.img, copyClass: t.copyClass, copyNet: t.copyNet, pnl: t.pnl,
    })));
    const copy = el('a', 'btn btn-sm btn-primary', 'Copy');
    copy.href = M.botDeepLink(t.w); copy.target = '_blank'; copy.rel = 'noreferrer';
    foot.append(copy);
    card.append(foot);
    grid.append(card);
  }
  host.append(grid);
}

function sectionHead(kicker, title, lede) {
  const head = el('div', 'sec-head');
  const box = el('div');
  box.append(el('p', 'sec-kicker', kicker), el('h2', null, title));
  head.append(box);
  const wrap = el('div');
  wrap.append(head);
  if (lede) wrap.append(el('p', 'sec-lede', lede));
  return wrap;
}

// --- Smart Money Index ------------------------------------------------------

function renderIndex() {
  const host = $('#index');
  if (!smi || smi.error) { host.hidden = true; return; }
  host.hidden = false;
  host.replaceChildren();
  host.append(sectionHead('Market signal', 'Smart Money Index',
    'One number for whether the tracked cohort is making money right now, measured against its own normal.'));

  const card = el('div', 'index-card');
  const dial = el('div', 'index-dial');
  const score = el('div', 'index-score');
  score.append(document.createTextNode(String(smi.score)), el('span', null, '/100'));
  dial.append(score, el('div', 'index-zone', smi.zone));
  card.append(dial);

  const mid = el('div');
  mid.append(el('p', 'index-read', M.smartMoneyNarrative(smi)));
  const chart = el('div', 'index-chart');
  chart.append(el('div', 'control-label', 'Daily index · last 24 closes'));
  const box = el('div');
  box.style.height = '54px';
  box.append(R.areaChart((smi.history || []).map((h) => h.score ?? h.value).filter(Number.isFinite).slice(-24), 54));
  chart.append(box);
  mid.append(chart);
  card.append(mid);

  const comp = el('div', 'index-comp');
  for (const c of smi.componentViews || []) {
    const d = el('div');
    d.append(el('span', null, c.label), el('b', null, String(c.score)), el('i', null, c.sentence));
    comp.append(d);
  }
  card.append(comp);
  host.append(card);
}

// --- angle boards -----------------------------------------------------------

function renderAngles() {
  const host = $('#angles');
  if (!ds.angles?.length) { host.hidden = true; return; }
  host.hidden = false;
  host.replaceChildren();
  host.append(sectionHead("Who's good at what", 'Pick a market, see who beats it',
    `Choose a category and a bet type, like NBA spreads or election longshots, and every wallet with a record in it is ranked against the others. Open a board for the full list, with the hit rate and profit each earned in that exact market.${archivedBoardsNote()}`));

  const catBar = el('div', 'control');
  catBar.append(el('div', 'control-label', 'Category'));
  const available = [...new Set(M.realAngles(ds.angles).map((a) => a.group))];
  const catSeg = el('div', 'seg');
  for (const [v, label] of [['all', 'All'], ...ds.groups.filter((g) => available.includes(g)).map((g) => [g, g])]) {
    const b = el('button', null, label);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(v === state.angleCat));
    b.onclick = () => { state.angleCat = v; state.boardsShown = 8; renderAngles(); };
    catSeg.append(b);
  }
  catBar.append(catSeg);

  const dimBar = el('div', 'control');
  dimBar.append(el('div', 'control-label', 'Cut by'));
  const dimSeg = el('div', 'seg');
  const dims = [['all', 'Everything'], ...ds.structures.map((s) => [s.key, s.label]),
                ['LOW', 'Longshots'], ['MID', 'Coin flips'], ['HIGH', 'Favorites']];
  for (const [v, label] of dims) {
    const b = el('button', null, label);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(v === state.angleDim));
    b.onclick = () => { state.angleDim = v; state.boardsShown = 8; renderAngles(); };
    dimSeg.append(b);
  }
  dimBar.append(dimSeg);

  const bars = el('div');
  bars.style.cssText = 'display:flex;gap:26px;flex-wrap:wrap;margin-top:18px';
  bars.append(catBar, dimBar);
  host.append(bars);

  let list = M.realAngles(ds.angles);
  if (state.angleCat !== 'all') list = list.filter((a) => a.group === state.angleCat);
  if (state.angleDim !== 'all') list = list.filter((a) => a.dim === state.angleDim);
  const picked = M.spreadAngles(list);

  const grid = el('div', 'angle-grid');
  for (const a of picked.slice(0, state.boardsShown)) grid.append(angleCard(a));
  host.append(grid);

  if (!picked.length) host.append(el('p', 'screener-state', 'No board matches that combination.'));
  if (picked.length > state.boardsShown) {
    const more = el('div', 'screener-more');
    const b = el('button', 'btn', `Show ${Math.min(8, picked.length - state.boardsShown)} more boards`);
    b.type = 'button';
    b.onclick = () => { state.boardsShown += 8; renderAngles(); };
    more.append(b);
    host.append(more);
  }
}

function angleCard(a) {
  const card = el('div', 'angle-card');
  card.append(el('span', 'angle-tag', a.group));
  card.append(el('div', 'angle-name', `${M.stripGeneral(a.nicheLabel)} · ${a.dimLabel}`));
  const lost = a.cohort - a.winners;
  card.append(el('p', 'angle-blurb',
    `${a.blurb}. Measured on resolved trades in this exact slice, not on overall profit. ${lost.toLocaleString('en-US')} of the ${a.cohort.toLocaleString('en-US')} wallets here lost money doing it.`));

  const stats = el('div', 'angle-stats');
  for (const [label, value] of [
    ['Measured', a.cohort.toLocaleString('en-US')],
    ['Finished ahead', `${Math.round((a.winners / a.cohort) * 100)}%`],
    ['Median hit rate', `${Math.round(a.medianWin * 100)}%`],
    ['Best', M.signedMoney(a.topPnl)],
  ]) {
    const d = el('div');
    d.append(el('span', null, label), el('b', null, value));
    stats.append(d);
  }
  card.append(stats);

  const lo = a.pnlDist[1], hi = a.pnlDist[7], med = a.pnlDist[4];
  const bar = el('div', 'spread-bar');
  const fill = el('i');
  fill.style.width = `${Math.max(2, Math.min(100, ((med - lo) / ((hi - lo) || 1)) * 100))}%`;
  bar.append(fill);
  const foot = el('div', 'spread-foot');
  foot.append(el('span', null, `Bottom 10% ${M.signedMoney(lo)}`),
              el('span', null, `median ${M.signedMoney(med)}`),
              el('span', null, `Top 10% ${M.signedMoney(hi)}`));
  card.append(bar, foot);

  const lead = el('div', 'angle-lead');
  a.leaders.slice(0, 3).forEach((ld, i) => lead.append(leaderRow(a, ld, i)));
  card.append(lead);

  const cardFoot = el('div', 'angle-foot');
  const b = el('button', 'btn', 'Full board');
  b.type = 'button';
  b.onclick = () => openBoard(a);
  cardFoot.append(b);
  card.append(cardFoot);
  return card;
}

function leaderRow(angle, ld, i) {
  const meta = ds.walletMeta[ld.wallet] || {};
  const t = ds.traders.find((x) => x.w === ld.wallet);
  const row = el('div', 'lead-row');
  row.append(el('span', 'rk', String(i + 1)));
  row.append(R.avatar(meta.name || ld.wallet, ld.wallet, t?.img, 26));
  const who = el('div');
  who.style.minWidth = '0';
  const a = el('a', 'who-name', meta.name || M.shortAddress(ld.wallet));
  a.href = M.traderPath(ld.wallet);
  who.append(a);
  const cls = meta.copyClass ?? t?.copyClass;
  const leadChip = cls ? R.copyChip(cls, t?.copyNet, { variant: 'signed', showState: false }) : null;
  if (leadChip) who.append(' ', leadChip);
  const p = M.percentileIn(angle, ld.pnl);
  who.append(el('span', 'meta',
    `${ld.resolved.toLocaleString('en-US')} resolved · ${Math.round(ld.win * 100)}% hit rate${p ? ` · top ${Math.max(1, 100 - p.pctl)}% of ${p.n.toLocaleString('en-US')}` : ''}`));
  row.append(who);
  row.append(el('span', `n ${ld.pnl >= 0 ? 'pos' : 'neg'}`, M.signedMoney(ld.pnl)));
  return row;
}

function openBoard(a) {
  const scrim = el('div', 'scrim');
  const modal = el('div', 'modal');
  const head = el('div', 'modal-head');
  const box = el('div');
  box.append(el('p', 'sec-kicker', a.group), el('h3', null, `${M.stripGeneral(a.nicheLabel)} · ${a.dimLabel}`));
  box.append(el('p', 'sec-lede',
    `${a.blurb}. ${a.cohort.toLocaleString('en-US')} wallets measured, ${a.winners.toLocaleString('en-US')} finished ahead. Median hit rate ${Math.round(a.medianWin * 100)}%.`));
  const x = el('button', 'x', '×');
  x.type = 'button';
  x.onclick = () => scrim.remove();
  head.append(box, x);
  modal.append(head);

  const body = el('div', 'modal-body screener-tablewrap');
  const table = el('table', 'screener-table');
  table.style.minWidth = '640px';
  const thead = el('thead');
  const hr = el('tr');
  for (const [label, cls] of [['#', 'rank'], ['Wallet', ''], ['Resolved', 'num'], ['Hit rate', 'num'], ['PnL in this slice', 'num']]) {
    hr.append(el('th', cls, label));
  }
  thead.append(hr);
  const tbody = el('tbody');
  a.leaders.forEach((ld, i) => {
    const meta = ds.walletMeta[ld.wallet] || {};
    const t = ds.traders.find((x) => x.w === ld.wallet);
    const tr = el('tr');
    tr.append(el('th', 'rank', String(i + 1)));
    const who = el('td');
    const w = el('div', 'who');
    w.append(R.avatar(meta.name || ld.wallet, ld.wallet, t?.img, 24));
    const link = el('a', 'who-name', meta.name || M.shortAddress(ld.wallet));
    link.href = M.traderPath(ld.wallet);
    w.append(link);
    const cls = meta.copyClass ?? t?.copyClass;
    const rowChip = cls ? R.copyChip(cls, t?.copyNet, { variant: 'signed', showState: false }) : null;
    if (rowChip) w.append(rowChip);
    who.append(w);
    tr.append(who);
    tr.append(el('td', 'num', ld.resolved.toLocaleString('en-US')));
    tr.append(el('td', 'num', `${Math.round(ld.win * 100)}%`));
    tr.append(R.moneyCell(ld.pnl));
    tbody.append(tr);
  });
  table.append(thead, tbody);
  body.append(table);
  modal.append(body);
  scrim.append(modal);
  scrim.onclick = (e) => { if (e.target === scrim) scrim.remove(); };
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { scrim.remove(); document.removeEventListener('keydown', esc); }
  });
  document.body.append(scrim);
}

// --- events -----------------------------------------------------------------

function renderEvents() {
  const host = $('#events');
  if (!ds.events?.length) { host.hidden = true; return; }
  host.hidden = false;
  host.replaceChildren();
  host.append(sectionHead('Event trading', 'The next big events, and who is proven in that category',
    `A big event is coming and you want in. The useful question is not who is winning overall, it is who has traded this kind of market well before. Each card pairs an event with the wallets that lead our board for its category. Matching is by category, not by the individual event.${archivedBoardsNote()}`));

  const byId = new Map(ds.angles.map((a) => [a.id, a]));
  const grid = el('div', 'event-grid');
  for (const ev of ds.events.slice(0, state.eventsShown)) {
    const angle = ev.boards.map((id) => byId.get(id)).find(Boolean);
    const card = el('div', 'event-card');
    const top = el('div', 'event-top');
    top.append(el('span', 'angle-tag', ev.group));
    const days = daysOut(ev.end);
    top.append(el('span', `event-when${days <= 14 ? ' soon' : ''}`, untilLabel(days)));
    card.append(top, el('div', 'event-title', ev.title));
    const link = el('a', 'event-link', `${ev.active} market${ev.active === 1 ? '' : 's'} open on Polymarket ↗`);
    link.href = `https://polymarket.com/event/${ev.slug}`;
    link.target = '_blank'; link.rel = 'noreferrer';
    card.append(link);

    if (angle) {
      const lead = el('div', 'angle-lead');
      lead.style.marginTop = '12px';
      angle.leaders.slice(0, 3).forEach((ld, i) => lead.append(leaderRow(angle, ld, i)));
      card.append(el('p', 'control-label', `Best on ${M.stripGeneral(angle.nicheLabel)} ${angle.dimLabel.toLowerCase()}`), lead);
      const foot = el('div', 'angle-foot');
      const b = el('button', 'btn', `See all ${angle.cohort.toLocaleString('en-US')} wallets`);
      b.type = 'button';
      b.onclick = () => openBoard(angle);
      foot.append(b);
      card.append(foot);
    }
    grid.append(card);
  }
  host.append(grid);

  if (ds.events.length > state.eventsShown) {
    const more = el('div', 'screener-more');
    const b = el('button', 'btn', `Show ${Math.min(6, ds.events.length - state.eventsShown)} more events`);
    b.type = 'button';
    b.onclick = () => { state.eventsShown += 6; renderEvents(); };
    more.append(b);
    host.append(more);
  }
}

const daysOut = (end) => Math.round((new Date(`${end}T00:00:00Z`) - Date.now()) / 86400000);
function untilLabel(d) {
  if (d < 0) return 'Closing';
  if (d === 0) return 'Today';
  if (d < 31) return `${d} day${d === 1 ? '' : 's'} out`;
  const m = Math.round(d / 30);
  return `${m} month${m === 1 ? '' : 's'} out`;
}

// --- search -----------------------------------------------------------------

function wireSearch() {
  const input = $('#q');
  const hits = $('#hits');
  let timer;
  input.oninput = () => {
    clearTimeout(timer);
    const raw = input.value;
    const direct = M.resolveAddress(raw);
    hits.replaceChildren();
    if (direct) {
      hits.append(hitRow({ wallet: direct, name: M.shortAddress(direct) }, 'Open this wallet'));
      hits.hidden = false;
      return;
    }
    if (raw.trim().length < 2) { hits.hidden = true; return; }
    timer = setTimeout(async () => {
      const r = await api.search(raw.trim()).catch(() => ({ hits: [] }));
      hits.replaceChildren();
      for (const h of r.hits) hits.append(hitRow(h));
      hits.hidden = !r.hits.length;
    }, 180);
  };
  input.onkeydown = (e) => {
    if (e.key !== 'Enter') return;
    const direct = M.resolveAddress(input.value);
    if (direct) location.href = M.traderPath(direct);
  };
  document.addEventListener('click', (e) => { if (!e.target.closest('.screener-search')) hits.hidden = true; });
}

function hitRow(h, note) {
  const a = el('a');
  a.href = M.traderPath(h.wallet);
  a.append(R.avatar(h.name, h.wallet, h.img, 26));
  const box = el('div');
  box.style.minWidth = '0';
  box.append(el('span', 'hit-name', h.name || M.shortAddress(h.wallet)));
  box.append(el('span', 'hit-meta', note || `${M.shortAddress(h.wallet)} · ${M.signedMoney(h.pnl)} lifetime`));
  a.append(box);
  const hitChip = h.copyClass ? R.copyChip(h.copyClass, h.copyNet, { variant: 'signed', showState: false }) : null;
  if (hitChip) {
    hitChip.style.marginLeft = 'auto';
    a.append(hitChip);
  }
  return a;
}

boot();
