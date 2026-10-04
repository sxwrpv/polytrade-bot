// Copy Score — the trader-level verdict, reproduced from the shipped client bundle.
//
// Verbatim from the source: the class taxonomy, chip text, tone mapping and the
// hue-scale formula. Inferred from the published dataset: the class cut-points
// (strong >= 10, marginal >= 0) and the shrinkage constant — both are marked
// INFERRED below because they are not stated anywhere public.

export const CLASS_DEF = {
  strong: { chip: 'Proven', line: 'Copying them has worked on their finished trades.', tone: 'clear', bot: 'allow' },
  marginal: { chip: 'Ahead', line: 'They finish ahead of what copying costs — but only just.', tone: 'ahead', bot: 'allow_caution' },
  uneconomic: { chip: 'Caution', line: "They make money. It doesn't survive being copied.", tone: 'negative', bot: 'refuse' },
  loss_making: { chip: 'Losing', line: "They've been losing on their own trades, before copying costs even start.", tone: 'negative', bot: 'warn' },
  not_measurable: { chip: 'Not comparable', line: 'They close out early, so we can warn about them but never rank them alongside hold-to-the-end traders.', tone: 'neutral', bot: 'grey' },
  unproven: { chip: 'Thin history', line: 'Too few finished trades to stand behind. A big return either way can still be one bet that came in.', tone: 'unknown', bot: 'refuse' },
};
export const NONE_DEF = {
  chip: 'Not scored',
  line: 'The classifier has not graded this wallet. That is not a middling score; it is the absence of one.',
  tone: 'unknown', bot: 'refuse',
};

export const CLASS_ORDER = ['strong', 'marginal', 'uneconomic', 'loss_making', 'not_measurable', 'unproven', 'none'];
export const RECOMMENDED = new Set(['strong', 'marginal']);
const NUMERIC_CLASSES = new Set(['strong', 'marginal', 'uneconomic', 'loss_making']);

export const TYPICAL_TRADER_NET = -2.05; // cohort baseline shown as "typical trader −2.0%"

export const COPY = {
  subtitle: 'A read on the trader, not on this trade.',
  headline: "Great traders aren't always great to copy. Copy Score tells you which ones are.",
  basis: 'Based on their finished trades to date, not a forecast. Assumes every one of their trades was copied exactly — your filters, budget and timing will change it.',
  what: "Copying isn't free — you buy after they do, and there are fees. Copy Score is what was left on their finished trades after that. The cost is much the same whoever you copy; what changes is whether their edge clears it.",
  how: 'It reads their whole finished record, then takes out the spread you pay for filling after them and the fees on the way in. Judge the trader with it, then judge this trade yourself.',
  boardContext: 'Copy Score is what puts them on this board and how high; the money column beside it only separates traders who scored the same. It asks whether following them has historically left anything after costs.',
  title: 'Traders worth copying',
};

export const known = (k) => !!k && k in CLASS_DEF;
export const normalize = (k) => (known(k) ? k : 'none');
export const def = (k) => (known(k) ? CLASS_DEF[k] : NONE_DEF);
export const rank = (k) => { const i = CLASS_ORDER.indexOf(normalize(k)); return i < 0 ? CLASS_ORDER.length : i; };
export const isNumeric = (k, v) => known(k) && NUMERIC_CLASSES.has(k) && v != null && Number.isFinite(v);

/** Signed integer form used on the trader page and trending cards: +6, −4, 0. */
export function signed(v) {
  if (v == null || !Number.isFinite(v)) return null;
  const n = Math.round(v);
  if (n === 0) return '0';
  return `${n > 0 ? '+' : '−'}${Math.abs(n)}`;
}
/** Magnitude-percent form used in the board's Copy Score column: 11%, −4%. */
export function magnitude(v) {
  if (v == null || !Number.isFinite(v)) return null;
  const n = Math.round(Math.abs(v));
  return `${v < 0 && n !== 0 ? '−' : ''}${n}%`;
}
export function pct1(v) {
  if (v == null || !Number.isFinite(v)) return null;
  const n = Math.round(Math.abs(v) * 10) / 10;
  return n === 0 ? '0.0%' : `${v < 0 ? '−' : '+'}${n.toFixed(1)}%`;
}

