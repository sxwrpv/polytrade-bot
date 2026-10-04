/* Wallet profile.
 *
 * Every figure here is computed at request time from Polymarket's public read
 * APIs — see lib/metrics.mjs. Where the tape cannot answer a question, the
 * panel says so rather than printing a zero.
 */
import * as M from './lib/screenerModel.js';
import * as R from './lib/render.js';
import { api } from './lib/dataSource.js';
import { seriesChart, tradingClock } from './lib/chart.js';
import { saveButton, savedPill, savedDrawer } from './lib/savedUi.js';

const { el } = R;
const wallet = (location.pathname.match(/0x[a-fA-F0-9]{40}/) || [''])[0].toLowerCase();
const page = document.getElementById('page');

let data = null;
const state = { chart: 'equity', window: 'all', tab: 'trades' };
const CHART_WINDOWS = [['d7', '7D'], ['d30', '30D'], ['d90', '90D'], ['all', 'ALL']];

// The saved list is independent of whether this wallet's tape loads, so the
// pill and drawer mount immediately rather than behind the fetch. This has to
// sit below `data`: the drawer paints on construction and its lookup reads it.
const drawer = savedDrawer({ lookup: () => new Map(data?.board ? [[wallet, data.board]] : []) });
document.getElementById('saved-slot').append(savedPill(drawer.open));

boot();

async function boot() {
  if (!M.isAddress(wallet)) {
    page.replaceChildren(el('p', 'screener-state', 'That is not a wallet address.'));
    return;
  }
  document.getElementById('pm-link').href = M.polymarketProfile(wallet);
  try {
    data = await api.trader(wallet);
  } catch (e) {
    page.replaceChildren(el('p', 'screener-state', `Could not read this wallet: ${e.message}`));
    return;
  }
  document.title = `${data.profile.displayName || M.shortAddress(wallet)} — PolyTrade Screener`;
  render();
}

function render() {
  page.replaceChildren();
  page.append(head());
  if (!data.tapeComplete) page.append(truncationNotice());
  page.append(numbers(), copyScore(), performance(), edge(), behaviour(), activity(), footer());
}

// --- header -----------------------------------------------------------------

function head() {
  const box = el('header', 'wallet-head');
  const p = data.profile;
  box.append(R.avatar(p.displayName, wallet, p.profileImage, 56));
  const who = el('div');
  who.append(el('h1', null, p.displayName || M.shortAddress(wallet)));
  const id = el('div', 'wallet-id');
  id.append(el('span', 'addr', wallet));
  if (p.pseudonym) id.append(el('span', 'coverage', p.pseudonym));
  if (p.firstTradeAt) id.append(el('span', 'coverage', `First trade ${p.firstTradeAt.slice(0, 10)}`));
  who.append(id);

  const flags = el('div', 'wallet-flags');
  const cs = data.copyScore;
  flags.append(el('span', 'control-label', 'Copy Score'));
  flags.append(R.copyChip(cs.class, cs.netReturnPct, { variant: 'signed' }));
  if (data.board) {
    const mirror = R.mirrorChip(data.board);
    if (mirror) flags.append(mirror);
    if (data.board.niche) flags.append(el('span', 'hchip', data.board.niche.replace(/_/g, ' ')));
  }
  who.append(flags);
  box.append(who);

  const acts = el('div', 'wallet-acts');
  acts.append(saveButton(wallet, () => ({
    name: data.profile.displayName,
    img: data.profile.profileImage,
    copyClass: data.copyScore.class,
    copyNet: data.copyScore.netReturnPct,
    pnl: data.summary.pnl,
  })));
  const pm = el('a', 'btn', 'Polymarket ↗');
  pm.href = M.polymarketProfile(wallet); pm.target = '_blank'; pm.rel = 'noreferrer';
  const copy = el('a', 'btn btn-primary', 'Copy in PolyTrade');
  copy.href = M.botDeepLink(wallet); copy.target = '_blank'; copy.rel = 'noreferrer';
  copy.title = M.SUPPORTS_WALLET_DEEP_LINK
    ? 'Open PolyTrade with this wallet'
    : 'Opens PolyTrade — bring the address with you, the link does not carry it yet';
  const addr = el('button', 'btn', 'Copy address');
  addr.type = 'button';
  addr.onclick = async () => {
    await navigator.clipboard.writeText(wallet).catch(() => {});
    addr.textContent = 'Copied';
    setTimeout(() => { addr.textContent = 'Copy address'; }, 1400);
  };
  acts.append(addr, pm, copy);
  box.append(acts);
  return box;
}

