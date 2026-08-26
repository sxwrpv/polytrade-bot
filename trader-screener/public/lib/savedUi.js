/* The saved-wallets pill and drawer.
 *
 * Both pages mount this, so the count and the list stay in step wherever you
 * save from. Everything renders off the store's subscription rather than off a
 * local copy, which is what keeps a save made on the wallet page visible in the
 * drawer without a reload.
 */
import { saved, savedAgo, MAX_SAVED } from './saved.js';
import * as M from './screenerModel.js';
import * as R from './render.js';

const { el } = R;
const HEART_ON = '♥';
const HEART_OFF = '♡';

/**
 * A save toggle for one wallet.
 * @param {() => object} meta  read lazily, so the snapshot is whatever the row
 *   knows at the moment of saving rather than at the moment of rendering.
 */
export function saveButton(wallet, meta = () => ({})) {
  const btn = el('button', 'save-btn');
  btn.type = 'button';
  const paint = () => {
    const on = saved.has(wallet);
    btn.setAttribute('aria-pressed', String(on));
    btn.textContent = on ? HEART_ON : HEART_OFF;
    btn.title = on ? 'Saved — click to remove' : 'Save this wallet';
    btn.setAttribute('aria-label', on ? `Remove ${wallet} from saved wallets` : `Save ${wallet}`);
  };
  paint();
  btn.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    const nowOn = saved.toggle(wallet, meta());
    if (nowOn) {
      btn.classList.remove('pop');
      void btn.offsetWidth; // restart the animation on a repeat save
      btn.classList.add('pop');
    } else if (saved.count() >= MAX_SAVED) {
      btn.title = `Saved list is full (${MAX_SAVED}).`;
    }
    paint();
  };
  saved.subscribe(paint);
  return btn;
}

/** The nav pill. Returns the element; it keeps its own count in step. */
export function savedPill(onOpen) {
  const btn = el('button', 'saved-pill glass');
  btn.type = 'button';
  const heart = el('span', 'heart', HEART_ON);
  const label = el('span', null, 'Saved');
  const count = el('span', 'saved-count', '0');
  btn.append(heart, label, count);

  let previous = saved.count();
  const paint = () => {
    const n = saved.count();
    count.textContent = String(n);
    btn.classList.toggle('is-empty', n === 0);
    btn.setAttribute('aria-label', `Saved wallets, ${n}`);
    if (n > previous) {
      btn.classList.remove('just-saved');
      void btn.offsetWidth;
      btn.classList.add('just-saved');
    }
    previous = n;
  };
  paint();
  saved.subscribe(paint);
  btn.onclick = onOpen;
  return btn;
}

/**
 * The drawer. Mounted once, opened and closed by the pill.
 * @param {object} opts
 * @param {() => Map<string, object>} opts.lookup  live cohort rows by wallet,
 *   so a saved wallet still in this week's cohort shows its current score
 *   rather than the one snapshotted at save time.
 */
