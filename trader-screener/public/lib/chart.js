/* Interactive chart and heat-map.
 *
 * Both are built the same way: an SVG that scales, plus a transparent overlay
 * that turns a pointer position into a data index. Nothing here reads layout
 * from the viewBox — the overlay's own bounding rect is the source of truth, so
 * the maths survives `preserveAspectRatio="none"` and any container width.
 *
 * Both are keyboard-operable and both announce the focused value, because a
 * chart whose numbers are only reachable by hovering is a chart half the
 * readers cannot use.
 */

const NS = 'http://www.w3.org/2000/svg';
export const UP = '#0b9e63';
export const DOWN = '#b3242f';

const svgEl = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
};
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/* ---------------------------------------------------------------------------
 * Series chart
 * ------------------------------------------------------------------------ */

/**
 * @param {{date:string, value:number}[]} points
 * @param {object} opts
 * @param {(v:number)=>string} opts.format   value formatter for the readout
 * @param {'area'|'bar'} opts.shape          area for cumulative, bar for daily
 * @param {string} opts.label                what the value is, for the readout
 */
export function seriesChart(points, {
  format = (v) => String(v), shape = 'area', label = 'Value', height = 200,
} = {}) {
  const wrap = el('div', 'chart');
  const clean = points.filter((p) => Number.isFinite(p.value));

  if (clean.length < 2) {
    wrap.append(el('p', 'chart-empty', 'Not enough data in this window to draw a series.'));
    return { node: wrap, focus: () => {} };
  }

  // Readout sits above the plot and always shows something: the last point
  // until the reader moves the cursor, so the chart is never mute.
  const readout = el('div', 'chart-readout');
  const rDate = el('span', 'chart-readout-date');
  const rValue = el('span', 'chart-readout-value');
  const rDelta = el('span', 'chart-readout-delta');
  readout.append(rValue, rDate, rDelta);
  wrap.append(readout);

  const W = 720;
  const H = height;
  const PAD = { top: 10, right: 10, bottom: 18, left: 10 };

  const values = clean.map((p) => p.value);
  const lo = Math.min(...values, 0);
  const hi = Math.max(...values, 0);
  const span = hi - lo || 1;
  const x = (i) => PAD.left + (i / (clean.length - 1)) * (W - PAD.left - PAD.right);
  const y = (v) => H - PAD.bottom - ((v - lo) / span) * (H - PAD.top - PAD.bottom);

  const rising = values[values.length - 1] >= values[0];
  const stroke = rising ? UP : DOWN;
  const gradId = `g${Math.random().toString(36).slice(2, 9)}`;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none',
    role: 'img', 'aria-label': `${label} over ${clean.length} days`,
  });
  svg.style.cssText = `width:100%;height:${H}px;display:block;touch-action:pan-y`;

  const defs = svgEl('defs');
  const grad = svgEl('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(
    svgEl('stop', { offset: '0%', 'stop-color': stroke, 'stop-opacity': 0.22 }),
    svgEl('stop', { offset: '100%', 'stop-color': stroke, 'stop-opacity': 0.02 }),
  );
  defs.append(grad);
  svg.append(defs);

  // Zero line, drawn only when the series actually crosses or sits above it.
  const zeroY = y(0);
  if (lo < 0 || hi > 0) {
    svg.append(svgEl('line', {
      x1: PAD.left, y1: zeroY, x2: W - PAD.right, y2: zeroY,
      stroke: 'rgba(20,32,26,0.16)', 'stroke-width': 1, 'stroke-dasharray': '3 3',
    }));
  }

  if (shape === 'bar') {
    const bw = Math.max(1, (W - PAD.left - PAD.right) / clean.length - 1);
    const bars = svgEl('g');
    clean.forEach((p, i) => {
      const top = Math.min(y(p.value), zeroY);
      const h = Math.max(1, Math.abs(y(p.value) - zeroY));
      bars.append(svgEl('rect', {
        x: x(i) - bw / 2, y: top, width: bw, height: h,
        fill: p.value >= 0 ? UP : DOWN, 'fill-opacity': 0.55,
      }));
    });
    svg.append(bars);
  } else {
    const pts = clean.map((p, i) => `${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ');
    svg.append(svgEl('polygon', {
      points: `${x(0).toFixed(1)},${zeroY.toFixed(1)} ${pts} ${x(clean.length - 1).toFixed(1)},${zeroY.toFixed(1)}`,
      fill: `url(#${gradId})`,
    }));
    svg.append(svgEl('polyline', {
      points: pts, fill: 'none', stroke, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round',
      'vector-effect': 'non-scaling-stroke',
    }));
  }

  // Crosshair, hidden until the pointer or keyboard puts it somewhere.
  const cross = svgEl('g', { opacity: '0' });
  const vline = svgEl('line', {
    y1: PAD.top, y2: H - PAD.bottom, stroke: 'rgba(20,32,26,0.34)',
    'stroke-width': 1, 'vector-effect': 'non-scaling-stroke',
  });
  const dot = svgEl('circle', { r: 4, fill: stroke, stroke: '#eef2ef', 'stroke-width': 2 });
  cross.append(vline, dot);
  svg.append(cross);
  wrap.append(svg);

  // Axis feet: first and last date, and the range the plot covers.
  const feet = el('div', 'chart-axis');
  feet.append(
    el('span', null, clean[0].date),
    el('span', 'chart-axis-range', `${format(lo)} — ${format(hi)}`),
    el('span', null, clean[clean.length - 1].date),
  );
  wrap.append(feet);

  let index = clean.length - 1;
  const paint = (i, active) => {
    index = clamp(i, 0, clean.length - 1);
    const p = clean[index];
    rValue.textContent = format(p.value);
    rValue.className = `chart-readout-value ${p.value >= 0 ? 'pos' : 'neg'}`;
    rDate.textContent = p.date;
    // Change since the start of the visible window: the number a reader
    // actually wants when they park the cursor somewhere.
    const delta = p.value - clean[0].value;
    rDelta.textContent = index === 0 ? '' : `${delta >= 0 ? '+' : '−'}${format(Math.abs(delta))} in window`;
    if (active) {
      cross.setAttribute('opacity', '1');
      vline.setAttribute('x1', x(index));
      vline.setAttribute('x2', x(index));
      dot.setAttribute('cx', x(index));
      dot.setAttribute('cy', y(p.value));
    } else {
      cross.setAttribute('opacity', '0');
    }
  };
  paint(clean.length - 1, false);

  // Returns null rather than 0 when the element has not been laid out — a
  // zero-width measurement is "we do not know where the pointer is", and
  // snapping the crosshair to the first point would be a confident wrong answer.
  const fromPointer = (clientX) => {
    const r = svg.getBoundingClientRect();
    if (!r.width) return null;
    return Math.round(((clientX - r.left) / r.width) * (clean.length - 1));
  };
  const track = (e) => {
    const i = fromPointer(e.clientX);
    if (i !== null) paint(i, true);
  };

  svg.addEventListener('pointermove', track);
  svg.addEventListener('pointerdown', track);
  svg.addEventListener('pointerleave', () => paint(clean.length - 1, false));

  // Keyboard: the same cursor, driven by arrows.
  svg.tabIndex = 0;
  svg.setAttribute('role', 'slider');
  svg.setAttribute('aria-valuemin', '0');
  svg.setAttribute('aria-valuemax', String(clean.length - 1));
  const announce = () => {
    svg.setAttribute('aria-valuenow', String(index));
    svg.setAttribute('aria-valuetext', `${clean[index].date}, ${format(clean[index].value)}`);
  };
  svg.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 7 : 1;
    if (e.key === 'ArrowRight') paint(index + step, true);
    else if (e.key === 'ArrowLeft') paint(index - step, true);
    else if (e.key === 'Home') paint(0, true);
    else if (e.key === 'End') paint(clean.length - 1, true);
    else return;
    e.preventDefault();
    announce();
  });
  svg.addEventListener('focus', () => { paint(index, true); announce(); });
  svg.addEventListener('blur', () => paint(clean.length - 1, false));

  return { node: wrap, focus: () => svg.focus() };
}