/* Polymarket's public activity endpoint refuses any offset past 5000, so a
 * busy wallet's tape is its most recent 5500 rows and nothing earlier. Every
 * total on this page is therefore a total over that window, not a lifetime
 * one, and saying so is the difference between a partial figure and a wrong
 * one. */
function truncationNotice() {
  const box = el('div', 'notice');
  box.append(el('strong', null, 'Partial history'));
  box.append(el('p', null,
    `Polymarket's public API serves at most 5,500 activity rows per wallet, and this one has more. ` +
    `Everything below is computed over the most recent ${data.trades.length >= 200 ? '5,500 rows' : 'available rows'} ` +
    `— ${data.summary.trades.toLocaleString('en-US')} fills across ${data.summary.markets.toLocaleString('en-US')} markets, ` +
    `back to ${data.series.length ? data.series[0].date : 'an unknown date'}. Read the totals as "over this window", not "lifetime".`));
  return box;
}

/** A table cell holding a node rather than text. */
function tdNode(node, cls) {
  const td = el('td', cls);
  td.append(node);
  return td;
}

/** Entry prices run from sub-cent longshots to 99c favourites, so a flat
 *  round-to-cents would print "0¢" for a real 0.3¢ entry. */
function cents(price) {
  if (price == null || !Number.isFinite(price)) return '—';
  const c = price * 100;
  return c > 0 && c < 1 ? `${c.toFixed(1)}¢` : `${Math.round(c)}¢`;
}

function panel(title, kicker, bodyNode, headExtra) {
  const p = el('section', 'panel');
  const h = el('div', 'panel-head');
  const box = el('div');
  if (kicker) box.append(el('p', 'sec-kicker', kicker));
  box.append(el('h2', null, title));
  h.append(box);
  if (headExtra) h.append(headExtra);
  p.append(h);
  if (bodyNode) p.append(bodyNode);
  return p;
}

function kpi(label, value, sub, tone) {
  const d = el('div', 'kpi');
  d.append(el('span', null, label));
  d.append(el('b', tone || null, value));
  if (sub) d.append(el('i', null, sub));
  return d;
}

// --- the numbers that matter ------------------------------------------------

function numbers() {
  const s = data.summary;
  const grid = el('div', 'kpis');
  grid.append(kpi('PnL', M.signedMoney(s.pnl),
    `${M.signedMoney(s.settledPnl)} settled · ${M.signedMoney(s.openPnl)} open`,
    s.pnl >= 0 ? 'pos' : 'neg'));
  grid.append(kpi('Win rate', M.percent(s.winRate),
    s.resolved ? `${s.resolved.toLocaleString('en-US')} resolved` : 'no resolved positions'));
  grid.append(kpi('Volume', M.money(s.volume), 'buy + sell notional'));
  grid.append(kpi('Trades', s.trades.toLocaleString('en-US'),
    s.avgTradeSize ? `${M.money(s.avgTradeSize)} avg fill` : null));
  grid.append(kpi('ROI', M.signedPercent(s.roi), 'PnL ÷ volume',
    s.roi == null ? null : s.roi >= 0 ? 'pos' : 'neg'));

  const p = panel('The numbers that matter', 'Wallet history', grid);

  const live = el('div', 'kpis');
  live.append(kpi('Cash available', M.money(data.balance.cashUsd),
    data.balance.cashSource === 'unavailable' ? 'chain read unavailable' : 'USDC on Polygon, read now'));
  live.append(kpi('In open positions', M.money(s.openValue),
    `${s.openMarkets.toLocaleString('en-US')} open market${s.openMarkets === 1 ? '' : 's'}`));
  const allRank = data.ranks?.all?.pnl?.rank;
  live.append(kpi('Polymarket rank', allRank ? `#${allRank.toLocaleString('en-US')}` : '—',
    'all-time, by PnL'));
  live.append(kpi('Last trade', s.lastTradeAt ? ago(s.lastTradeAt) : '—',
    s.lastTradeAt ? s.lastTradeAt.slice(0, 10) : 'no fills on the loaded tape'));
  live.append(kpi('Active days', s.activeDays.toLocaleString('en-US'), 'days with at least one fill'));
  live.append(kpi('Rebates & rewards', M.money(s.rebateUsd),
    s.rebateRows ? `${s.rebateRows.toLocaleString('en-US')} rows — a copier earns none of this` : 'none on this tape'));
  p.append(live);
  return p;
}

