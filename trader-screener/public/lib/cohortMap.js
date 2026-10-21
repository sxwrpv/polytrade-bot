/* The board as a map.
 *
 * Every wallet in the window, placed by volume (across) and PnL (up), coloured
 * by its Copy Score verdict. Wallets outside the board's current cut are
 * dimmed, and the volume and PnL filters are drawn as the lines they are, so
 * dragging a slider shows what it removes. Both axes are logarithmic because
 * money here spans five orders of magnitude; PnL uses a signed log so a loss
 * sits below the zero line instead of falling off the chart.
 *
 * Dots go on a canvas (the research snapshot holds thousands of wallets);
 * every label is DOM, so text stays crisp. The table below the map is the
 * accessible form of the same rows; the map is a picture of them.
 */
import * as M from './screenerModel.js';

const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
// The kit's curves: expo-out for arrivals, in-out for moves between states.
const easeOut = (t) => 1 - (1 - t) ** 4;
const easeInOut = (t) => (t < 0.5 ? 8 * t ** 4 : 1 - (-2 * t + 2) ** 4 / 2);

/** Signed log with a linear core under $100, so −$50 and +$50 stay apart. */
const slog = (v) => Math.sign(v) * Math.log10(1 + Math.abs(v) / 100);
const unslog = (y) => Math.sign(y) * (10 ** Math.abs(y) - 1) * 100;

const BANDS = [
  ['strong', 'Proven', '--map-proven'],
  ['marginal', 'Ahead', '--map-ahead'],
  ['uneconomic', 'Caution', '--map-caution'],
  ['loss_making', 'Losing', '--map-losing'],
  ['other', 'Unscored', '--map-other'],
];
const bandKey = (t) => {
  const b = M.bandOf(t);
  return BANDS.some(([k]) => k === b) ? b : 'other';
};

const MARGIN = { top: 12, right: 14, bottom: 28, left: 58 };

