/* System Design page behaviour.
 *
 * Loaded by app.js after the fragment is injected. Three jobs, all optional —
 * the page is complete and readable if none of them run:
 *
 *   1. Reveal each diagram once as it enters the viewport (IntersectionObserver).
 *   2. Draw the connecting lines in, for lines that have no dash pattern of
 *      their own, so the dashed recovery arrows keep their meaning.
 *   3. Mark figures that actually overflow, so the scroll hint only appears
 *      where there is something to scroll to.
 *
 * Motion is gated on prefers-reduced-motion, and re-checked when the user
 * changes the setting mid-session. No animation loops, and none of it is tied
 * to live system state.
 */
const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';

function prefersReducedMotion() {
  return window.matchMedia?.(REDUCED_MOTION).matches ?? false;
}

/* Lines that already carry a dash pattern (async / recovery edges) are left
   alone — overwriting stroke-dasharray would silently change what they mean. */
function drawableLines(svg) {
  return [...svg.querySelectorAll('path[marker-end], line[marker-end]')].filter(
    (line) => !line.getAttribute('stroke-dasharray'),
  );
}

function prepareLineDrawing(svg) {
  for (const line of drawableLines(svg)) {
    let length = 0;
    try {
      length = line.getTotalLength();
    } catch {
      continue; // getTotalLength is unavailable for this node; leave it drawn.
    }
    if (!Number.isFinite(length) || length <= 0) continue;
    line.dataset.sdLength = String(length);
    line.style.strokeDasharray = `${length}`;
    line.style.strokeDashoffset = `${length}`;
  }
}

function revealFigure(figure, index) {
  if (figure.classList.contains('sd-revealed')) return;
  const svg = figure.querySelector('svg.diagram');
  figure.classList.add('sd-revealed');
  if (!svg) return;
  // Stagger the node fades a little so a sequence reads in order rather than
  // appearing all at once. Capped so a long diagram never feels slow.
  const nodes = svg.querySelectorAll('rect, circle, text, polygon');
  nodes.forEach((node, position) => {
    node.style.setProperty('--sd-delay', `${Math.min(position * 6, 420)}ms`);
  });
  drawableLines(svg).forEach((line, position) => {
    const length = line.dataset.sdLength;
    if (!length) return;
    line.style.transition = `stroke-dashoffset .9s cubic-bezier(.22,.61,.36,1) ${
      Math.min(120 + position * 70, 700)}ms`;
    line.style.strokeDashoffset = '0';
  });
  void index;
}

function showFinished(figure) {
  const svg = figure.querySelector('svg.diagram');
  figure.classList.add('sd-revealed');
  if (!svg) return;
  for (const line of svg.querySelectorAll('path[marker-end], line[marker-end]')) {
    if (!line.dataset.sdLength) continue;
    line.style.strokeDasharray = '';
    line.style.strokeDashoffset = '';
    line.style.transition = '';
  }
}

function markScrollableFigures(figures) {
  const sync = () => {
    for (const figure of figures) {
      figure.classList.toggle(
        'is-scrollable',
        figure.scrollWidth > figure.clientWidth + 1,
      );
    }
  };
  sync();
  window.addEventListener('resize', sync, { passive: true });
}

export function setupSystemDesign(root) {
  const figures = [...root.querySelectorAll('.sd-figure')];
  if (!figures.length) return;

  markScrollableFigures(figures);

  const still = prefersReducedMotion() || !('IntersectionObserver' in window);
  if (still) {
    figures.forEach(showFinished);
    return;
  }

  // Order matters. `sd-animate` and the inline dash offsets are what hide a
  // diagram before its reveal, so they are applied LAST — only once the
  // observer exists and is watching every figure. If anything above throws,
  // the page is left in its finished, fully readable state rather than blank.
  let observer;
  try {
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          revealFigure(entry.target, figures.indexOf(entry.target));
          observer.unobserve(entry.target);
        }
      },
      { threshold: 0.18, rootMargin: '0px 0px -8% 0px' },
    );
    figures.forEach((figure) => observer.observe(figure));
    figures.forEach((figure) => {
      const svg = figure.querySelector('svg.diagram');
      if (svg) prepareLineDrawing(svg);
    });
    root.classList.add('sd-animate');
  } catch {
    observer?.disconnect();
    root.classList.remove('sd-animate');
    figures.forEach(showFinished);
    return;
  }

  // Failsafe. The whole page is diagrams, and an unrevealed diagram is a blank
  // box, so never let a throttled or missed observer callback cost the reader
  // the content: shortly after load, force-reveal anything already on screen.
  // Figures still below the fold keep their normal scroll-triggered reveal.
  setTimeout(() => {
    for (const figure of figures) {
      if (figure.classList.contains('sd-revealed')) continue;
      const box = figure.getBoundingClientRect();
      if (box.top < window.innerHeight && box.bottom > 0) {
        observer.unobserve(figure);
        showFinished(figure);
      }
    }
  }, 1500);

  // If the reader turns reduced-motion on mid-visit, stop animating and show
  // every diagram in its finished state immediately.
  window.matchMedia?.(REDUCED_MOTION).addEventListener?.('change', (event) => {
    if (!event.matches) return;
    observer.disconnect();
    root.classList.remove('sd-animate');
    figures.forEach(showFinished);
  });
}