function ago(iso) {
  const d = Math.floor((Date.now() - new Date(iso)) / 86400000);
  if (d <= 0) return 'today';
  if (d === 1) return 'yesterday';
  if (d < 30) return `${d}d ago`;
  const m = Math.floor(d / 30);
  return `${m}mo ago`;
}

// --- Copy Score -------------------------------------------------------------

function copyScore() {
  const cs = data.copyScore;
  const body = el('div', 'panel-body');

  if (cs.status === 'thin') {
    body.append(el('p', 'sec-lede',
      `Only ${cs.resolved} finished trade${cs.resolved === 1 ? '' : 's'} on the loaded tape — too few to separate a margin from noise. That is the absence of a verdict, not a negative one.`));
    return panel('Copy Score', 'What a copier would have kept', body);
  }

  const eqn = el('div', 'eqn');
  const step = (label, value, note) => {
    const d = el('div');
    d.append(el('span', null, label), el('b', null, value));
    if (note) d.append(el('i', null, note));
    return d;
  };
  eqn.append(step('Trader return', `${cs.shrunkReturnPct >= 0 ? '+' : '−'}${Math.abs(cs.shrunkReturnPct).toFixed(1)}%`,
    `adjusted down from ${cs.grossReturnPct.toFixed(1)}% raw, for ${cs.resolved} finished trades`));
  eqn.append(step('− Spread', `${cs.costSpreadPct.toFixed(1)}%`, 'you fill behind them, at a worse price'));
  eqn.append(step('− Fees', `${cs.costFeesPct.toFixed(1)}%`, 'venue fees plus the copy fee'));
  const net = el('div');
  net.append(el('span', null, '= Copier return'),
    el('b', cs.netReturnPct >= 0 ? 'pos' : 'neg', `${cs.netReturnPct >= 0 ? '+' : '−'}${Math.abs(cs.netReturnPct).toFixed(1)}%`));
  net.append(el('i', null, `typical wallet ${M.TYPICAL_TRADER_NET.toFixed(1)}%`));
  eqn.append(net);
  body.append(eqn);

  body.append(el('p', 'sec-lede', M.classDef(cs.class).line));
  if (cs.marginCents != null) {
    body.append(el('p', 'panel-note',
      `In cents: they average ${cents(cs.avgEntryPrice)} entries and clear about ${cs.marginCents.toFixed(1)}¢ of margin, against roughly ${cs.lineCents.toFixed(1)}¢ of copy cost.`));
  }
  if (cs.rebateShare != null && cs.rebateShare >= 5) {
    body.append(el('p', 'panel-note',
      `About ${cs.rebateShare.toFixed(0)}% of what this wallet made on finished markets came from maker rebates and rewards, which a copier does not earn. The copied result would look different from the number above.`));
  }
  if (cs.class === 'not_measurable') {
    body.append(el('p', 'panel-note',
      `${(data.holdTime.soldBeforeResolution * 100).toFixed(0)}% of their realised value arrived by selling out rather than by the market resolving, across ${(data.holdTime.soldEpisodeShare * 100).toFixed(0)}% of finished markets. Copy Score measures what is left for someone who mirrors a buy and holds to the result, so for this wallet it is not measuring the same thing. The threshold above which that applies is our reading, not a published constant.`));
  }
  body.append(el('p', 'panel-note',
    'Based on their finished trades to date, not a forecast. It assumes every one of their trades was copied exactly; your filters, budget and timing will change it. The spread and fee rates, and the shrinkage applied to thin records, are reconstructed from published examples rather than published constants.'));

  if (data.board && data.board.copyNet != null) {
    body.append(el('p', 'panel-note',
      `The ranked cohort scores this wallet ${M.scoreSigned(data.board.copyNet)} (${M.classDef(data.board.copyClass).chip}); the figure above is recomputed here from the live tape, so the two can differ.`));
  }
  return panel('Copy Score', 'What a copier would have kept', body);
}