/** The continuous hue ramp behind the chip: −8 → red, +16 → green. */
export function scale(v) {
  const hue = 8 + 137 * Math.max(0, Math.min(1, (v + 8) / 24));
  return {
    background: `hsla(${hue}, 70%, 93%, 1)`,
    color: `hsl(${hue}, 72%, 26%)`,
    borderColor: `hsla(${hue}, 50%, 62%, 0.55)`,
  };
}

export function chip(copyClass, netReturnPct) {
  const d = def(copyClass);
  const numeric = isNumeric(copyClass, netReturnPct);
  return {
    key: normalize(copyClass),
    def: d,
    numeric,
    signed: numeric ? signed(netReturnPct) : '—',
    magnitude: numeric ? magnitude(netReturnPct) : '—',
    scale: numeric ? scale(netReturnPct) : null,
    ariaLabel: `Copy Score: ${numeric ? `${signed(netReturnPct)}. ${d.chip}` : d.chip}. ${COPY.subtitle}`,
  };
}

// ---------------------------------------------------------------------------
// Local recomputation, for wallets outside the cached cohort.
// ---------------------------------------------------------------------------

export const COST_MODEL = {
  // Cost of filling *after* the trader, as a share of the entry price.
  spreadPctOfEntry: 1.48,
  // Venue fees plus the platform's own 0.5%, as a share of the entry price.
  feesPctOfEntry: 3.14,
  // INFERRED. Empirical-Bayes shrink: shrunk = gross * n / (n + PRIOR).
  // Solved from a published wallet (gross 81.37 %, 6 resolved -> shrunk 2.58 %).
  shrinkPrior: 183,
  // Below this many finished trades the verdict is withheld entirely.
  minResolved: 5,
  // Above this share of positions exited before resolution the number is not
  // measuring the same thing, so the trader is graded "not comparable".
  soldBeforeResolutionCeiling: 0.5,
};

/**
 * Recompute Copy Score from a wallet's own finished record.
 * @param {{grossReturnPct:number, resolved:number, avgEntryPrice:number,
 *          soldBeforeResolution:number}} input
 */
export function compute({ grossReturnPct, resolved, avgEntryPrice, soldBeforeResolution }, model = COST_MODEL) {
  const out = {
    version: 'copy_v2_local', unit: 'net_return_per_trade',
    resolved, grossReturnPct, avgEntryPrice,
    costSpreadPct: model.spreadPctOfEntry,
    costFeesPct: model.feesPctOfEntry,
    costPct: model.spreadPctOfEntry + model.feesPctOfEntry,
    shrunkReturnPct: null, netReturnPct: null,
    marginCents: null, lineCents: null,
    thinHistory: resolved < 20, class: null, status: 'ok',
  };
  if (!resolved || resolved < model.minResolved || !Number.isFinite(grossReturnPct)) {
    out.class = 'unproven';
    out.status = 'thin';
    return out;
  }
  const shrunk = grossReturnPct * (resolved / (resolved + model.shrinkPrior));
  const net = shrunk - out.costPct;
  out.shrunkReturnPct = round2(shrunk);
  out.netReturnPct = round2(net);
  if (Number.isFinite(avgEntryPrice)) {
    out.marginCents = round2((grossReturnPct / 100) * avgEntryPrice * 100);
    out.lineCents = round2((out.costPct / 100) * avgEntryPrice * 100);
  }
  if (soldBeforeResolution != null && soldBeforeResolution > model.soldBeforeResolutionCeiling) {
    out.class = 'not_measurable';
  } else if (grossReturnPct < 0) {
    out.class = 'loss_making';
  } else if (net >= 10) {
    out.class = 'strong';
  } else if (net >= 0) {
    out.class = 'marginal';
  } else {
    out.class = 'uneconomic';
  }
  return out;
}

const round2 = (v) => (Number.isFinite(v) ? Math.round(v * 100) / 100 : null);
