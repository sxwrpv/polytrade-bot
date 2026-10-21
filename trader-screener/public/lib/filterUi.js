/* Numeric filters as histogram sliders.
 *
 * Each filter shows where the board's wallets sit on its scale: one bar per
 * slider step, the bars it keeps in green, so a threshold is chosen by looking
 * at the wallets. Thresholds come from a ladder (FILTER_SPECS in
 * screenerModel.js), so "$10K" and "$1M" are each one step from their
 * neighbours and both ends of the range stay in reach.
 *
 * The control is built once and updated in place. Replacing the input while it
 * is being dragged ends the drag, which is how the old sliders lost the pointer
 * after a single step.
 */
import { histogram, positionValue, stopPosition } from './screenerModel.js';

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

/**
 * @param {object} o
 * @param {string} o.key        filter key in the board state ('volumeMin' …)
 * @param {'min'|'max'} o.cut   a floor keeps bars to the right, a ceiling to the left
 * @param {(raw: string) => void} o.onInput  receives the new threshold, '' for off
 */
export function histogramFilter({ key, cut = 'min', onInput }) {
  const id = `f-${key}`;
  const node = el('div', `hfilter${cut === 'max' ? ' is-ceiling' : ''}`);

  const head = el('div', 'hfilter-head');
  const label = el('label');
  label.htmlFor = id;
  const output = el('output');
  output.setAttribute('for', id);
  head.append(label, output);

  const plot = el('div', 'hfilter-plot');
  const bars = el('div', 'hfilter-bars');
  bars.setAttribute('aria-hidden', 'true');
  const input = document.createElement('input');
  input.type = 'range';
  input.id = id;
  input.min = '0';
  input.step = '1';
  plot.append(bars, input);

  const scale = el('div', 'hfilter-scale');
  scale.setAttribute('aria-hidden', 'true');
  const lo = el('span');
  const hi = el('span');
  scale.append(lo, hi);
  node.append(head, plot, scale);

  let stops = [];
  let format = String;
  let grown = false;
  let heights = [];
  let endBin = 0;   // wallets in the open-ended bin folded into the end bar

  const isOff = (position) => (cut === 'max' ? position >= stops.length : position <= 0);

  function paint(position, raw) {
    // Bar i spans the step between handle positions i and i+1, so the handle
    // always sits on the boundary where the kept bars begin (or end). At the
    // extreme step the kept wallets live in the folded end bin, so that bar
    // stays lit while it still holds someone.
    const last = bars.children.length - 1;
    [...bars.children].forEach((bar, i) => {
      const kept = cut === 'max'
        ? i < position || (position === 0 && i === 0 && endBin > 0)
        : i >= position || (position === stops.length && i === last && endBin > 0);
      bar.classList.toggle('in', kept);
    });
    const off = isOff(position);
    const sign = cut === 'max' ? '≤' : '≥';
    node.classList.toggle('is-active', !off);
    output.textContent = off ? 'Any' : `${sign} ${format(Number(raw))}`;
    input.setAttribute('aria-valuetext', off ? 'Any' : `${cut === 'max' ? 'at most' : 'at least'} ${format(Number(raw))}`);
    input.style.setProperty('--pos', String(stops.length ? position / stops.length : 0));
  }

  input.addEventListener('input', () => {
    const position = Number(input.value);
    const raw = positionValue(stops, position, cut);
    paint(position, raw);
    onInput(raw);
  });
  // Scrolling the page must never drag a filter.
  input.addEventListener('wheel', () => input.blur(), { passive: true });

  /**
   * @param {object} o
   * @param {string} o.label     visible label, e.g. "Volume 7D at least"
   * @param {(v: number) => string} o.format
   * @param {number[]} o.stops   thresholds, ascending
   * @param {number[]} o.values  every wallet's figure in the window
   * @param {string} o.value     the stored threshold, '' when off
   */
  function update({ label: text, format: fmt, stops: next, values, value }) {
    label.textContent = text;
    format = fmt;
    stops = next;
    input.max = String(stops.length);

    // One bin per position from the model, folded to one bar per step between
    // positions: the open-ended bin at the far end shares the outermost bar.
    const bins = histogram(values, stops, cut);
    endBin = cut === 'max' ? bins[0] : bins.at(-1);
    const counts = cut === 'max'
      ? [bins[0] + bins[1], ...bins.slice(2)]
      : [...bins.slice(0, -2), bins.at(-2) + bins.at(-1)];
    const peak = Math.max(1, ...counts);
    while (bars.children.length < counts.length) bars.append(el('i'));
    while (bars.children.length > counts.length) bars.lastElementChild.remove();
    // Square-root heights keep a bar of three wallets visible next to one of
    // three hundred; the exact count is in the filter header.
    heights = counts.map((c) => (c ? Math.max(0.1, Math.sqrt(c / peak)) : 0));
    [...bars.children].forEach((bar, i) => {
      bar.style.setProperty('--i', String(i));
      if (grown) bar.style.setProperty('--h', String(heights[i]));
    });
    if (!grown) {
      // Bars grow in a wave the first time (the kit's bar-chart entrance),
      // from whatever heights are current by the time the frame comes.
      grown = true;
      requestAnimationFrame(() => requestAnimationFrame(() => {
        [...bars.children].forEach((bar, i) => bar.style.setProperty('--h', String(heights[i] ?? 0)));
      }));
    }

    const position = stopPosition(stops, value, cut);
    input.value = String(position);
    lo.textContent = cut === 'max' ? format(stops[0]) : 'Any';
    // "$50M+" reads as "and up"; a signed score already carries its sign.
    const top = format(stops.at(-1));
    hi.textContent = cut === 'max' ? 'Any' : /^[+−-]/.test(top) ? top : `${top}+`;
    paint(position, value);
  }

  return { node, update };
}