// --- performance ------------------------------------------------------------

const CHARTS = [
  ['equity', 'Cumulative PnL'], ['daily', 'Daily PnL'],
  ['drawdown', 'Drawdown'], ['volume', 'Volume'], ['trades', 'Trades'],
];

function performance() {
  const body = el('div', 'panel-body');

  // Two independent switches: which series, and how far back. Upstream ties
  // them together; keeping them apart means changing one does not reset the other.
  const switches = el('div', 'chart-switches');
  const shapeSeg = el('div', 'seg-inline');
  for (const [k, label] of CHARTS) {
    const b = el('button', null, label);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(k === state.chart));
    b.onclick = () => { state.chart = k; render(); };
    shapeSeg.append(b);
  }
  const winSeg = el('div', 'seg-inline');
  for (const [k, label] of CHART_WINDOWS) {
    const b = el('button', null, label);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(k === state.window));
    b.onclick = () => { state.window = k; render(); };
    winSeg.append(b);
  }
  switches.append(shapeSeg, winSeg);

  const windowed = sliceSeries(data.series, state.window);
  const spec = CHART_SPEC[state.chart];
  const points = windowed.map((d) => ({ date: d.date, value: spec.pick(d) }));

  const box = el('div', 'chartbox');
  box.append(seriesChart(points, {
    format: spec.format, shape: spec.shape, label: spec.label, height: 210,
  }).node);
  body.append(box);

  if (windowed.length) {
    const note = windowed.length === data.series.length
      ? `${windowed.length.toLocaleString('en-US')} active day${windowed.length === 1 ? '' : 's'}, ${windowed[0].date} to ${windowed[windowed.length - 1].date}.`
      : `${windowed.length.toLocaleString('en-US')} of ${data.series.length.toLocaleString('en-US')} active days shown, ${windowed[0].date} to ${windowed[windowed.length - 1].date}.`;
    body.append(el('p', 'panel-note', note));
  } else {
    body.append(el('p', 'panel-note', 'No activity in this window.'));
  }

  const cols = el('div', 'cols3');
  cols.style.marginTop = '18px';

  const outcomes = el('div');
  outcomes.append(el('p', 'control-label', 'Day outcomes'));
  const o = data.dayOutcomes;
  const dl1 = el('dl', 'dl');
  addRow(dl1, 'Positive days', `${o.positive} (${pctOf(o.positive, data.series.length)})`);
  addRow(dl1, 'Negative days', String(o.negative));
  addRow(dl1, 'Flat days', String(o.flat));
  addRow(dl1, 'Average P&L day', M.signedMoney(o.avgDaily), o.avgDaily);
  addRow(dl1, 'Max drawdown', M.money(o.maxDrawdown), o.maxDrawdown);
  outcomes.append(dl1);
  cols.append(outcomes);

  const sizing = el('div');
  sizing.append(el('p', 'control-label', 'Order sizing'));
  const bars = el('div', 'bars');
  for (const band of data.orderSizing) {
    const row = el('div', 'b');
    row.append(el('span', null, band.label));
    const track = el('div', 'track');
    const fill = el('i');
    fill.style.width = `${Math.round(band.share * 100)}%`;
    track.append(fill);
    row.append(track, el('span', 'v', `${Math.round(band.share * 100)}%`));
    row.title = `${band.n.toLocaleString('en-US')} finished position${band.n === 1 ? '' : 's'}`;
    bars.append(row);
  }
  sizing.append(bars);
  sizing.append(el('p', 'panel-note', 'Share of finished positions per stake band.'));
  cols.append(sizing);

  const flow = el('div');
  flow.append(el('p', 'control-label', 'Trade flow'));
  const dl2 = el('dl', 'dl');
  const f = data.tradeFlow;
  const total = f.buys + f.sells || 1;
  addRow(dl2, 'Buy fills', `${f.buys.toLocaleString('en-US')} (${Math.round((f.buys / total) * 100)}%)`);
  addRow(dl2, 'Sell fills', `${f.sells.toLocaleString('en-US')} (${Math.round((f.sells / total) * 100)}%)`);
  addRow(dl2, 'Buy notional', M.money(f.buyNotional));
  addRow(dl2, 'Sell notional', M.money(f.sellNotional));
  addRow(dl2, 'Profit factor', data.summary.profitFactor == null ? '—' : data.summary.profitFactor.toFixed(2));
  flow.append(dl2);
  cols.append(flow);

  body.append(cols);
  return panel('Performance over time', 'The tape', body, switches);
}