/* ---------------------------------------------------------------------------
 * Trading clock heat-map
 * ------------------------------------------------------------------------ */

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DAY_SHORT = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];

export const CLOCK_METRICS = [
  ['fills', 'Fills', (c) => c.buys + c.sells],
  ['buys', 'Buys', (c) => c.buys],
  ['sells', 'Sells', (c) => c.sells],
  ['usd', 'Notional', (c) => c.usd],
];

/**
 * 7×24 heat-map with hover, pinning, marginal totals and a metric switch.
 * @param {{buys:number,sells:number,usd:number}[][]} matrix  [day][hour]
 * @param {(v:number)=>string} money
 */
export function tradingClock(matrix, { money = (v) => String(v) } = {}) {
  const wrap = el('div', 'clockwrap');
  let metricKey = 'fills';
  let pinned = null;

  const controls = el('div', 'clock-controls');
  const seg = el('div', 'seg-inline');
  for (const [key, label] of CLOCK_METRICS) {
    const b = el('button', null, label);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(key === metricKey));
    b.onclick = () => {
      metricKey = key;
      [...seg.children].forEach((c) => c.setAttribute('aria-pressed', String(c === b)));
      draw();
    };
    seg.append(b);
  }
  controls.append(seg);
  const readout = el('div', 'clock-readout');
  controls.append(readout);
  wrap.append(controls);

  const grid = el('div', 'clock');
  const rowTotals = el('div', 'clock-rowtotals');
  const body = el('div', 'clock-body');
  body.append(grid, rowTotals);
  wrap.append(body);

  const hoursAxis = el('div', 'clock-x');
  wrap.append(hoursAxis);
  const colTotals = el('div', 'clock-coltotals');
  wrap.append(colTotals);

  wrap.append(el('p', 'chart-hint',
    'Hover a cell for its exact numbers; click to pin one. Arrow keys move the cursor.'));

  const cells = [];

  function valueOf(cell) {
    return CLOCK_METRICS.find(([k]) => k === metricKey)[2](cell);
  }
  const fmt = (v) => (metricKey === 'usd' ? money(v) : v.toLocaleString('en-US'));

  function describe(d, h) {
    const c = matrix[d][h];
    const total = c.buys + c.sells;
    if (!total) return `${DAYS[d]} ${String(h).padStart(2, '0')}:00 UTC — no fills`;
    return `${DAYS[d]} ${String(h).padStart(2, '0')}:00 UTC — ${total.toLocaleString('en-US')} fill${total === 1 ? '' : 's'}`
      + ` (${c.buys.toLocaleString('en-US')} buy / ${c.sells.toLocaleString('en-US')} sell), ${money(c.usd)}`;
  }

  function draw() {
    const flat = matrix.flat().map(valueOf);
    const peak = Math.max(...flat, 1);

    grid.replaceChildren();
    rowTotals.replaceChildren();
    cells.length = 0;

    for (let d = 0; d < 7; d++) {
      grid.append(el('div', 'clock-lab', DAY_SHORT[d]));
      let rowSum = 0;
      for (let h = 0; h < 24; h++) {
        const v = valueOf(matrix[d][h]);
        rowSum += v;
        const cell = el('button', 'clock-cell');
        cell.type = 'button';
        cell.dataset.d = d;
        cell.dataset.h = h;
        cell.tabIndex = d === 0 && h === 0 ? 0 : -1;
        // Intensity is on a square root so a single very busy hour does not
        // flatten every other hour to the same near-empty tint.
        const t = v > 0 ? Math.sqrt(v / peak) : 0;
        cell.style.setProperty('--t', t.toFixed(3));
        if (v === 0) cell.classList.add('is-empty');
        cell.setAttribute('aria-label', describe(d, h));
        cell.onpointerenter = () => show(d, h);
        cell.onfocus = () => show(d, h);
        cell.onclick = () => {
          pinned = pinned && pinned.d === d && pinned.h === h ? null : { d, h };
          highlight();
          show(d, h);
        };
        cell.onkeydown = (e) => move(e, d, h);
        grid.append(cell);
        cells.push(cell);
      }
      const rt = el('div', 'clock-total', rowSum ? fmt(rowSum) : '');
      rt.title = `${DAYS[d]} total`;
      rowTotals.append(rt);
    }

    // Hour axis and per-hour totals under the grid.
    hoursAxis.replaceChildren(el('span'));
    colTotals.replaceChildren(el('span'));
    const colMax = Math.max(...Array.from({ length: 24 }, (_, h) =>
      matrix.reduce((a, row) => a + valueOf(row[h]), 0)), 1);
    for (let h = 0; h < 24; h++) {
      hoursAxis.append(el('span', null, h % 3 === 0 ? String(h).padStart(2, '0') : ''));
      const sum = matrix.reduce((a, row) => a + valueOf(row[h]), 0);
      const bar = el('span', 'clock-colbar');
      const fill = el('i');
      fill.style.height = `${Math.round((sum / colMax) * 100)}%`;
      bar.append(fill);
      bar.title = `${String(h).padStart(2, '0')}:00 UTC — ${fmt(sum)}`;
      colTotals.append(bar);
    }

    highlight();
    show(pinned?.d ?? null, pinned?.h ?? null);
  }

  function highlight() {
    for (const c of cells) {
      const on = pinned && Number(c.dataset.d) === pinned.d && Number(c.dataset.h) === pinned.h;
      c.classList.toggle('is-pinned', Boolean(on));
    }
  }

  function show(d, h) {
    if (d == null) {
      const busiest = busiestCell();
      readout.textContent = busiest
        ? `Busiest: ${describe(busiest.d, busiest.h)}`
        : 'No fills on the loaded tape.';
      readout.classList.remove('is-pinned');
      return;
    }
    readout.textContent = describe(d, h);
    readout.classList.toggle('is-pinned', Boolean(pinned && pinned.d === d && pinned.h === h));
  }

  function busiestCell() {
    let best = null, bestV = 0;
    for (let d = 0; d < 7; d++) {
      for (let h = 0; h < 24; h++) {
        const v = matrix[d][h].buys + matrix[d][h].sells;
        if (v > bestV) { bestV = v; best = { d, h }; }
      }
    }
    return best;
  }

  function move(e, d, h) {
    const delta = { ArrowRight: [0, 1], ArrowLeft: [0, -1], ArrowDown: [1, 0], ArrowUp: [-1, 0] }[e.key];
    if (!delta) return;
    e.preventDefault();
    const nd = clamp(d + delta[0], 0, 6);
    const nh = clamp(h + delta[1], 0, 23);
    const next = cells[nd * 24 + nh];
    for (const c of cells) c.tabIndex = -1;
    next.tabIndex = 0;
    next.focus();
  }

  grid.addEventListener('pointerleave', () => show(pinned?.d ?? null, pinned?.h ?? null));

  draw();
  return { node: wrap };
}
