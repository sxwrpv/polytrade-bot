/* Small DOM helpers shared by the board and the trader page.
 * Charts keep the upstream geometry so a series drawn here matches one drawn
 * there; only the stroke colours move to the PolyTrade palette.
 */
import * as M from './screenerModel.js';

const NS = 'http://www.w3.org/2000/svg';
export const UP = '#0b9e63';
export const DOWN = '#b3242f';

export function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

const TONE_COLOR = {
  clear: 'var(--green-deep)', ahead: 'var(--green-text)', caution: 'var(--yellow)',
  negative: 'var(--red)', neutral: 'var(--cyan)', unknown: 'var(--faint)',
};

/** The Copy Score pill. `variant: 'signed'` prints +6 / −4 instead of 11%. */
export function copyChip(copyClass, netReturnPct, { variant = 'board', showState = true } = {}) {
  const def = M.classDef(copyClass);
  const numeric = M.scoreIsNumeric(copyClass, netReturnPct);
  // A number-only chip with no number to show would render as an empty pill.
  if (!showState && !numeric) return null;
  const n = el('span', 'cchip');
  n.style.setProperty('--tone', TONE_COLOR[def.tone]);
  n.title = `${def.chip} — ${def.line}`;
  n.setAttribute('aria-label',
    `Copy Score: ${numeric ? `${M.scoreSigned(netReturnPct)}. ${def.chip}` : def.chip}. A read on the trader, not on this trade.`);
  if (showState) n.append(el('span', 'cchip-state', def.chip));
  if (numeric) {
    n.append(el('span', 'cchip-num',
      variant === 'signed' ? M.scoreSigned(netReturnPct) : M.scoreMagnitude(netReturnPct)));
  }
  return n;
}

/** Week-over-week score move. Hidden below 5 points, exactly as upstream. */
export function scoreMove(delta) {
  if (!delta || !Number.isFinite(delta[0])) return null;
  const [pts, days] = delta;
  const n = Math.round(Math.abs(pts));
  if (n < 5) return null;
  const up = pts > 0;
  const node = el('span', `smove ${up ? 'up' : 'down'}`, `${up ? '▲' : '▼'} ${n} pts`);
  node.title = `Copy Score is ${n} ${n === 1 ? 'point' : 'points'} ${up ? 'higher' : 'lower'} than ${days} days ago`;
  return node;
}

const HARD_TO_MIRROR = {
  market_making: { label: 'Market maker', why: 'They earn mainly from the buy/sell spread, so copying means paying that spread instead of collecting it.' },
  arb: { label: 'Arbitrage', why: 'They profit by holding both sides of a market at once; a bot mirrors one side, which leaves the risk without the locked-in profit.' },
  frequency: { label: 'Very high frequency', why: 'They place 90+ orders a day, so their edge is often gone before a copy can fill.' },
};
export function hardToMirror({ mm, arb, freq }) {
  const out = [];
  if (mm === 'market_maker') out.push(HARD_TO_MIRROR.market_making);
  if (arb === 'arb') out.push(HARD_TO_MIRROR.arb);
  if (freq === 'vhft') out.push(HARD_TO_MIRROR.frequency);
  return out;
}
export function mirrorChip(trader) {
  const flags = hardToMirror(trader);
  if (!flags.length) return null;
  const n = el('span', 'hchip', flags.length === 1 ? flags[0].label : `${flags[0].label} +${flags.length - 1}`);
  n.title = `Harder to mirror — ${flags.map((f) => f.why).join(' ')}`;
  return n;
}

export function sparkline(values, width = 90, height = 30) {
  if (!values || values.length < 3) return el('span', 'coverage', 'no series');
  const min = Math.min(...values);
  const span = Math.max(...values) - min || 1;
  const pts = values.map((v, i) =>
    `${((i / (values.length - 1)) * width).toFixed(1)},${(height - 2 - ((v - min) / span) * (height - 4)).toFixed(1)}`
  ).join(' ');
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('aria-hidden', 'true');
  svg.style.cssText = `width:${width}px;height:${height}px;display:block`;
  const line = document.createElementNS(NS, 'polyline');
  line.setAttribute('points', pts);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', values[values.length - 1] >= values[0] ? UP : DOWN);
  line.setAttribute('stroke-width', '1.5');
  line.setAttribute('stroke-linejoin', 'round');
  svg.append(line);
  return svg;
}

export function areaChart(values, height = 168) {
  const v = (values || []).filter(Number.isFinite);
  if (v.length < 3) return el('p', 'coverage', 'No daily series for this window.');
  const min = Math.min(...v, 0);
  const span = Math.max(...v, 0) - min || 1;
  const x = (i) => 10 + (i / (v.length - 1)) * 700;
  const y = (n) => height - 8 - ((n - min) / span) * (height - 16);
  const pts = v.map((n, i) => `${x(i).toFixed(1)},${y(n).toFixed(1)}`).join(' ');
  const zero = y(0).toFixed(1);
  const up = v[v.length - 1] >= v[0];
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 720 ${height}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('aria-hidden', 'true');
  svg.style.cssText = 'width:100%;height:100%;display:block';
  const fill = document.createElementNS(NS, 'polygon');
  fill.setAttribute('points', `${x(0).toFixed(1)},${zero} ${pts} ${x(v.length - 1).toFixed(1)},${zero}`);
  fill.setAttribute('fill', up ? 'rgba(11,158,99,.14)' : 'rgba(179,36,47,.12)');
  const base = document.createElementNS(NS, 'line');
  base.setAttribute('x1', 10); base.setAttribute('y1', zero);
  base.setAttribute('x2', 710); base.setAttribute('y2', zero);
  base.setAttribute('stroke', 'rgba(20,32,26,0.12)'); base.setAttribute('stroke-width', '1');
  const line = document.createElementNS(NS, 'polyline');
  line.setAttribute('points', pts);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', up ? UP : DOWN);
  line.setAttribute('stroke-width', '2');
  line.setAttribute('stroke-linejoin', 'round');
  line.setAttribute('stroke-linecap', 'round');
  svg.append(fill, base, line);
  return svg;
}

export function avatar(name, wallet, src, size = 28) {
  const n = el('span', 'av');
  n.style.cssText = `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.36)}px`;
  const label = (name && !name.startsWith('0x') ? name.slice(0, 2) : wallet.slice(2, 4)).toUpperCase();
  if (src) {
    const img = document.createElement('img');
    img.src = src; img.alt = ''; img.loading = 'lazy';
    img.onerror = () => { img.remove(); n.textContent = label; };
    n.append(img);
  } else {
    n.textContent = label;
  }
  return n;
}

/** A money cell that colours by sign and never colours an unavailable value. */
export function moneyCell(value, { signed = true } = {}) {
  const text = signed ? M.signedMoney(value) : M.money(value);
  const cls = value == null || !Number.isFinite(value) ? '' : value >= 0 ? 'pos' : 'neg';
  return el('td', `num ${cls}`, text);
}