/** How each series is picked off a day row, and how its values read. */
const CHART_SPEC = {
  equity: { label: 'Cumulative PnL', shape: 'area', pick: (d) => d.cumPnl, format: (v) => M.money(v) },
  daily: { label: 'Daily PnL', shape: 'bar', pick: (d) => d.pnl, format: (v) => M.money(v) },
  drawdown: { label: 'Drawdown', shape: 'area', pick: (d) => d.drawdown, format: (v) => M.money(v) },
  volume: { label: 'Volume', shape: 'bar', pick: (d) => d.volume, format: (v) => M.money(v) },
  trades: { label: 'Trades', shape: 'bar', pick: (d) => d.trades, format: (v) => v.toLocaleString('en-US') },
};

/** Slice the daily series to a window, counted back from the last active day
 *  rather than from today — a wallet that stopped trading a month ago should
 *  still show its last 7 active days, not an empty chart. */
function sliceSeries(series, window) {
  if (window === 'all' || !series.length) return series;
  const days = { d7: 7, d30: 30, d90: 90 }[window] ?? 30;
  const last = new Date(`${series[series.length - 1].date}T00:00:00Z`);
  const cut = new Date(last);
  cut.setUTCDate(cut.getUTCDate() - days + 1);
  const from = cut.toISOString().slice(0, 10);
  return series.filter((d) => d.date >= from);
}

const pctOf = (n, d) => (d ? `${Math.round((n / d) * 100)}%` : '—');
function addRow(dl, label, value, tone) {
  dl.append(el('dt', null, label));
  const dd = el('dd', tone == null || !Number.isFinite(tone) ? null : tone >= 0 ? 'pos' : 'neg', value);
  dl.append(dd);
}

// --- the edge ---------------------------------------------------------------

