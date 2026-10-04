/* Wallet metrics, reconstructed from Polymarket's public account tape.
 *
 * The tape is /activity: every fill, redemption, split, merge, reward and
 * rebate, with a USDC amount on each. /positions supplies what is still held.
 * Nothing else is available without an indexer, and where the tape cannot
 * answer a question this module returns null rather than a plausible number.
 *
 * The central construct is a MARKET EPISODE: everything the wallet did in one
 * conditionId. An episode is FINISHED when nothing is still held in it, and a
 * finished episode is what "resolved trade" means everywhere below —
 * including in Copy Score, whose whole claim is about finished records.
 */

import * as cs from './copyscore.mjs';

const DAY = 86400;
const n = (v) => (Number.isFinite(v) ? v : 0);
const r2 = (v) => (Number.isFinite(v) ? Math.round(v * 100) / 100 : null);
const dayKey = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);

/** Rewards and rebates are income the wallet earns that a copier does not. */
const REBATE_TYPES = new Set(['REWARD', 'MAKER_REBATE', 'TAKER_REBATE', 'YIELD']);
/** Inventory that arrives without a market price, so it distorts return math. */
const INVENTORY_TYPES = new Set(['SPLIT', 'MERGE', 'CONVERSION']);

// --- classification --------------------------------------------------------
// The public tape carries no category field, so niche and bet type are read
// off the event slug and market title. This is a classification, not a label
// from the venue, and the UI says so.

const NICHE_RULES = [
  [/\b(nfl|super-?bowl)\b/, 'NFL', 'Sports'],
  [/\b(nba|nbafinals)\b/, 'NBA', 'Sports'],
  [/\b(mlb|world-?series)\b/, 'MLB', 'Sports'],
  [/\b(nhl|stanley)\b/, 'NHL', 'Sports'],
  [/\b(ucl|uel|epl|lal|seri[ea]|bun|lig|mls|fifa|soccer|world-?cup|ecs)\b/, 'SOCCER', 'Sports'],
  [/\b(atp|wta|tennis|wimbledon|us-?open|aus-?open)\b/, 'TENNIS', 'Sports'],
  [/\b(ufc|mma|boxing)\b/, 'MMA', 'Sports'],
  [/\b(cricket|ipl|t20)\b/, 'CRICKET', 'Sports'],
  [/\b(golf|pga|masters)\b/, 'GOLF', 'Sports'],
  [/\b(f1|formula-?1|grand-prix)\b/, 'FORMULA_1', 'Sports'],
  [/\b(csgo|dota|lol|valorant|esports)\b/, 'ESPORTS', 'Sports'],
  [/\b(bitcoin|btc)\b/, 'BITCOIN', 'Crypto'],
  [/\b(ethereum|eth)\b/, 'ETHEREUM', 'Crypto'],
  [/\b(solana|sol)\b/, 'SOLANA', 'Crypto'],
  [/\b(crypto|xrp|doge|token|coin)\b/, 'CRYPTO', 'Crypto'],
  [/\b(election|president|senate|governor|congress|primary|nominee)\b/, 'US_ELECTION', 'Politics'],
  [/\b(trump|biden|harris|politic|parliament|cabinet|impeach)\b/, 'POLITICS', 'Politics'],
  [/\b(israel|gaza|iran|hezbollah|middle-?east|syria|lebanon)\b/, 'MIDDLE_EAST', 'World'],
  [/\b(ukraine|russia|putin|zelensk)\b/, 'RUSSIA_UKRAINE', 'World'],
  [/\b(china|taiwan|xi-jinping)\b/, 'CHINA_TAIWAN', 'World'],
  [/\b(fed|cpi|inflation|rate-?cut|gdp|recession|jobs-?report|unemployment|wti|crude|oil)\b/, 'MACRO', 'Economy'],
  [/\b(earnings|revenue|ipo|stock|nasdaq|s-and-p)\b/, 'BUSINESS', 'Economy'],
  [/\b(elon|musk|tesla)\b/, 'ELON_MUSK', 'Tech'],
  [/\b(openai|gpt|llm|apple|google|twitter|x-com)\b/, 'TECH', 'Tech'],
  [/\b(temperature|rain|snow|hurricane|weather|storm)\b/, 'WEATHER', 'Weather'],
  [/\b(oscar|grammy|emmy|movie|album|celebrity|nobel|time-person)\b/, 'CULTURE', 'Culture'],
  [/\b(space|spacex|nasa|launch|science)\b/, 'SCIENCE', 'Tech'],
];

