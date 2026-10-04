const WINDOWS = { d7: 'WEEK', d30: 'MONTH', all: 'ALL' };
const LEADERBOARD = 'https://data-api.polymarket.com/v1/leaderboard';
const LIMIT = 100;

export function periodToLeaderboardWindow(period) {
  const window = WINDOWS[period];
  if (!window) throw new Error(`unsupported period: ${period}`);
  return window;
}

const addressOf = (row) => String(row?.proxyWallet || '').toLowerCase();
const finite = (value) => {
  if (value == null || value === '') return null;
  return Number.isFinite(Number(value)) ? Number(value) : null;
};

export function buildLiveUniverse({ period, pnlRows = [], volumeRows = [], snapshot, fetchedAt }) {
  periodToLeaderboardWindow(period);
  const overlay = new Map((snapshot?.traders || []).map((row) => [String(row.w).toLowerCase(), row]));
  const live = new Map();
  for (const row of [...pnlRows, ...volumeRows]) {
    const w = addressOf(row);
    if (!/^0x[a-f0-9]{40}$/.test(w)) continue;
    live.set(w, { ...(live.get(w) || {}), ...row });
  }

  const traders = [...live.entries()].map(([w, row]) => {
    const score = overlay.get(w) || {};
    const pnl = finite(row.pnl);
    const vol = finite(row.vol);
    return {
      w,
      name: row.userName || null,
      img: row.profileImage || null,
      xUsername: row.xUsername || null,
      verified: Boolean(row.verifiedBadge),
      followers: null,
      pnl: period === 'all' ? pnl : null,
      d7: period === 'd7' ? pnl : null,
      d30: period === 'd30' ? pnl : null,
      vol: period === 'all' ? vol : null,
      d7Vol: period === 'd7' ? vol : null,
      d30Vol: period === 'd30' ? vol : null,
      openVal: null,
      openPositions: null,
      copyClass: score.copyClass ?? null,
      copyNet: score.copyNet ?? null,
      mm: null,
      arb: null,
      freq: null,
      cats: [],
      fills: null,
      activeDays: null,
      winRate: null,
      avgSize: null,
      lastTradeDay: fetchedAt.slice(0, 10),
      liveRank: finite(row.rank),
      livePeriod: period,
    };
  });

  return {
    meta: {
      source: 'live-polymarket-leaderboard',
      generatedAt: fetchedAt,
      windowAnchor: fetchedAt.slice(0, 10),
      scoreGeneratedAt: snapshot?.meta?.generatedAt || null,
      leaderboardWindow: periodToLeaderboardWindow(period),
      boardsFrozen: false,
    },
    traders,
    walletMeta: {},
    spark: {},
    wow: {},
    copyDelta: {},
    angles: [],
    events: [],
    groups: [],
    structures: [],
    wowAnchor: null,
  };
}

export async function fetchLiveUniverse({
  period, snapshot, fetchImpl = fetch, fetchedAt = new Date().toISOString(), signal,
}) {
  const timePeriod = periodToLeaderboardWindow(period);
  const getRows = async (orderBy) => {
    const query = new URLSearchParams({ timePeriod, orderBy, limit: String(LIMIT), offset: '0' });
    const response = await fetchImpl(`${LEADERBOARD}?${query}`, {
      headers: { accept: 'application/json', 'user-agent': 'polytrade-screener/1.0' },
      signal,
    });
    if (!response.ok) throw new Error(`Polymarket leaderboard ${response.status}`);
    const rows = await response.json();
    if (!Array.isArray(rows)) throw new Error('Polymarket leaderboard returned an invalid payload');
    return rows;
  };
  const [pnlRows, volumeRows] = await Promise.all([getRows('PNL'), getRows('VOL')]);
  return buildLiveUniverse({ period, pnlRows, volumeRows, snapshot, fetchedAt });
}