function edge() {
  const body = el('div', 'panel-body');
  const cols = el('div', 'cols2');

  // Niches
  const niches = el('div');
  niches.append(el('p', 'control-label', 'Where they win — by niche'));
  if (data.niches.length) {
    const t = el('table', 'screener-table');
    t.style.minWidth = '0';
    t.append(thead(['Niche', 'Bets', 'Hit rate', 'ROI', 'PnL']));
    const tb = el('tbody');
    for (const n of data.niches.slice(0, 8)) {
      const tr = el('tr');
      tr.append(el('td', null, n.label));
      tr.append(el('td', 'num', n.bets.toLocaleString('en-US')));
      tr.append(el('td', 'num', M.percent(n.winRate)));
      tr.append(el('td', 'num', M.signedPercent(n.roi)));
      tr.append(R.moneyCell(n.pnl));
      tb.append(tr);
    }
    t.append(tb);
    niches.append(t);
    niches.append(el('p', 'panel-note', 'Niche is read from the market slug and title — the public tape carries no category field, so this is a classification, not a label from the venue.'));
  } else {
    niches.append(el('p', 'panel-note', 'No finished positions to classify yet.'));
  }
  cols.append(niches);

  // Trade types
  const types = el('div');
  types.append(el('p', 'control-label', 'By bet type'));
  if (data.tradeTypes.length) {
    const t = el('table', 'screener-table');
    t.style.minWidth = '0';
    t.append(thead(['Type', 'Share', 'Hit rate', 'ROI', 'PnL']));
    const tb = el('tbody');
    for (const x of data.tradeTypes) {
      const tr = el('tr');
      tr.append(el('td', null, x.label));
      tr.append(el('td', 'num', M.percent(x.share)));
      tr.append(el('td', 'num', M.percent(x.winRate)));
      tr.append(el('td', 'num', M.signedPercent(x.roi)));
      tr.append(R.moneyCell(x.pnl));
      tb.append(tr);
    }
    t.append(tb);
    types.append(t);
  } else {
    types.append(el('p', 'panel-note', 'No finished positions to classify yet.'));
  }
  cols.append(types);
  body.append(cols);

  const cols2 = el('div', 'cols3');
  cols2.style.marginTop = '18px';

  // Hold time
  const hold = el('div');
  hold.append(el('p', 'control-label', 'Hold time & exits'));
  const h = data.holdTime;
  const dl = el('dl', 'dl');
  if (h.n) {
    addRow(dl, 'Median hold', hours(h.median));
    addRow(dl, '25th percentile', hours(h.p25));
    addRow(dl, '75th percentile', hours(h.p75));
  } else {
    addRow(dl, 'Median hold', '—');
  }
  addRow(dl, 'Value taken by selling', M.percent(h.soldBeforeResolution, 1));
  addRow(dl, 'Value from resolution', M.percent(h.heldToResolution, 1));
  addRow(dl, 'Markets with any sell', M.percent(h.soldEpisodeShare, 1));
  hold.append(dl);
  if (!h.n) hold.append(el('p', 'panel-note', 'No sell fills on the loaded tape, so hold percentiles are unavailable.'));
  cols2.append(hold);

  // Price brackets
  const brackets = el('div');
  brackets.append(el('p', 'control-label', 'Entry-price profile'));
  if (data.priceBrackets.length) {
    const dl2 = el('dl', 'dl');
    for (const b of data.priceBrackets) addRow(dl2, `${b.label} · ${b.n} bets`, M.signedMoney(b.pnl), b.pnl);
    brackets.append(dl2);
  } else {
    brackets.append(el('p', 'panel-note', 'No finished positions yet.'));
  }
  cols2.append(brackets);

  // Balance
  const bal = el('div');
  bal.append(el('p', 'control-label', 'Balance & funding'));
  const dl3 = el('dl', 'dl');
  addRow(dl3, 'Cash available', M.money(data.balance.cashUsd));
  addRow(dl3, 'In open positions', M.money(data.balance.inOpenPositions));
  addRow(dl3, 'Total account value', M.money(data.balance.totalAccountValue));
  addRow(dl3, 'Net deposited', '—');
  addRow(dl3, 'True ROI (PnL ÷ deposited)', '—');
  bal.append(dl3);
  bal.append(el('p', 'panel-note', 'Deposits and withdrawals are not exposed by the public tape, so true ROI stays unavailable rather than being guessed at.'));
  cols2.append(bal);
  body.append(cols2);

  // Trading clock
  const clockWrap = el('div');
  clockWrap.style.marginTop = '18px';
  clockWrap.append(el('p', 'control-label', 'Trading clock · hour × day (UTC)'));
  clockWrap.append(clock());
  body.append(clockWrap);

  return panel('Where they win', 'The edge', body);
}

function hours(v) {
  if (v == null || !Number.isFinite(v)) return '—';
  if (v < 1) return `${Math.round(v * 60)}m`;
  if (v < 48) return `${v.toFixed(1)}h`;
  return `${(v / 24).toFixed(1)}d`;
}

function thead(labels) {
  const h = el('thead');
  const tr = el('tr');
  labels.forEach((l, i) => tr.append(el('th', i === 0 ? '' : 'num', l)));
  h.append(tr);
  return h;
}

function clock() {
  return tradingClock(data.clock, { money: M.money }).node;
}