export function savedDrawer({ lookup = () => new Map() } = {}) {
  const scrim = el('div', 'drawer-scrim');
  const panel = el('aside', 'drawer glass');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-label', 'Saved wallets');
  panel.hidden = true;
  scrim.hidden = true;

  const head = el('div', 'drawer-head');
  const title = el('div');
  title.append(el('h2', null, 'Saved wallets'));
  const sub = el('p', 'sub');
  title.append(sub);
  const close = el('button', 'drawer-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close saved wallets');
  close.onclick = () => hide();
  head.append(title, close);

  const body = el('div', 'drawer-body');
  const foot = el('div', 'drawer-foot');
  panel.append(head, body, foot);

  let lastFocus = null;

  function paint() {
    const rows = saved.list();
    sub.textContent = rows.length
      ? `${rows.length} wallet${rows.length === 1 ? '' : 's'} · kept on this device`
      : 'Nothing saved yet';

    body.replaceChildren();
    if (!rows.length) {
      const empty = el('div', 'drawer-empty');
      empty.append(el('span', 'heart', HEART_OFF));
      empty.append(document.createTextNode(
        'Tap the heart on any wallet to keep it here. The list lives in this browser only — it is not an account, and it is not sent anywhere.'));
      body.append(empty);
    }

    const cohort = lookup();
    for (const rec of rows) {
      const live = cohort.get(rec.w) || null;
      const row = el('div', 'saved-row');

      row.append(R.avatar(live?.name ?? rec.name, rec.w, live?.img ?? rec.img, 32));

      const mid = el('div');
      mid.style.minWidth = '0';
      const link = el('a', 'who-name', live?.name || rec.name || M.shortAddress(rec.w));
      link.href = M.traderPath(rec.w);
      mid.append(link);
      mid.append(el('span', 'meta', `${M.shortAddress(rec.w)} · saved ${savedAgo(rec.savedAt)}`));

      const chips = el('div', 'chips');
      // Prefer the live cohort figure; fall back to the snapshot, labelled.
      const cls = live?.copyClass ?? rec.copyClass;
      const net = live ? live.copyNet : rec.copyNet;
      const chip = cls ? R.copyChip(cls, net, { variant: 'signed' }) : null;
      if (chip) {
        if (!live) chip.title += ' — as saved; this wallet is not in the current cohort';
        chips.append(chip);
      }
      const pnl = live ? live.pnl : rec.pnl;
      if (Number.isFinite(pnl)) {
        chips.append(el('span', 'meta', `${M.signedMoney(pnl)} lifetime`));
      }
      if (!live && (cls || Number.isFinite(pnl))) {
        chips.append(el('span', 'hchip', 'as saved'));
      }
      if (chips.children.length) mid.append(chips);
      row.append(mid);

      const remove = el('button', 'save-btn', HEART_ON);
      remove.type = 'button';
      remove.setAttribute('aria-pressed', 'true');
      remove.title = 'Remove from saved';
      remove.setAttribute('aria-label', `Remove ${rec.w} from saved wallets`);
      remove.onclick = () => saved.remove(rec.w);
      row.append(remove);

      body.append(row);
    }

    foot.replaceChildren();
    if (rows.length) {
      const csv = el('button', 'btn btn-sm', 'Export CSV');
      csv.type = 'button';
      csv.onclick = () => exportSaved(rows, cohort);
      const clear = el('button', 'btn btn-sm', 'Clear all');
      clear.type = 'button';
      clear.onclick = () => {
        // Destructive and one click from the list, so it asks first.
        if (confirm(`Remove all ${rows.length} saved wallets? This cannot be undone.`)) saved.clear();
      };
      foot.append(csv, clear);
    }
    foot.append(el('p', 'cap',
      'Saved wallets are stored in this browser. Clearing site data clears them, and they do not follow you to another device.'));
  }

  function show() {
    clearTimeout(unmount);
    lastFocus = document.activeElement;
    scrim.hidden = false;
    panel.hidden = false;
    // Force layout so the transition has a start state, rather than waiting a
    // frame: requestAnimationFrame does not fire in a backgrounded or
    // non-compositing tab, and a dialog that only opens when the window happens
    // to be visible is a dialog that sometimes does not open.
    void panel.offsetWidth;
    scrim.classList.add('is-open');
    panel.classList.add('is-open');
    close.focus();
    document.addEventListener('keydown', onKey);
  }

  let unmount = null;
  function hide() {
    scrim.classList.remove('is-open');
    panel.classList.remove('is-open');
    document.removeEventListener('keydown', onKey);
    // Unmount after the transition, but cancel it if the drawer is reopened
    // first — otherwise a quick close-then-open hides the panel mid-animation.
    clearTimeout(unmount);
    unmount = setTimeout(() => { scrim.hidden = true; panel.hidden = true; }, 240);
    if (lastFocus instanceof HTMLElement) lastFocus.focus();
  }

  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); hide(); return; }
    if (e.key !== 'Tab') return;
    // Keep focus inside the dialog while it is open.
    const focusable = panel.querySelectorAll('a[href], button:not([disabled])');
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  scrim.onclick = hide;
  saved.subscribe(paint);
  paint();

  document.body.append(scrim, panel);
  return { open: show, close: hide, element: panel };
}

function exportSaved(rows, cohort) {
  const cell = (v) => {
    if (v == null || (typeof v === 'number' && !Number.isFinite(v))) return '';
    const s = String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const header = ['wallet', 'name', 'copy_class', 'copy_score', 'pnl_usd', 'saved_at', 'source'];
  const lines = [header.join(',')];
  for (const rec of rows) {
    const live = cohort.get(rec.w) || null;
    lines.push([
      rec.w,
      live?.name ?? rec.name,
      live?.copyClass ?? rec.copyClass,
      live ? live.copyNet : rec.copyNet,
      live ? live.pnl : rec.pnl,
      rec.savedAt ? new Date(rec.savedAt).toISOString() : '',
      live ? 'current cohort' : 'as saved',
    ].map(cell).join(','));
  }
  const url = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `saved-wallets-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
