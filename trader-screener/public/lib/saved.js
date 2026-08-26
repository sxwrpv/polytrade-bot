/* Saved wallets.
 *
 * A small store over localStorage, kept free of DOM so its rules are testable:
 * what a record holds, how a legacy list migrates, and what happens when the
 * storage is full or unavailable.
 *
 * Each record snapshots the wallet's identity at save time — name, image, and
 * the score it carried. That is deliberate: the cohort snapshot behind the
 * board turns over, and a wallet you saved last week may not be in this week's
 * ranked set at all. Without the snapshot the saved tab would show a bare
 * address and nothing else. The saved copy is labelled "as saved" wherever it
 * is shown, so it is never mistaken for a live figure.
 */

const KEY = 'polytrade.saved';
/** The earlier "Following" list. Read once, then left alone. */
const LEGACY_KEY = 'polytrade.following';

/** Bound so a runaway loop cannot fill the origin's storage quota. */
export const MAX_SAVED = 500;

const isAddress = (v) => /^0x[0-9a-fA-F]{40}$/.test(String(v ?? '').trim());

export function createStore(storage = globalThis.localStorage, now = () => Date.now()) {
  const listeners = new Set();
  let cache = null;

  function read() {
    if (cache) return cache;
    cache = load();
    return cache;
  }

  function load() {
    let rows = parse(storage?.getItem(KEY));
    if (rows === null) {
      // First run on a browser that used the old Following list: carry it over
      // rather than silently starting empty.
      const legacy = parse(storage?.getItem(LEGACY_KEY));
      rows = Array.isArray(legacy)
        ? legacy.filter(isAddress).map((w) => ({ w: w.toLowerCase(), savedAt: null, migrated: true }))
        : [];
      if (rows.length) persist(rows);
    }
    return rows.filter((r) => r && isAddress(r.w));
  }

  function parse(raw) {
    if (raw == null) return null;
    try {
      const value = JSON.parse(raw);
      if (!Array.isArray(value)) return null;
      // The legacy shape is a bare array of addresses; the current one is records.
      return value.map((v) => (typeof v === 'string' ? v : v)).length ? value : [];
    } catch {
      return null;
    }
  }

  function persist(rows) {
    cache = rows;
    try {
      storage?.setItem(KEY, JSON.stringify(rows));
    } catch {
      // Quota or a locked-down browser. The list still works for this session;
      // failing loudly here would break saving for no gain.
    }
    for (const fn of listeners) fn(rows);
  }

  return {
    list: () => [...read()].sort((a, b) => (b.savedAt ?? 0) - (a.savedAt ?? 0)),
    count: () => read().length,
    has: (wallet) => isAddress(wallet) && read().some((r) => r.w === wallet.toLowerCase()),

    /** Save, or update the snapshot on a wallet already saved. */
    save(wallet, meta = {}) {
      if (!isAddress(wallet)) return false;
      const w = wallet.toLowerCase();
      const rows = read().filter((r) => r.w !== w);
      if (rows.length >= MAX_SAVED) return false;
      rows.push({
        w,
        name: meta.name ?? null,
        img: meta.img ?? null,
        copyClass: meta.copyClass ?? null,
        copyNet: Number.isFinite(meta.copyNet) ? meta.copyNet : null,
        pnl: Number.isFinite(meta.pnl) ? meta.pnl : null,
        savedAt: now(),
      });
      persist(rows);
      return true;
    },

    remove(wallet) {
      if (!isAddress(wallet)) return false;
      const w = wallet.toLowerCase();
      const rows = read();
      const next = rows.filter((r) => r.w !== w);
      if (next.length === rows.length) return false;
      persist(next);
      return true;
    },

    /** @returns {boolean} whether the wallet is saved after the toggle. */
    toggle(wallet, meta) {
      if (this.has(wallet)) { this.remove(wallet); return false; }
      return this.save(wallet, meta);
    },

    clear() { persist([]); },

    /** @returns {() => void} unsubscribe */
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

export const saved = createStore();

/** "3 days ago" for a save timestamp; null reads as carried over. */
export function savedAgo(ts, now = Date.now()) {
  if (ts == null) return 'carried over';
  const mins = (now - ts) / 60000;
  if (mins < 1) return 'just now';
  if (mins < 60) return `${Math.round(mins)}m ago`;
  const hours = mins / 60;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? 'yesterday' : `${days}d ago`;
}