// --- behaviour --------------------------------------------------------------

function behaviour() {
  const b = data.behaviour;
  const body = el('div', 'panel-body');
  const cols = el('div', 'cols2');

  const left = el('div');
  const dl = el('dl', 'dl');
  addRow(dl, 'Median hold', hours(b.medianHoldHours));
  addRow(dl, 'Win / loss asymmetry', b.winLossAsymmetry == null ? '—' : `${b.winLossAsymmetry.toFixed(2)}×`);
  addRow(dl, 'Sells before close', M.percent(b.sellBeforeClose, 1));
  addRow(dl, 'Hedged markets', `${M.percent(b.hedgedMarketPct, 1)} (${b.hedgedMarkets})`);
  left.append(dl);
  cols.append(left);

  const right = el('div');
  const dl2 = el('dl', 'dl');
  addRow(dl2, 'Average entry price', cents(b.avgEntryPrice));
  addRow(dl2, 'Profitable weeks', M.percent(b.profitableWeeks));
  addRow(dl2, 'Maker / taker', '—');
  addRow(dl2, 'Sharpe / Sortino', '—');
  right.append(dl2);
  cols.append(right);
  body.append(cols);

  body.append(el('p', 'panel-note',
    'Maker/taker and risk ratios need order-level chain logs, which the public read API does not expose. They stay unavailable rather than being estimated from fills.'));

  if (data.board) {
    const cohort = el('div');
    cohort.style.marginTop = '16px';
    cohort.append(el('p', 'control-label', 'As the ranked cohort sees this wallet'));
    const dl3 = el('dl', 'dl');
    const bd = data.board;
    addRow(dl3, 'Maker fill ratio', bd.maker == null ? '—' : M.percent(bd.maker, 1));
    addRow(dl3, 'Median hold', hours(bd.hold));
    addRow(dl3, 'Profitable weeks', bd.weeksUp == null ? '—' : M.percent(bd.weeksUp, 1));
    addRow(dl3, 'Max drawdown', M.money(bd.maxDD), bd.maxDD);
    addRow(dl3, 'Average fill size', M.money(bd.avgSize));
    addRow(dl3, 'Lifetime fills', bd.fills == null ? '—' : bd.fills.toLocaleString('en-US'));
    cohort.append(dl3);
    cohort.append(el('p', 'panel-note', 'These come from the cached cohort snapshot, computed over the full chain history rather than the tape loaded here.'));
    body.append(cohort);
  }

  return panel('Style & behaviour', 'The edge', body);
}

// --- activity ---------------------------------------------------------------

const TABS = [['trades', 'Latest fills'], ['buys', 'Buys'], ['sells', 'Sells'],
              ['open', 'Open positions'], ['wins', 'Biggest wins'], ['losses', 'Worst losses']];

function activity() {
  const body = el('div', 'panel-body');
  const seg = el('div', 'seg-inline');
  for (const [k, label] of TABS) {
    const b = el('button', null, label);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(k === state.tab));
    b.onclick = () => { state.tab = k; render(); };
    seg.append(b);
  }

  const wrap = el('div', 'screener-tablewrap');
  wrap.append(state.tab === 'open' ? openTable()
    : state.tab === 'wins' ? finishedTable(data.positions.topWins)
    : state.tab === 'losses' ? finishedTable(data.positions.worstLosses)
    : fillsTable());
  body.append(wrap);
  return panel('Activity', 'The tape', body, seg);
}

function marketCell(row) {
  const td = el('td');
  const box = el('div', 'mkt');
  if (row.icon) {
    const img = document.createElement('img');
    img.src = row.icon; img.alt = ''; img.loading = 'lazy';
    img.onerror = () => img.remove();
    box.append(img);
  }
  const a = el('a', null, row.title || '—');
  a.href = row.eventSlug ? `https://polymarket.com/event/${row.eventSlug}` : '#';
  a.target = '_blank'; a.rel = 'noreferrer';
  a.title = row.title || '';
  box.append(a);
  td.append(box);
  return td;
}