export function classifyNiche(row) {
  const hay = `${row.eventSlug || ''} ${row.slug || ''} ${row.title || ''}`.toLowerCase();
  for (const [re, niche, group] of NICHE_RULES) if (re.test(hay)) return { niche, group };
  return { niche: null, group: null };
}

export function classifyStructure(row) {
  const hay = `${row.title || ''} ${row.slug || ''}`.toLowerCase();
  if (/o\/u|over\/under|-total-|\btotal \d/.test(hay)) return { key: 'totals', label: 'Over / under' };
  if (/spread|handicap|by \d+\+? (points|goals|runs)/.test(hay)) return { key: 'spread', label: 'Spread' };
  if (/ vs\.? |moneyline|win on \d{4}-\d{2}-\d{2}/.test(hay)) return { key: 'moneyline', label: 'Moneyline' };
  if (/^will\b/.test((row.title || '').toLowerCase())) return { key: 'prop', label: 'Props' };
  return { key: 'binary', label: 'Yes / no' };
}

// --- helpers ---------------------------------------------------------------

function quantile(sorted, q) {
  if (!sorted.length) return null;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos), hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

function isoWeek(dateStr) {
  const d = new Date(`${dateStr}T00:00:00Z`);
  const day = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - day + 3);
  const firstThursday = new Date(Date.UTC(d.getUTCFullYear(), 0, 4));
  const f = (firstThursday.getUTCDay() + 6) % 7;
  firstThursday.setUTCDate(firstThursday.getUTCDate() - f + 3);
  return `${d.getUTCFullYear()}-W${String(1 + Math.round((d - firstThursday) / (7 * 864e5))).padStart(2, '0')}`;
}

// --- episodes ---------------------------------------------------------------

/**
 * Group the tape by market. One episode per conditionId, holding the fills,
 * the redemption proceeds, and whether anything is still held there.
 */
export function buildEpisodes(tape, openPositions) {
  const stillOpen = new Set(openPositions.map((p) => p.conditionId));
  const episodes = new Map();

  for (const row of tape) {
    if (!row.conditionId) continue;
    let ep = episodes.get(row.conditionId);
    if (!ep) {
      ep = {
        conditionId: row.conditionId,
        title: row.title, slug: row.slug, eventSlug: row.eventSlug, icon: row.icon,
        outcome: row.outcome, outcomeIndex: row.outcomeIndex,
        buyUsd: 0, sellUsd: 0, redeemUsd: 0,
        buyShares: 0, sellShares: 0,
        fills: 0, buyFills: 0, sellFills: 0,
        outcomes: new Set(),
        inventoryUsd: 0,
        firstTs: row.timestamp, lastTs: row.timestamp,
      };
      episodes.set(row.conditionId, ep);
    }
    ep.firstTs = Math.min(ep.firstTs, row.timestamp);
    ep.lastTs = Math.max(ep.lastTs, row.timestamp);

    if (row.type === 'TRADE') {
      ep.fills += 1;
      ep.outcomes.add(row.outcomeIndex);
      if (row.side === 'SELL') {
        ep.sellUsd += n(row.usdcSize); ep.sellShares += n(row.size); ep.sellFills += 1;
      } else {
        ep.buyUsd += n(row.usdcSize); ep.buyShares += n(row.size); ep.buyFills += 1;
        // Keep the first title/outcome seen on a buy: it names the side taken.
        ep.title ||= row.title; ep.outcome ||= row.outcome;
      }
    } else if (row.type === 'REDEEM') {
      ep.redeemUsd += n(row.usdcSize);
    } else if (INVENTORY_TYPES.has(row.type)) {
      ep.inventoryUsd += n(row.usdcSize);
    }
  }

  for (const ep of episodes.values()) {
    ep.held = stillOpen.has(ep.conditionId);
    ep.finished = !ep.held && ep.buyUsd > 0;
    ep.proceedsUsd = ep.sellUsd + ep.redeemUsd;
    ep.pnl = ep.proceedsUsd - ep.buyUsd;
    ep.stake = ep.buyUsd;
    ep.avgEntry = ep.buyShares > 0 ? ep.buyUsd / ep.buyShares : null;
    ep.exitedBySelling = ep.sellShares > 0;
    // An episode whose inventory arrived off-market is not a clean read on
    // trading skill, so it is excluded from the return that Copy Score uses.
    ep.priceable = ep.inventoryUsd === 0;
  }
  return [...episodes.values()];
}