export function cohortMap({ onOpen }) {
  const node = el('figure', 'cohort-map');
  const head = el('div', 'cmap-head');
  const caption = el('figcaption');
  const title = el('b', null, 'The board as a map');
  const sub = el('span');
  caption.append(title, sub);
  const legend = el('div', 'cmap-legend');
  const keys = new Map();
  for (const [key, text, token] of BANDS) {
    const item = el('span', 'cmap-key');
    const sw = el('i');
    sw.style.setProperty('background', `var(${token})`);
    const count = el('b');
    item.append(sw, el('span', null, text), count);
    keys.set(key, count);
    legend.append(item);
  }
  head.append(caption, legend);

  const plot = el('div', 'cmap-plot');
  plot.setAttribute('role', 'img');
  const canvas = document.createElement('canvas');
  canvas.setAttribute('aria-hidden', 'true');
  const xAxis = el('div', 'cmap-axis cmap-x');
  const yAxis = el('div', 'cmap-axis cmap-y');
  const tip = el('div', 'cmap-tip');
  tip.setAttribute('aria-hidden', 'true');
  plot.append(canvas, xAxis, yAxis, tip);
  const note = el('p', 'cmap-note',
    'Each dot is a wallet. Dimmed dots are outside the board’s current filters. Click a dot to open the wallet.');
  node.append(head, plot, note);

  const ctx = canvas.getContext('2d');
  let points = [];          // current layout, in data space
  let shown = [];           // what is drawn this frame (data space, may be mid-tween)
  let domain = null;
  let thresholds = {};
  let hover = -1;
  let raf = 0;
  let colours = {};
  let size = { w: 0, h: 0 };

  const readColours = () => {
    const cs = getComputedStyle(node);
    colours = Object.fromEntries([
      ...BANDS.map(([k, , token]) => [k, cs.getPropertyValue(token).trim() || '#5c6b62']),
      ['dim', cs.getPropertyValue('--map-dim').trim() || '#c3cec7'],
      ['grid', cs.getPropertyValue('--map-grid').trim() || 'rgba(20,32,26,.12)'],
      ['zero', cs.getPropertyValue('--map-zero').trim() || 'rgba(20,32,26,.35)'],
      ['line', cs.getPropertyValue('--map-line').trim() || '#07603c'],
      ['ink', cs.getPropertyValue('--text').trim() || '#14201a'],
    ]);
  };

  const sx = (x) => MARGIN.left + ((x - domain.x0) / (domain.x1 - domain.x0)) * (size.w - MARGIN.left - MARGIN.right);
  const sy = (y) => MARGIN.top + (1 - (y - domain.y0) / (domain.y1 - domain.y0)) * (size.h - MARGIN.top - MARGIN.bottom);

  // Every decade gets a label until they would touch; then every other one.
  const xStep = () => ((size.w - MARGIN.left - MARGIN.right) / (domain.x1 - domain.x0) < 52 ? 2 : 1);

  function layoutAxes() {
    xAxis.replaceChildren();
    yAxis.replaceChildren();
    const step = xStep();
    for (let e = Math.ceil(domain.x0); e <= Math.floor(domain.x1); e += step) {
      const t = el('span', null, M.money(10 ** e));
      t.style.setProperty('--x', `${sx(e)}px`);
      xAxis.append(t);
    }
    for (const v of yTicks()) {
      const t = el('span', v === 0 ? 'is-zero' : null, v === 0 ? '$0' : M.money(v));
      t.style.setProperty('--y', `${sy(slog(v))}px`);
      yAxis.append(t);
    }
  }

  function yTicks() {
    const out = [0];
    for (let e = 3; e <= 9; e += 1) {
      for (const sign of [1, -1]) {
        const v = sign * 10 ** e;
        const y = slog(v);
        if (y >= domain.y0 && y <= domain.y1) out.push(v);
      }
    }
    return out.sort((a, b) => a - b);
  }

  function draw(progress = 1) {
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.w, size.h);
    if (!domain) return;

    // Gridlines: the kit's dashed 3 4, and a firmer line at zero PnL.
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = colours.grid;
    for (let e = Math.ceil(domain.x0); e <= Math.floor(domain.x1); e += 1) {
      const x = Math.round(sx(e)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, MARGIN.top); ctx.lineTo(x, size.h - MARGIN.bottom); ctx.stroke();
    }
    for (const v of yTicks()) {
      if (v === 0) continue;
      const y = Math.round(sy(slog(v))) + 0.5;
      ctx.beginPath(); ctx.moveTo(MARGIN.left, y); ctx.lineTo(size.w - MARGIN.right, y); ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.strokeStyle = colours.zero;
    const zy = Math.round(sy(0)) + 0.5;
    ctx.beginPath(); ctx.moveTo(MARGIN.left, zy); ctx.lineTo(size.w - MARGIN.right, zy); ctx.stroke();

    // Dots: what the filters remove first, faint, so the kept ones sit on top.
    const many = shown.length > 900;
    const r = many ? 2.1 : 3.2;
    const count = Math.ceil(shown.length * progress);
    for (const pass of [false, true]) {
      for (let i = 0; i < count; i++) {
        const p = shown[i];
        if (p.kept !== pass) continue;
        ctx.globalAlpha = pass ? 0.9 : many ? 0.22 : 0.3;
        ctx.fillStyle = pass ? colours[p.band] : colours.dim;
        ctx.beginPath();
        ctx.arc(sx(p.x), sy(p.y), pass ? r : r * 0.85, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.globalAlpha = 1;

    // The volume and PnL filters, drawn where they cut.
    ctx.strokeStyle = colours.line;
    ctx.setLineDash([5, 4]);
    ctx.lineWidth = 1.5;
    if (Number.isFinite(thresholds.volume) && thresholds.volume > 0) {
      const x = sx(clamp(Math.log10(thresholds.volume), domain.x0, domain.x1));
      ctx.beginPath(); ctx.moveTo(x, MARGIN.top); ctx.lineTo(x, size.h - MARGIN.bottom); ctx.stroke();
    }
    if (Number.isFinite(thresholds.pnl)) {
      const y = sy(clamp(slog(thresholds.pnl), domain.y0, domain.y1));
      ctx.beginPath(); ctx.moveTo(MARGIN.left, y); ctx.lineTo(size.w - MARGIN.right, y); ctx.stroke();
    }
    ctx.setLineDash([]);

    if (hover >= 0 && shown[hover]) {
      const p = shown[hover];
      ctx.strokeStyle = colours.ink;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(sx(p.x), sy(p.y), r + 4, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  function resize() {
    const w = plot.clientWidth;
    const h = plot.clientHeight;
    if (!w || !h) return;
    size = { w, h };
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    if (domain) layoutAxes();
    draw();
  }
  new ResizeObserver(() => { if (plot.clientWidth !== size.w || plot.clientHeight !== size.h) resize(); }).observe(plot);

  function animate(from, to, ms, ease, entering) {
    cancelAnimationFrame(raf);
    if (reduce || !from) { shown = to; draw(1); return; }
    const start = performance.now();
    const step = (now) => {
      const t = clamp((now - start) / ms, 0, 1);
      const k = ease(t);
      if (entering) {
        shown = to;
        draw(k);
      } else {
        shown = to.map((p) => {
          const a = from.get(p.w);
          return a ? { ...p, x: a.x + (p.x - a.x) * k, y: a.y + (p.y - a.y) * k } : p;
        });
        draw(1);
      }
      if (t < 1) raf = requestAnimationFrame(step);
      else { shown = to; draw(1); }
    };
    raf = requestAnimationFrame(step);
  }

  let firstPaint = true;
  let seen = false;
  new IntersectionObserver(([entry], observer) => {
    if (!entry.isIntersecting) return;
    observer.disconnect();
    seen = true;
    // Before the first render there is nothing to sweep in; render does it.
    if (points.length && !firstPaint) animate(true, points, 900, easeOut, true);
  }, { threshold: 0.2 }).observe(plot);

  /**
   * @param {object} o
   * @param {object[]} o.traders  every wallet in the universe for this window
   * @param {string} o.period
   * @param {Set<string>} o.kept  wallets on the board after every cut
   * @param {object} o.filters    the board's filter state
   */
  function render({ traders, period, kept, filters }) {
    if (!colours.dim) readColours();
    const prev = new Map(points.map((p) => [p.w, p]));
    const periodChanged = node.dataset.period && node.dataset.period !== period;
    node.dataset.period = period;
    const label = M.PERIOD_LABEL[period];
    sub.textContent = `${label} volume across, ${label} PnL up · log scales`;

    const placed = traders
      .map((t) => ({ t, vol: M.volumeIn(t, period), pnl: M.pnlIn(t, period) }))
      .filter(({ vol, pnl }) => Number.isFinite(vol) && vol > 0 && Number.isFinite(pnl))
      .map(({ t, vol, pnl }) => ({
        w: t.w, name: t.name, band: bandKey(t), vol, pnl,
        x: Math.log10(vol), y: slog(pnl), kept: kept.has(t.w),
      }))
      .sort((a, b) => a.x - b.x);
    // The axis starts at the 1st percentile's decade: a handful of dust-sized
    // wallets should not spend a third of the width on $1–$100.
    const floorX = placed.length ? Math.floor(placed[Math.floor(placed.length * 0.01)].x) : 0;
    points = placed.filter((p) => p.x >= floorX || p.kept);

    if (points.length) {
      const xs = points.map((p) => p.x);
      const ys = points.map((p) => p.y);
      const x0 = Math.floor(Math.min(...xs));
      const x1 = Math.max(x0 + 2, Math.ceil(Math.max(...xs)));
      const pad = 0.25;
      domain = { x0, x1, y0: Math.min(0, Math.min(...ys)) - pad, y1: Math.max(0, Math.max(...ys)) + pad };
    } else {
      domain = null;
    }
    thresholds = {
      volume: M.finiteFilter(filters.volumeMin) ?? NaN,
      pnl: M.finiteFilter(filters.pnlMin) ?? NaN,
    };

    const tally = new Map(BANDS.map(([k]) => [k, 0]));
    for (const p of points) if (p.kept) tally.set(p.band, tally.get(p.band) + 1);
    for (const [k, b] of keys) b.textContent = tally.get(k).toLocaleString('en-US');
    const keptCount = points.filter((p) => p.kept).length;
    plot.setAttribute('aria-label', `${points.length.toLocaleString('en-US')} wallets by ${label} volume and ${label} PnL; ${keptCount.toLocaleString('en-US')} pass the current filters. The table below lists them.`);
    note.hidden = !points.length;
    if (!points.length) {
      xAxis.replaceChildren();
      yAxis.replaceChildren();
      shown = [];
      draw();
      return;
    }

    if (!size.w) resize();
    layoutAxes();
    hover = -1;
    tip.classList.remove('on');
    if (firstPaint) {
      // The dots sweep in left to right the first time the map is on screen.
      firstPaint = false;
      if (seen && !reduce) animate(true, points, 900, easeOut, true);
      else { shown = seen || reduce ? points : []; draw(1); }
      return;
    }
    if (periodChanged) animate(prev, points, 650, easeInOut, false);
    else { shown = points; draw(1); }
  }

  // Hover and click: the nearest dot within reach, kept wallets first.
  function nearest(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const mx = clientX - rect.left;
    const my = clientY - rect.top;
    let best = -1;
    let bestD = 14 ** 2;
    let bestKept = false;
    shown.forEach((p, i) => {
      const d = (sx(p.x) - mx) ** 2 + (sy(p.y) - my) ** 2;
      if (d > 14 ** 2) return;
      if ((p.kept && !bestKept) || ((p.kept === bestKept) && d < bestD)) {
        best = i; bestD = d; bestKept = p.kept;
      }
    });
    return best;
  }

  function showTip(i) {
    const p = shown[i];
    tip.replaceChildren();
    const name = el('b', null, p.name || M.shortAddress(p.w));
    const band = el('span', `cmap-tip-band is-${p.band}`, BANDS.find(([k]) => k === p.band)[1]);
    const rows = el('span', 'cmap-tip-rows');
    rows.append(
      el('span', null, `PnL ${M.signedMoney(p.pnl)}`),
      el('span', null, `Volume ${M.money(p.vol)}`),
    );
    tip.append(name, band, rows);
    if (!p.kept) tip.append(el('span', 'cmap-tip-out', 'Outside the current filters'));
    const x = sx(p.x);
    const y = sy(p.y);
    const flip = x > size.w * 0.62;
    tip.style.setProperty('--tx', `${flip ? x - 14 : x + 14}px`);
    tip.style.setProperty('--ty', `${y}px`);
    tip.classList.toggle('is-flip', flip);
    tip.classList.add('on');
  }

  canvas.addEventListener('pointermove', (e) => {
    const i = nearest(e.clientX, e.clientY);
    if (i === hover) return;
    hover = i;
    canvas.classList.toggle('is-pointing', i >= 0);
    if (i >= 0) showTip(i); else tip.classList.remove('on');
    draw();
  });
  canvas.addEventListener('pointerleave', () => {
    hover = -1;
    tip.classList.remove('on');
    canvas.classList.remove('is-pointing');
    draw();
  });
  canvas.addEventListener('click', (e) => {
    const i = nearest(e.clientX, e.clientY);
    if (i >= 0) onOpen(shown[i].w);
  });

  return { node, render };
}