function fillsTable() {
  const rows = data.trades.filter((t) =>
    state.tab === 'buys' ? t.side === 'BUY' : state.tab === 'sells' ? t.side === 'SELL' : true).slice(0, 60);
  const t = el('table', 'screener-table');
  t.append(thead(['Market', 'Side', 'Outcome', 'Price', 'Size', 'Notional', 'When']));
  const tb = el('tbody');
  if (!rows.length) {
    const tr = el('tr');
    const td = el('td'); td.colSpan = 7;
    td.append(el('p', 'screener-state', 'No fills of that kind on the loaded tape.'));
    tr.append(td); tb.append(tr);
  }
  for (const r of rows) {
    const tr = el('tr');
    tr.append(marketCell(r));
    tr.append(tdNode(sideChip(r.side)));
    tr.append(el('td', null, r.outcome || '—'));
    tr.append(el('td', 'num', cents(r.price)));
    tr.append(el('td', 'num', Math.round(r.size).toLocaleString('en-US')));
    tr.append(el('td', 'num', M.money(r.usdcSize)));
    tr.append(el('td', 'num', new Date(r.timestamp * 1000).toISOString().slice(0, 10)));
    tb.append(tr);
  }
  t.append(tb);
  return t;
}

function sideChip(side) {
  return el('span', `side ${side === 'SELL' ? 'sell' : 'buy'}`, side || '—');
}

function openTable() {
  const rows = data.positions.open;
  const t = el('table', 'screener-table');
  t.append(thead(['Market', 'Outcome', 'Entry → now', 'Size', 'Value', 'PnL']));
  const tb = el('tbody');
  if (!rows.length) {
    const tr = el('tr'); const td = el('td'); td.colSpan = 6;
    td.append(el('p', 'screener-state', 'No open positions.'));
    tr.append(td); tb.append(tr);
  }
  for (const r of rows) {
    const tr = el('tr');
    tr.append(marketCell(r));
    tr.append(el('td', null, r.outcome || '—'));
    tr.append(el('td', 'num', `${cents(r.avgPrice)} → ${cents(r.curPrice)}`));
    tr.append(el('td', 'num', Math.round(r.size).toLocaleString('en-US')));
    tr.append(el('td', 'num', M.money(r.currentValue)));
    tr.append(R.moneyCell(r.cashPnl));
    tb.append(tr);
  }
  t.append(tb);
  return t;
}

function finishedTable(rows) {
  const t = el('table', 'screener-table');
  t.append(thead(['Market', 'Side', 'Entry', 'Stake', 'Exit', 'Ended', 'Realised']));
  const tb = el('tbody');
  if (!rows.length) {
    const tr = el('tr'); const td = el('td'); td.colSpan = 7;
    td.append(el('p', 'screener-state', 'No finished markets on the loaded tape.'));
    tr.append(td); tb.append(tr);
  }
  for (const r of rows) {
    const tr = el('tr');
    tr.append(marketCell(r));
    tr.append(el('td', null, r.outcome || '—'));
    tr.append(el('td', 'num', cents(r.avgPrice)));
    tr.append(el('td', 'num', M.money(r.stake)));
    tr.append(tdNode(el('span', 'side ' + (r.soldOut ? 'sell' : 'buy'), r.soldOut ? 'Sold' : 'Resolved')));
    tr.append(el('td', 'num', r.endedAt));
    tr.append(R.moneyCell(r.realizedPnl));
    tb.append(tr);
  }
  t.append(tb);
  return t;
}

function footer() {
  const f = el('footer', 'screener-footer');
  f.append(el('p', null,
    `Read live from Polymarket's public API at ${new Date(data.fetchedAt).toISOString().slice(0, 16).replace('T', ' ')}Z. ` +
    `${data.summary.trades.toLocaleString('en-US')} fills across ${data.summary.markets.toLocaleString('en-US')} markets, ` +
    `${data.summary.resolved.toLocaleString('en-US')} of them finished${data.tapeComplete ? '' : ' — partial history, see the notice above'}. ` +
    'Past wallet activity does not predict future results, and nothing here is financial advice.'));
  return f;
}