// ---------------------------------------------------------------------------

export function buildProfile({ wallet, tape, open, rankRows, value, cash }) {
  if (open.complete === false) {
    throw new Error('current positions are incomplete; resolved metrics and Copy Score are unavailable');
  }
  const complete = tape.complete !== false;
  const fills = tape.filter((r) => r.type === 'TRADE');
  const rebates = tape.filter((r) => REBATE_TYPES.has(r.type));
  const buys = fills.filter((f) => f.side === 'BUY');
  const sells = fills.filter((f) => f.side === 'SELL');

  const buyNotional = buys.reduce((a, f) => a + n(f.usdcSize), 0);
  const sellNotional = sells.reduce((a, f) => a + n(f.usdcSize), 0);
  const volume = buyNotional + sellNotional;
  const rebateUsd = rebates.reduce((a, r) => a + n(r.usdcSize), 0);

  const episodes = buildEpisodes(tape, open);
  const finished = episodes.filter((e) => e.finished);
  const heldEpisodes = episodes.filter((e) => e.held);

  const settledPnl = finished.reduce((a, e) => a + e.pnl, 0);
  const openValue = open.reduce((a, p) => a + n(p.currentValue), 0);
  const openPnl = open.reduce((a, p) => a + n(p.cashPnl), 0);
  const totalPnl = settledPnl + openPnl;

  const wins = finished.filter((e) => e.pnl > 0);
  const losses = finished.filter((e) => e.pnl < 0);
  const winRate = finished.length ? wins.length / finished.length : null;
  const grossWin = wins.reduce((a, e) => a + e.pnl, 0);
  const grossLoss = Math.abs(losses.reduce((a, e) => a + e.pnl, 0));
  const profitFactor = grossLoss > 0 ? grossWin / grossLoss : null;

  const avgEntryPrice = buyNotional > 0
    ? buys.reduce((a, f) => a + n(f.price) * n(f.usdcSize), 0) / buyNotional : null;

  // --- daily series -------------------------------------------------------
  const byDay = new Map();
  const touch = (k) => {
    let d = byDay.get(k);
    if (!d) { d = { date: k, volume: 0, trades: 0, pnl: 0 }; byDay.set(k, d); }
    return d;
  };
  for (const f of fills) {
    const d = touch(dayKey(f.timestamp));
    d.volume += n(f.usdcSize);
    d.trades += 1;
  }
  // Settled P&L lands on the day the market's last activity happened.
  for (const e of finished) touch(dayKey(e.lastTs)).pnl += e.pnl;

  const series = [...byDay.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
  let cum = 0, peak = 0, maxDrawdown = 0;
  for (const d of series) {
    cum += d.pnl;
    d.cumPnl = r2(cum);
    peak = Math.max(peak, cum);
    d.drawdown = r2(cum - peak);
    maxDrawdown = Math.min(maxDrawdown, cum - peak);
  }

  const pnlDays = series.filter((d) => d.pnl !== 0);
  const dayOutcomes = {
    positive: series.filter((d) => d.pnl > 0).length,
    negative: series.filter((d) => d.pnl < 0).length,
    flat: series.filter((d) => d.pnl === 0).length,
    avgDaily: pnlDays.length ? r2(pnlDays.reduce((a, d) => a + d.pnl, 0) / pnlDays.length) : null,
    maxDrawdown: r2(maxDrawdown),
  };

  const weeks = new Map();
  for (const d of series) weeks.set(isoWeek(d.date), (weeks.get(isoWeek(d.date)) || 0) + d.pnl);
  const weekVals = [...weeks.values()].filter((v) => v !== 0);
  const profitableWeeks = weekVals.length ? weekVals.filter((v) => v > 0).length / weekVals.length : null;

  // --- order sizing --------------------------------------------------------
  const bands = [
    ['< $100', (v) => v < 100],
    ['$100 – $1K', (v) => v >= 100 && v < 1000],
    ['$1K – $10K', (v) => v >= 1000 && v < 10000],
    ['$10K+', (v) => v >= 10000],
  ];
  const stakes = finished.length ? finished.map((e) => e.stake) : buys.map((f) => n(f.usdcSize));
  const orderSizing = bands.map(([label, test]) => ({
    label,
    share: stakes.length ? stakes.filter(test).length / stakes.length : 0,
    n: stakes.filter(test).length,
  }));

  // --- hold time: FIFO match a SELL against the oldest open lot ------------
  const lots = new Map();
  const holdHours = [];
  for (const f of [...fills].sort((a, b) => a.timestamp - b.timestamp)) {
    const q = lots.get(f.asset) || [];
    if (f.side === 'BUY') {
      q.push({ ts: f.timestamp, size: n(f.size) });
      lots.set(f.asset, q);
    } else {
      let remaining = n(f.size);
      while (remaining > 0 && q.length) {
        const lot = q[0];
        const take = Math.min(lot.size, remaining);
        holdHours.push((f.timestamp - lot.ts) / 3600);
        lot.size -= take;
        remaining -= take;
        if (lot.size <= 1e-9) q.shift();
      }
    }
  }
  const holdSorted = holdHours.slice().sort((a, b) => a - b);

  // Two different readings of "they close out early", and they answer
  // different questions. The count says how often it happens. The value share
  // says how much of what they made came from an exit a copier holding to
  // resolution would not have mirrored — that is the one Copy Score gates on,
  // because the more of the return that arrives by selling, the less of it
  // reaches the copier.
  const exitedBySelling = finished.filter((e) => e.exitedBySelling).length;
  const soldEpisodeShare = finished.length ? exitedBySelling / finished.length : 0;
  const finishedProceeds = finished.reduce((a, e) => a + e.proceedsUsd, 0);
  const soldValueShare = finishedProceeds > 0
    ? finished.reduce((a, e) => a + e.sellUsd, 0) / finishedProceeds : 0;
  const soldBeforeResolution = soldValueShare;

  // --- price brackets ------------------------------------------------------
  const bracketDefs = [['< 20¢', 0, 0.2], ['20–50¢', 0.2, 0.5], ['50–80¢', 0.5, 0.8], ['80¢+', 0.8, 1.01]];
  const priceBrackets = bracketDefs.map(([label, lo, hi]) => {
    const rows = finished.filter((e) => e.avgEntry != null && e.avgEntry >= lo && e.avgEntry < hi);
    return {
      label, n: rows.length,
      pnl: r2(rows.reduce((a, e) => a + e.pnl, 0)),
      winRate: rows.length ? rows.filter((e) => e.pnl > 0).length / rows.length : null,
    };
  }).filter((b) => b.n > 0);

  // --- trading clock -------------------------------------------------------
  const clock = Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => ({ buys: 0, sells: 0, usd: 0 })));
  for (const f of fills) {
    const d = new Date(f.timestamp * 1000);
    const cell = clock[d.getUTCDay()][d.getUTCHours()];
    if (f.side === 'SELL') cell.sells += 1; else cell.buys += 1;
    cell.usd += n(f.usdcSize);
  }

  // --- niches and bet types ------------------------------------------------
  const nicheAgg = new Map(), typeAgg = new Map();
  for (const e of finished) {
    const { niche, group } = classifyNiche(e);
    const st = classifyStructure(e);
    if (niche) {
      const x = nicheAgg.get(niche) || { key: niche, label: niche.replace(/_/g, ' '), group, pnl: 0, bets: 0, wins: 0, stake: 0 };
      x.pnl += e.pnl; x.bets += 1; x.wins += e.pnl > 0 ? 1 : 0; x.stake += e.stake;
      nicheAgg.set(niche, x);
    }
    const t = typeAgg.get(st.key) || { key: st.key, label: st.label, pnl: 0, bets: 0, wins: 0, stake: 0 };
    t.pnl += e.pnl; t.bets += 1; t.wins += e.pnl > 0 ? 1 : 0; t.stake += e.stake;
    typeAgg.set(st.key, t);
  }
  const finalise = (m) => [...m.values()].map((x) => ({
    ...x,
    pnl: r2(x.pnl),
    winRate: x.bets ? x.wins / x.bets : null,
    roi: x.stake > 0 ? r2((x.pnl / x.stake) * 100) : null,
    share: finished.length ? x.bets / finished.length : null,
  })).sort((a, b) => b.pnl - a.pnl);

  const hedged = episodes.filter((e) => e.outcomes.size > 1).length;

  // --- Copy Score ----------------------------------------------------------
  // Only priceable episodes count: inventory that arrived through a split,
  // merge or conversion has no entry price, so including it would overstate
  // what a copier buying at market could have earned.
  const scored = finished.filter((e) => e.priceable && e.stake > 0);
  const scoredStake = scored.reduce((a, e) => a + e.stake, 0);
  const scoredPnl = scored.reduce((a, e) => a + e.pnl, 0);
  const grossReturnPct = scoredStake > 0 ? (scoredPnl / scoredStake) * 100 : null;
  const scoredEntry = scored.length
    ? scored.reduce((a, e) => a + (e.avgEntry ?? 0) * e.stake, 0) / scoredStake : avgEntryPrice;

  const copyScore = cs.compute({
    grossReturnPct,
    resolved: scored.length,
    avgEntryPrice: scoredEntry,
    soldBeforeResolution,
  });
  copyScore.rebateShare = scoredPnl > 0 ? r2((rebateUsd / scoredPnl) * 100) : null;

  // --- windows -------------------------------------------------------------
  const now = Math.floor(Date.now() / 1000);
  const windows = {};
  for (const [key, days] of [['d7', 7], ['d30', 30], ['d90', 90], ['all', null]]) {
    const from = days ? now - days * DAY : 0;
    const wf = fills.filter((f) => f.timestamp >= from);
    const we = finished.filter((e) => e.lastTs >= from);
    const vol = wf.reduce((a, f) => a + n(f.usdcSize), 0);
    const pnl = we.reduce((a, e) => a + e.pnl, 0) + (days ? 0 : openPnl);
    windows[key] = {
      pnl: r2(pnl), volume: r2(vol), trades: wf.length,
      roi: vol > 0 ? r2((pnl / vol) * 100) : null,
      winRate: we.length ? we.filter((e) => e.pnl > 0).length / we.length : null,
      resolved: we.length,
    };
  }

  const timestamps = fills.map((f) => f.timestamp);
  const lastFill = timestamps.length ? Math.max(...timestamps) : null;
  const firstFill = timestamps.length ? Math.min(...timestamps) : null;

  return {
    wallet,
    tapeComplete: complete,
    profile: {
      wallet,
      displayName: rankRows?.name || fills[0]?.name || null,
      pseudonym: fills[0]?.pseudonym || null,
      profileImage: rankRows?.image || fills[0]?.profileImage || null,
      bio: rankRows?.bio || fills[0]?.bio || null,
      firstTradeAt: firstFill ? new Date(firstFill * 1000).toISOString() : null,
    },
    summary: {
      pnl: r2(totalPnl),
      settledPnl: r2(settledPnl),
      openPnl: r2(openPnl),
      volume: r2(volume),
      buyNotional: r2(buyNotional),
      sellNotional: r2(sellNotional),
      trades: fills.length,
      avgTradeSize: fills.length ? r2(volume / fills.length) : null,
      winRate,
      resolved: finished.length,
      markets: episodes.length,
      roi: volume > 0 ? r2((totalPnl / volume) * 100) : null,
      profitFactor: profitFactor == null ? null : r2(profitFactor),
      openValue: r2(openValue),
      openMarkets: heldEpisodes.length || open.length,
      lastTradeAt: lastFill ? new Date(lastFill * 1000).toISOString() : null,
      activeDays: byDay.size,
      portfolioValue: value,
      rebateUsd: r2(rebateUsd),
      rebateRows: rebates.length,
    },
    ranks: rankRows || {},
    windows,
    series,
    dayOutcomes,
    orderSizing,
    tradeFlow: {
      buys: buys.length, sells: sells.length,
      buyNotional: r2(buyNotional), sellNotional: r2(sellNotional),
    },
    niches: finalise(nicheAgg),
    tradeTypes: finalise(typeAgg),
    holdTime: {
      n: holdHours.length,
      median: quantile(holdSorted, 0.5),
      p25: quantile(holdSorted, 0.25),
      p75: quantile(holdSorted, 0.75),
      // Share of realised value that came from selling out rather than from
      // the market resolving.
      soldBeforeResolution: soldValueShare,
      heldToResolution: 1 - soldValueShare,
      // Share of finished markets that saw any sell at all.
      soldEpisodeShare,
      exitedBySelling,
    },
    priceBrackets,
    clock,
    behaviour: {
      medianHoldHours: quantile(holdSorted, 0.5),
      winLossAsymmetry: wins.length && losses.length
        ? r2((grossWin / wins.length) / (grossLoss / losses.length)) : null,
      sellBeforeClose: soldValueShare,
      sellBeforeCloseByMarket: soldEpisodeShare,
      hedgedMarketPct: episodes.length ? hedged / episodes.length : 0,
      hedgedMarkets: hedged,
      avgEntryPrice: avgEntryPrice == null ? null : r2(avgEntryPrice),
      profitableWeeks,
      // Maker/taker needs order-level chain logs the public tape does not
      // carry. Reported as unavailable rather than estimated from fills.
      makerRatio: null,
      takerRatio: null,
    },
    balance: {
      cashUsd: cash?.usd ?? null,
      cashAsOf: cash?.asOf ?? null,
      cashSource: cash?.source ?? null,
      inOpenPositions: r2(openValue),
      totalAccountValue: cash?.usd != null ? r2(cash.usd + openValue) : null,
      netDeposited: null,
      trueRoi: null,
    },
    copyScore,
    positions: {
      open: [...open].sort((a, b) => n(b.currentValue) - n(a.currentValue)).slice(0, 100),
      topWins: [...finished].sort((a, b) => b.pnl - a.pnl).slice(0, 12)
        .map((e) => ({
          title: e.title, slug: e.slug, eventSlug: e.eventSlug, icon: e.icon, outcome: e.outcome,
          avgPrice: e.avgEntry, stake: r2(e.stake), realizedPnl: r2(e.pnl),
          soldOut: e.exitedBySelling, endedAt: new Date(e.lastTs * 1000).toISOString().slice(0, 10),
        })),
      worstLosses: [...finished].sort((a, b) => a.pnl - b.pnl).slice(0, 12)
        .map((e) => ({
          title: e.title, slug: e.slug, eventSlug: e.eventSlug, icon: e.icon, outcome: e.outcome,
          avgPrice: e.avgEntry, stake: r2(e.stake), realizedPnl: r2(e.pnl),
          soldOut: e.exitedBySelling, endedAt: new Date(e.lastTs * 1000).toISOString().slice(0, 10),
        })),
    },
    trades: fills.slice(0, 200),
  };
}
