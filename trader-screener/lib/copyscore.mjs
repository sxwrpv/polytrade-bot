// Copy Score — the trader-level verdict, reproduced from the shipped client bundle.
//
// Inferred from the published dataset: the class cut-points (strong >= 10,
// marginal >= 0) and the shrinkage constant — both are marked INFERRED below
// because they are not stated anywhere public.

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
