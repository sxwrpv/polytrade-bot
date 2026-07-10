"""Copy engine — polls watched trader wallets and mirrors their positions.

Runs as an asyncio background task. Each tick, per (user, followed trader):
  1. fetch the trader's live positions (data-api)
  2. diff them against the user's OPEN copy_positions rows **in the DB**
     (the source of truth — so a restart never re-opens a held position)
  3. emit intents — OPEN / CLOSE / RESIZE / RESOLVE — and execute them

Sizing is portfolio-aware (fixes the original per-position %-of-balance bug): a
trader position's target notional is its weight within the trader's portfolio,
scaled by the user's earmarked capital (allocation_pct), capped by
max_position_usd and the user's available collateral. Σ(weights)=1, so total
exposure to a trader can never exceed the earmarked capital.

`plan_actions` is pure (no IO) and exhaustively tested. The engine's collaborators
(client factory, order placement, collateral lookup) are injectable so the whole
diff→intent→persist path is verifiable without funds.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import aiosqlite
import httpx

from backend.config import (
    COPY_ENGINE_POLL_SECONDS,
    DEFAULT_COPY_RATIO_PCT,
    DEFAULT_IGNORE_BELOW_USD,
    DEFAULT_MAX_POSITION_USD,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_PRICE,
    DETECTION_POLL_SECONDS,
    ENCRYPTION_SECRET,
    MAX_COPY_SLIPPAGE_PCT,
    validate_slippage_pct,
)
from backend.core import detection, execution, wallet
from backend.core.polymarket import Position
from backend.db.database import now_iso

log = logging.getLogger("copy_engine")

MIN_NOTIONAL_USD = 1.0       # don't open/resize below this (avoids dust + min-size rejects)
RESIZE_THRESHOLD = 0.25      # rebalance only when target drifts >25% from current


# ---------------------------------------------------------------------------
# Planning (pure)
# ---------------------------------------------------------------------------

@dataclass
class Action:
    kind: str                 # 'open' | 'close' | 'resize' | 'resolve'
    token_id: str = ""
    condition_id: str = ""
    outcome: str = ""
    side: str = ""            # 'BUY' | 'SELL' | '' (resolve has no order)
    amount: float = 0.0       # pUSD for BUY, shares for SELL
    notional_usd: float = 0.0
    reference_price: float | None = None
    subkind: str = ""         # 'increase' | 'decrease' for resize
    trader_shares: float = 0.0           # trader's share count we are mirroring
    position: object | None = None       # trader position snapshot
    row: dict | None = None              # existing copy_positions row
    trader_address: str = ""             # configured followed wallet (lower-case)
    claim_id: str = ""                   # durable BUY reservation/fencing token


def plan_actions(
    trader_positions: list[Position],
    open_rows: list[dict],
    follow: dict,
    user_capital: float,
    available_collateral: float,
    *,
    min_notional: float = MIN_NOTIONAL_USD,
    resize_threshold: float = RESIZE_THRESHOLD,
    size_multiplier: float = 1.0,
    max_total_exposure: float | None = None,
    block_opens: bool = False,
    ratio_pct: float = DEFAULT_COPY_RATIO_PCT,
    max_per_trade: float | None = None,
    min_leader: float = 0.0,
    ignore_below: float | None = None,
    max_open: int | None = None,
    min_price: float = DEFAULT_MIN_PRICE,
    max_price: float = DEFAULT_MAX_PRICE,
) -> list[Action]:
    """Diff a leader's live positions against our open copies and emit
    open/close/resize/resolve intents.

    Sizing (owner model, 2026-07-06): each OPEN mirrors the LEADER's own
    position value — copy notional = leader_position_value × ratio_pct% — then
    clamped by MAX/TRADE (max_per_trade), available collateral, and the
    per-trader exposure cap. Entry filters skip a leader position when it's too
    small (min_leader), outside the price band (min_price..max_price), when our
    resulting copy would be dust (ignore_below), or when we're already at the
    MAX OPEN count for this trader.
    """
    pos_by_token = {p.asset: p for p in trader_positions if p.size > 0}
    rows_by_token = {r["token_id"]: r for r in open_rows}

    max_pos = max_per_trade if max_per_trade is not None else follow["max_position_usd"]
    dust_floor = ignore_below if ignore_below is not None else min_notional
    open_count = len(open_rows)              # already-open copies for this trader
    remaining = available_collateral
    if max_total_exposure is not None:   # portfolio exposure cap
        current_exposure = sum(r["notional_usd"] for r in open_rows)
        remaining = min(remaining, max(0.0, max_total_exposure - current_exposure))
    actions: list[Action] = []

    for token, p in pos_by_token.items():
        row = rows_by_token.get(token)
        if p.redeemable:
            # market resolved — realize ours (no order); never open into it.
            if row is not None:
                actions.append(Action(kind="resolve", token_id=token, row=row, position=p,
                                      reference_price=p.cur_price))
            continue

        if row is None:
            if block_opens:                 # risk gate (paused-opens / daily loss)
                continue
            if max_open is not None and open_count >= max_open:
                continue                    # MAX OPEN reached for this trader
            leader_notional = p.current_value
            if leader_notional < min_leader:            # MIN LEADER $ filter
                continue
            if not (min_price <= p.cur_price <= max_price):  # price-band filter
                continue
            # OPEN: copy the leader's dollar position, scaled by RATIO %, capped.
            amt = min(leader_notional * ratio_pct / 100.0, max_pos, remaining)
            if amt >= dust_floor:
                actions.append(Action(
                    kind="open", token_id=token, condition_id=p.condition_id,
                    outcome=p.outcome.upper(), side="BUY", amount=amt, notional_usd=amt,
                    reference_price=p.avg_price, trader_shares=p.size, position=p))
                remaining -= amt
                open_count += 1
        else:
            # RESIZE: mirror the trader's own change in share count for THIS market.
            base = row.get("trader_shares") or p.size
            ratio = (p.size / base) if base > 0 else 1.0
            if ratio > 1 + resize_threshold and not block_opens:
                delta_shares = row["shares"] * (ratio - 1)
                headroom = max_pos - row["notional_usd"]
                amt = min(delta_shares * p.cur_price, remaining, headroom)
                if amt >= min_notional:
                    actions.append(Action(
                        kind="resize", subkind="increase", token_id=token, side="BUY",
                        amount=amt, notional_usd=amt, reference_price=p.cur_price,
                        trader_shares=p.size, row=row, position=p))
                    remaining -= amt
            elif ratio < 1 - resize_threshold:
                shares_to_sell = row["shares"] * (1 - ratio)
                actions.append(Action(
                    kind="resize", subkind="decrease", token_id=token, side="SELL",
                    amount=shares_to_sell, reference_price=p.cur_price,
                    trader_shares=p.size, row=row, position=p))
            # else: within band — hold

    # trader exited (token no longer held) → close ours
    for token, row in rows_by_token.items():
        if token not in pos_by_token:
            actions.append(Action(kind="close", token_id=token, side="SELL",
                                  amount=row["shares"], row=row))
    return actions


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class CopyEngine:
    def __init__(self, db, pm, *, client_factory=None, place_order=None,
                 collateral_fn=None, detector=None,
                 poll_interval: float | None = None,
                 detection_interval: float | None = None,
                 risk_lock: asyncio.Lock | None = None) -> None:
        self.db = db
        self.pm = pm
        self.poll_interval = poll_interval or COPY_ENGINE_POLL_SECONDS
        self.detection_interval = detection_interval or DETECTION_POLL_SECONDS
        self.detector = detector or detection.ActivityPollDetector(pm)
        self._client_factory = client_factory or self._default_client_factory
        # Market FOK for BOTH sides (owner's call, 2026-07-03 — copies must
        # fill rather than strictly bound price). The pre-flight slippage
        # guard inside place_market_order still skips a copy when the quoted
        # average exceeds the wallet's max_slippage_pct vs the leader's price,
        # so "market buy" here means "fill at market unless it's gone N% away".
        self._place_order = place_order or execution.place_market_order
        self._collateral_fn = collateral_fn or self._default_collateral
        # Shared with risk-setting API writes in the running app. A pause or
        # stricter limit either wins before a BUY, or is acknowledged only after
        # an already-submitted BUY completes.
        self._risk_lock = risk_lock or asyncio.Lock()
        self._clients: dict[str, object] = {}
        # fast-detection cursors / dedupe, per (user_id, trader_address).
        # _seen values are insertion-ordered dicts used as bounded sets.
        self._cursors: dict[tuple, int] = {}
        self._seen: dict[tuple, dict] = defaultdict(dict)

    # --- lifecycle ---------------------------------------------------------
    async def run(self, stop_event: asyncio.Event) -> None:
        # A reserved claim is provably pre-submission and can be reclaimed after
        # a crash. A stale submitting claim is never retried automatically.
        await self._recover_stale_claims()
        # Two cadences: fast trade detection (entry latency) + slow reconciliation
        # (missed trades, drift, resolutions).
        await asyncio.gather(
            self._loop(self._detect_tick, self.detection_interval, stop_event),
            self._loop(self._reconcile_tick, self.poll_interval, stop_event),
        )

    async def _recover_stale_claims(self) -> None:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)).isoformat()
        released = await self.db.execute(
            "DELETE FROM copy_open_claims WHERE state='reserved' AND updated_at < ?", (cutoff,))
        uncertain = await self.db.execute(
            "UPDATE copy_open_claims SET state='uncertain',last_error=?,updated_at=? "
            "WHERE state='submitting' AND updated_at < ?",
            ("stale submission requires reconciliation", now_iso(), cutoff))
        if released or uncertain:
            log.warning("claim recovery: released_reserved=%d marked_uncertain=%d",
                        released, uncertain)

    async def aclose(self) -> None:
        """Close every cached per-user CLOB client (network sessions)."""
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                pass
        self._clients.clear()

    async def _loop(self, fn, interval: float, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await fn()
            except Exception:
                log.exception("loop %s failed", getattr(fn, "__name__", fn))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # --- fast path: per-leader trade detection -----------------------------
    async def _detect_tick(self) -> None:
        follows = await self.db.fetchall(
            "SELECT * FROM followed_traders WHERE is_active = 1")
        # drop cursor/dedupe state for unfollowed pairs — without this the maps
        # only ever grow for the life of the process
        active = {(f["user_id"], f["trader_address"]) for f in follows}
        for key in [k for k in self._cursors if k not in active]:
            self._cursors.pop(key, None)
            self._seen.pop(key, None)
        by_trader: dict[str, list[dict]] = defaultdict(list)
        for f in follows:
            by_trader[f["trader_address"]].append(f)

        for trader_address, trader_follows in by_trader.items():
            pending = []
            for f in trader_follows:
                key = (f["user_id"], trader_address)
                if key not in self._cursors:
                    # first sight: start now, don't retro-copy the leader's history
                    self._cursors[key] = int(time.time())
                    continue
                pending.append(f)
            if not pending:
                continue
            # One detector call per TRADER per tick, regardless of follower count.
            # Detectors with per-trader server-side cursor state (OnChainDetector's
            # _last_block) would otherwise have their cursor consumed by whichever
            # follower's row happened to be processed first, silently starving
            # every other follower of that trader for the tick. Fan the same
            # result out to every follower below instead.
            since = min(self._cursors[(f["user_id"], trader_address)] for f in pending)
            try:
                trades = sorted(await self.detector.new_trades(trader_address, since),
                                key=lambda x: x.timestamp)
            except Exception:
                log.exception("detect failed for %s", trader_address)
                continue
            for f in pending:
                key = (f["user_id"], trader_address)
                cursor = self._cursors[key]
                seen = self._seen[key]
                for t in trades:
                    if t.timestamp <= cursor:
                        continue
                    if t.tx_hash and t.tx_hash in seen:
                        continue
                    try:
                        await self._handle_leader_trade(f, t)
                    except Exception:
                        log.exception("handle leader trade failed")
                    if t.tx_hash:
                        seen[t.tx_hash] = None
                        if len(seen) > 2000:      # bound the dedupe window;
                            for h in list(seen)[:1000]:   # dict = insertion-ordered,
                                del seen[h]               # so this drops the oldest
                    self._cursors[key] = max(self._cursors[key], t.timestamp)

    async def _handle_leader_trade(self, follow: dict, trade) -> None:
        detected_at = time.time()
        leader_age = detected_at - float(trade.timestamp or detected_at)
        user_id = follow["user_id"]
        trader = follow["trader_address"]
        frisk = self._follow_risk(follow)
        user = await self.db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
        if not user:
            return
        token = trade.asset
        existing = await self.db.fetchone(
            "SELECT * FROM copy_positions WHERE user_id = ? AND token_id = ? AND status = 'open'",
            (user_id, token))

        if trade.side.upper() == "BUY":
            if frisk["paused"]:             # pause = no NEW buys…
                return
            if existing:                    # already in — reconciler handles resize
                return
            if await self._opens_blocked(user_id, trader, frisk["daily_limit"]):
                return
            # Leader's TOTAL position in this market — needed BOTH to size the
            # copy (ratio-of-leader) and as trader_shares for the resize math
            # (the reconciler resizes off p.size/trader_shares, so recording a
            # single top-up trade against a large position would read as a big
            # increase and churn). Falls back to this trade if not yet indexed.
            trader_total = trade.size
            leader_price = float(trade.price or 0)
            try:
                tpos = await self.pm.get_positions(trader, size_threshold=0)
                match = next((p for p in tpos if p.asset == token), None)
                if match and match.size > 0:
                    trader_total = match.size
                    leader_price = match.cur_price or leader_price
            except Exception:
                log.exception("trader position lookup failed; using trade size")
            leader_notional = trader_total * (leader_price or float(trade.price or 0))

            # entry filters (same as the reconciler's plan_actions)
            if leader_notional < frisk["min_leader"]:
                log.info("fast-open skipped %s reason=below_min_leader (%.2f<%.2f) trader=%s",
                         token, leader_notional, frisk["min_leader"], trader[:10])
                return
            if not (frisk["min_price"] <= (leader_price or 0) <= frisk["max_price"]):
                log.info("fast-open skipped %s reason=price_out_of_band (%.3f) trader=%s",
                         token, leader_price or 0, trader[:10])
                return

            client = await self._get_client(user)   # expensive — after the cheap checks
            available = await self._collateral_fn(client)
            all_open = await self.db.fetchall(
                "SELECT notional_usd, trader_address FROM copy_positions "
                "WHERE user_id = ? AND status = 'open'", (user_id,))
            trader_open_rows = [r for r in all_open if r["trader_address"] == trader]
            if frisk["max_open"] is not None and len(trader_open_rows) >= frisk["max_open"]:
                log.info("fast-open skipped %s reason=max_open (%d) trader=%s",
                         token, frisk["max_open"], trader[:10])
                return
            # RATIO %: copy the leader's dollar position, scaled, then capped.
            notional = min(leader_notional * frisk["ratio_pct"] / 100.0,
                           frisk["max_per_trade"], available)
            if frisk["max_exposure"] is not None:   # cap exposure to THIS trader
                trader_open = sum(r["notional_usd"] for r in trader_open_rows)
                notional = min(notional, max(0.0, frisk["max_exposure"] - trader_open))
            if notional < frisk["ignore_below"]:
                log.info(
                    "fast-open skipped %s age=%.1fs notional=%.2f reason=below_dust_floor trader=%s",
                    token, leader_age, notional, trader[:10])
                return
            log.info(
                "fast-open candidate %s age=%.1fs side=%s notional=%.2f ref=%.4f trader=%s",
                token, leader_age, trade.side.upper(), notional, float(trade.price or 0), trader[:10])
            action = Action(
                kind="open", token_id=token, condition_id=trade.condition_id,
                outcome=trade.outcome.upper(), side="BUY", amount=notional,
                notional_usd=notional, reference_price=trade.price,
                trader_shares=trader_total,
                position=SimpleNamespace(
                    proxy_wallet=trader, asset=token, condition_id=trade.condition_id,
                    size=trader_total, avg_price=float(trade.price or leader_price),
                    cur_price=leader_price, current_value=leader_notional,
                    redeemable=False, outcome=trade.outcome.upper(), slug=trade.slug,
                    title=trade.title),
                trader_address=trader,
            )
            spent = await self._execute(user_id, client, action, slippage=frisk["slippage"])
            if spent:
                log.info(
                    "fast-open recorded %s total_age=%.1fs notional=%.2f trader=%s",
                    token, time.time() - float(trade.timestamp or detected_at), spent, trader[:10])
        else:  # leader SELL — exit fast (market FOK; exits aren't spread-sensitive).
            # Deliberately NOT gated on paused: pause stops new buys, but the
            # money already in open copies keeps being managed.
            if not existing:
                return
            # Sell PROPORTIONALLY to the leader's reduction, not everything: a
            # 10% trim by the leader must not full-exit us — the reconciler
            # would see the leader still holding and re-buy what we just sold
            # (churn, paying the spread twice). trader_shares is the leader
            # total we recorded at open/last-resize; ≥95% of it counts as a
            # full exit (avoids dust positions from rounding).
            base = float(existing.get("trader_shares") or 0.0)
            fraction = min(1.0, trade.size / base) if base > 0 else 1.0
            client = await self._get_client(user)
            # Claim the row before placing the exit order: if a manual close or
            # the reconciler is closing/resizing this same position concurrently,
            # only one caller wins the atomic status flip — the other skips
            # instead of also submitting a SELL for the same shares.
            if not await self.db.claim_managed_sell(user_id, token, existing["id"]):
                return
            full_exit = fraction >= 0.95
            try:
                sell_shares = existing["shares"] if full_exit else existing["shares"] * fraction
                result = await self._place_order(
                    client, self.pm, token, "SELL", sell_shares,
                    reference_price=trade.price)
            except Exception:
                # Raised failures are before execution's submission boundary.
                await self.db.try_transition(existing["id"], "closing", "open")
                raise
            if full_exit:
                if result.ok:
                    # Persistence is intentionally outside the pre-submission
                    # exception handler: after a successful order, any DB failure
                    # must leave the durable closing fence in place.
                    await self._close_row(user_id, existing, result.avg_price,
                                          result.filled_shares)
                elif not getattr(result, "submission_uncertain", False):
                    await self.db.try_transition(existing["id"], "closing", "open")
                return
            if result.ok:
                sold = result.filled_shares
                pnl = (result.avg_price - existing["entry_price"]) * sold
                new_shares = max(0.0, existing["shares"] - sold)
                frac_left = new_shares / existing["shares"] if existing["shares"] else 0.0
                async with self.db.transaction(write=True) as tx:
                    changed = await tx.execute(
                        "UPDATE copy_positions SET shares=?,notional_usd=?,trader_shares=?,status='open' "
                        "WHERE id=? AND user_id=? AND status='closing'",
                        (new_shares, existing["notional_usd"] * frac_left,
                         max(0.0, base - trade.size), existing["id"], user_id))
                    if changed != 1:
                        raise RuntimeError("fast partial SELL finalization lost closing fence")
                    await self._event(
                        user_id, existing["id"], "partial", None, pnl, store=tx)
            elif not getattr(result, "submission_uncertain", False):
                await self.db.try_transition(existing["id"], "closing", "open")

    async def _reconcile_tick(self) -> None:
        follows = await self.db.fetchall(
            "SELECT * FROM followed_traders WHERE is_active = 1")
        by_user: dict[str, list[dict]] = {}
        for f in follows:
            by_user.setdefault(f["user_id"], []).append(f)
        for user_id, user_follows in by_user.items():
            try:
                await self._sync_user(user_id, user_follows)
            except Exception:
                log.exception("sync failed for user %s", user_id)

    # --- per-user ----------------------------------------------------------
    async def _sync_user(self, user_id: str, follows: list[dict]) -> None:
        user = await self.db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
        if not user:
            return
        client = await self._get_client(user)
        available = await self._collateral_fn(client)
        open_rows_all = await self.db.fetchall(
            "SELECT * FROM copy_positions WHERE user_id = ? AND status = 'open'",
            (user_id,))
        # user capital = available collateral + cost basis of open copies
        user_capital = available + sum(r["notional_usd"] for r in open_rows_all)

        for follow in follows:
            frisk = self._follow_risk(follow)
            trader = follow["trader_address"]
            open_rows = [r for r in open_rows_all if r["trader_address"] == trader]
            # PAUSE means "no new buys" — NOT "abandon the positions already
            # bought with the user's money". A paused follow with open rows
            # still gets closes/resolutions/resize-downs (block_opens
            # suppresses opens and resize-ups); only a paused follow with
            # nothing open is skipped entirely. (Surfaced live 2026-07-03:
            # the owner paused a wallet and its resolved positions sat
            # unmanaged forever.)
            if frisk["paused"] and not open_rows:
                continue
            positions = await self.pm.get_positions(trader)
            block_opens = frisk["paused"] or await self._opens_blocked(
                user_id, trader, frisk["daily_limit"])
            actions = plan_actions(
                positions, open_rows, follow, user_capital, available,
                max_total_exposure=frisk["max_exposure"], block_opens=block_opens,
                ratio_pct=frisk["ratio_pct"], max_per_trade=frisk["max_per_trade"],
                min_leader=frisk["min_leader"], ignore_below=frisk["ignore_below"],
                max_open=frisk["max_open"], min_price=frisk["min_price"],
                max_price=frisk["max_price"])
            for action in actions:
                action.trader_address = trader
                spent = await self._execute(user_id, client, action, slippage=frisk["slippage"])
                if action.side == "BUY":
                    available = max(0.0, available - spent)

    # --- execution + persistence ------------------------------------------
    async def _execute(self, user_id: str, client, action: Action,
                       slippage: float = MAX_COPY_SLIPPAGE_PCT) -> float:
        if action.side == "BUY":
            return await self._execute_buy(user_id, client, action)
        if action.kind == "resolve":
            if not await self.db.try_transition(action.row["id"], "open", "closing"):
                return 0.0   # already being closed/resolved elsewhere
            await self._realize_resolution(user_id, action)
            return 0.0
        # Every SELL mutating a managed row needs the same durable close fence;
        # this includes resize-down as well as full close.
        claimed_sell = action.side == "SELL" and action.row is not None
        if claimed_sell:
            if not await self.db.claim_managed_sell(
                    user_id, action.token_id, action.row["id"]):
                log.info("sell skipped (already claimed): %s", action.row["id"])
                return 0.0
        try:
            result = await self._place_order(
                client, self.pm, action.token_id, action.side, action.amount,
                reference_price=action.reference_price, max_slippage_pct=slippage)
        except httpx.HTTPStatusError as e:
            # A dead order book (404) on a CLOSE means the market resolved
            # before we could exit — the position redeems instead of selling.
            if action.kind == "close" and e.response.status_code == 404:
                await self._resolve_departed(user_id, action.row)
                return 0.0
            if claimed_sell:
                await self.db.try_transition(action.row["id"], "closing", "open")
            raise
        except Exception:
            # execution turns exceptions after submission into an uncertain
            # OrderResult. A raised exception is pre-submission and retryable.
            if claimed_sell:
                await self.db.try_transition(action.row["id"], "closing", "open")
            raise
        if not result.ok:
            if claimed_sell and not getattr(result, "submission_uncertain", False):
                await self.db.try_transition(action.row["id"], "closing", "open")
            log.warning("order skipped (%s %s): %s", action.kind, action.token_id, result.reason)
            return 0.0
        if action.kind == "close":
            await self._record_close(user_id, action, result)
        elif action.kind == "resize":
            recorded = await self._record_resize(user_id, action, result)
            return recorded
        return 0.0

    async def _execute_buy(self, user_id: str, client, action: Action) -> float:
        """Reserve, fence, submit once, then atomically persist every BUY."""
        async with self._risk_lock:
            prepared = await self._prepare_buy(user_id, action)
            if prepared is None:
                return 0.0
            action, risk = prepared
            if not await self._mark_claim_submitting(user_id, action):
                await self._release_buy_claim(user_id, action.token_id, action.claim_id)
                return 0.0
            try:
                result = await self._place_order(
                    client, self.pm, action.token_id, "BUY", action.amount,
                    reference_price=action.reference_price,
                    max_slippage_pct=risk["slippage"],
                    min_price=risk["min_price"], max_price=risk["max_price"])
            except Exception:
                # execution.place_market_order converts every exception at or
                # after the submission boundary into submission_uncertain. A
                # raised exception is therefore pre-submission and retryable.
                await self._release_buy_claim(user_id, action.token_id, action.claim_id)
                raise
            if not result.ok:
                log.warning("order skipped (%s %s): %s", action.kind,
                            action.token_id, result.reason)
                if getattr(result, "submission_uncertain", False):
                    await self._mark_claim_uncertain(user_id, action, result.reason)
                else:
                    await self._release_buy_claim(user_id, action.token_id, action.claim_id)
                return 0.0
            try:
                if action.kind == "open":
                    return await self._record_open(user_id, action, result)
                return await self._record_resize(user_id, action, result)
            except Exception as exc:
                await self._mark_claim_uncertain(user_id, action, f"filled; persistence failed: {exc}")
                log.critical("BUY filled but persistence failed; claim retained: %s %s",
                             user_id[:10], action.token_id, exc_info=True)
                raise

    async def _prepare_buy(self, user_id: str, action: Action) -> tuple[Action, dict] | None:
        trader = (action.trader_address or (action.row or {}).get("trader_address")
                  or getattr(action.position, "proxy_wallet", "")).lower()
        if not trader:
            return None
        try:
            async with self.db.transaction(write=True) as tx:
                user_sql = "SELECT * FROM users WHERE id = ?" + (" FOR UPDATE" if self.db.is_pg else "")
                user = await tx.fetchone(user_sql, (user_id,))
                follow = await tx.fetchone(
                    "SELECT * FROM followed_traders WHERE user_id=? AND trader_address=?",
                    (user_id, trader))
                if not user or not follow or not follow.get("is_active"):
                    return None
                risk = self._follow_risk(follow)
                if bool(user.get("paused")) or risk["paused"]:
                    return None
                if await self._opens_blocked(user_id, trader, risk["daily_limit"], store=tx):
                    return None
                open_all = await tx.fetchall(
                    "SELECT * FROM copy_positions WHERE user_id=? "
                    "AND status IN ('open','closing','reconciliation_required')", (user_id,))
                claims = await tx.fetchall(
                    "SELECT * FROM copy_open_claims WHERE user_id=? "
                    "AND state IN ('reserved','submitting','uncertain')", (user_id,))
                trader_open = [r for r in open_all if r["trader_address"].lower() == trader]
                trader_claims = [r for r in claims if r["trader_address"].lower() == trader]
                allowed = float(action.amount)
                if action.kind == "open":
                    active = await tx.fetchone(
                        "SELECT id FROM copy_positions WHERE user_id=? AND token_id=? "
                        "AND status IN ('open','closing','reconciliation_required')", (user_id, action.token_id))
                    if active:
                        return None
                    reserved_opens = sum(1 for r in trader_claims if r.get("action") == "open")
                    if risk["max_open"] is not None and len(trader_open) + reserved_opens >= risk["max_open"]:
                        return None
                    p = action.position
                    price = float(getattr(p, "cur_price", 0) or action.reference_price or 0)
                    leader_notional = float(getattr(p, "current_value", 0) or 0)
                    if leader_notional <= 0:
                        leader_notional = float(getattr(p, "size", 0) or 0) * price
                    if leader_notional < risk["min_leader"] or not (risk["min_price"] <= price <= risk["max_price"]):
                        return None
                    allowed = min(allowed, leader_notional * risk["ratio_pct"] / 100.0,
                                  risk["max_per_trade"])
                    floor = risk["ignore_below"]
                elif action.kind == "resize" and action.subkind == "increase":
                    fresh = await tx.fetchone(
                        "SELECT * FROM copy_positions WHERE id=? AND status='open'", (action.row["id"],))
                    if not fresh:
                        return None
                    action = replace(action, row=fresh)
                    allowed = min(allowed, max(0.0, risk["max_per_trade"] - fresh["notional_usd"]))
                    floor = MIN_NOTIONAL_USD
                else:
                    return None
                trader_used = (sum(float(r["notional_usd"]) for r in trader_open)
                               + sum(float(r.get("reserved_usd") or 0) for r in trader_claims))
                if risk["max_exposure"] is not None:
                    allowed = min(allowed, max(0.0, risk["max_exposure"] - trader_used))
                if user.get("max_total_exposure_usd") is not None:
                    user_used = (sum(float(r["notional_usd"]) for r in open_all)
                                 + sum(float(r.get("reserved_usd") or 0) for r in claims))
                    allowed = min(allowed, max(0.0, float(user["max_total_exposure_usd"]) - user_used))
                if allowed < floor:
                    return None
                claim_id = uuid.uuid4().hex
                now = now_iso()
                await tx.execute(
                    "INSERT INTO copy_open_claims(user_id,token_id,trader_address,claim_id,action,state,"
                    "reserved_usd,risk_revision,claimed_at,updated_at) VALUES(?,?,?,?,?,'reserved',?,?,?,?)",
                    (user_id, action.token_id, trader, claim_id, action.kind,
                     allowed, int(user.get("risk_revision") or 0), now, now))
                return replace(action, amount=allowed, notional_usd=allowed,
                               trader_address=trader, claim_id=claim_id), risk
        except aiosqlite.IntegrityError:
            log.info("buy skipped (already claimed): %s %s", user_id[:10], action.token_id)
            return None

    async def _mark_claim_submitting(self, user_id: str, action: Action) -> bool:
        async with self.db.transaction(write=True) as tx:
            user_sql = "SELECT risk_revision FROM users WHERE id=?" + (" FOR UPDATE" if self.db.is_pg else "")
            await tx.fetchone(user_sql, (user_id,))
            count = await tx.execute(
                "UPDATE copy_open_claims SET state='submitting',updated_at=? "
                "WHERE user_id=? AND token_id=? AND claim_id=? AND state='reserved' "
                "AND risk_revision=(SELECT risk_revision FROM users WHERE id=?) "
                "AND EXISTS(SELECT 1 FROM users WHERE id=? AND paused=0) "
                "AND EXISTS(SELECT 1 FROM followed_traders WHERE user_id=? AND trader_address=? "
                "AND is_active=1 AND paused=0)",
                (now_iso(), user_id, action.token_id, action.claim_id, user_id,
                 user_id, user_id, action.trader_address))
            return count == 1

    async def _mark_claim_uncertain(self, user_id: str, action: Action, error: str) -> None:
        await self.db.execute(
            "UPDATE copy_open_claims SET state='uncertain',updated_at=?,last_error=? "
            "WHERE user_id=? AND token_id=? AND claim_id=?",
            (now_iso(), error[:500], user_id, action.token_id, action.claim_id))

    async def _release_buy_claim(self, user_id: str, token_id: str, claim_id: str) -> None:
        await self.db.execute(
            "DELETE FROM copy_open_claims WHERE user_id=? AND token_id=? AND claim_id=?",
            (user_id, token_id, claim_id))

    async def _record_open(self, user_id, action, result) -> float:
        p = action.position
        spent = round(result.filled_shares * result.avg_price, 2)
        pid = uuid.uuid4().hex
        async with self.db.transaction(write=True) as tx:
            user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if self.db.is_pg else "")
            await tx.fetchone(user_sql, (user_id,))
            await tx.execute(
                "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
                "market_slug,market_title,outcome,shares,trader_shares,entry_price,notional_usd,status,opened_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                (pid, user_id, action.trader_address, action.condition_id, action.token_id,
                 getattr(p, "slug", ""), getattr(p, "title", ""), action.outcome,
                 result.filled_shares, action.trader_shares, result.avg_price, spent, now_iso()))
            await tx.execute(
                "INSERT INTO trade_events(id,user_id,position_id,event_type,amount_usd,pnl,ts) "
                "VALUES(?,?,?,'open',?,NULL,?)",
                (uuid.uuid4().hex, user_id, pid, spent, now_iso()))
            deleted = await tx.execute(
                "DELETE FROM copy_open_claims WHERE user_id=? AND token_id=? AND claim_id=? "
                "AND state='submitting'", (user_id, action.token_id, action.claim_id))
            if deleted != 1:
                raise RuntimeError("BUY claim fencing token lost during open finalization")
        return spent

    async def _record_close(self, user_id, action, result) -> None:
        await self._close_row(user_id, action.row, result.avg_price, result.filled_shares)

    async def _record_resize(self, user_id, action, result) -> float:
        row = action.row
        if action.subkind == "increase":
            spent = round(result.filled_shares * result.avg_price, 2)
            async with self.db.transaction(write=True) as tx:
                user_sql = "SELECT id FROM users WHERE id=?" + (" FOR UPDATE" if self.db.is_pg else "")
                await tx.fetchone(user_sql, (user_id,))
                fresh = await tx.fetchone(
                    "SELECT * FROM copy_positions WHERE id=? AND status='open'", (row["id"],))
                if not fresh:
                    raise RuntimeError("position changed before resize persistence")
                new_shares = fresh["shares"] + result.filled_shares
                new_notional = fresh["notional_usd"] + spent
                new_entry = ((fresh["entry_price"] * fresh["shares"]
                              + result.avg_price * result.filled_shares) / new_shares)
                changed = await tx.execute(
                    "UPDATE copy_positions SET shares=?,notional_usd=?,entry_price=?,trader_shares=? "
                    "WHERE id=? AND status='open'",
                    (new_shares, new_notional, new_entry, action.trader_shares, fresh["id"]))
                if changed != 1:
                    raise RuntimeError("resize persistence lost position race")
                await tx.execute(
                    "INSERT INTO trade_events(id,user_id,position_id,event_type,amount_usd,pnl,ts) "
                    "VALUES(?,?,?,'partial',?,NULL,?)",
                    (uuid.uuid4().hex, user_id, fresh["id"], spent, now_iso()))
                deleted = await tx.execute(
                    "DELETE FROM copy_open_claims WHERE user_id=? AND token_id=? AND claim_id=? "
                    "AND state='submitting'", (user_id, action.token_id, action.claim_id))
                if deleted != 1:
                    raise RuntimeError("BUY claim fencing token lost during resize finalization")
            return spent
        else:  # decrease — sold some shares
            sold = result.filled_shares
            pnl = (result.avg_price - row["entry_price"]) * sold
            new_shares = max(0.0, row["shares"] - sold)
            frac_left = new_shares / row["shares"] if row["shares"] else 0.0
            async with self.db.transaction(write=True) as tx:
                changed = await tx.execute(
                    "UPDATE copy_positions SET shares=?,notional_usd=?,trader_shares=?,status='open' "
                    "WHERE id=? AND user_id=? AND status='closing'",
                    (new_shares, row["notional_usd"] * frac_left,
                     action.trader_shares, row["id"], user_id))
                if changed != 1:
                    raise RuntimeError("resize SELL finalization lost closing fence")
                await self._event(user_id, row["id"], "partial", None, pnl, store=tx)
            return 0.0

    async def _realize_resolution(self, user_id, action) -> None:
        """Market resolved: realize PnL from the resolution price (~1 if won, ~0 if
        lost). The on-chain CTF redeem is a separate flow finalized in phase 10."""
        row, p = action.row, action.position
        await self._close_row(user_id, row, p.cur_price, row["shares"],
                              event_type="resolve", status="resolved")

    async def _resolve_departed(self, user_id: str, row: dict) -> None:
        """Finalize a position whose market died before we could exit (resolved
        and possibly auto-redeemed). The winning TOKEN comes from Gamma's
        resolved outcome prices — NOT from the wallet's REDEEM records, which
        are per-condition and can't tell the sides apart when both were held
        (seen live 2026-07-03: matching on conditionId marked losing sides of
        both-sides copies as $1 winners). Redeem records remain the fallback
        when Gamma doesn't know the market."""
        exit_price = None
        try:
            prices = await self.pm.get_resolved_prices(row["condition_id"])
            if row["token_id"] in prices:
                exit_price = 1.0 if prices[row["token_id"]] >= 0.5 else 0.0
        except Exception:
            log.exception("gamma outcome lookup failed for %s", row["id"])
        if exit_price is None:
            try:
                redeems = await self.pm.get_redeems(user_id)
                paid = sum(float(r.get("usdcSize", 0) or 0) for r in redeems
                           if r.get("conditionId") == row["condition_id"])
                # per-condition only: correct when we held one side; ambiguous
                # for both-sides copies (gamma path above covers those)
                exit_price = 1.0 if paid > 0 else 0.0
            except Exception:
                log.exception("redeem lookup failed for %s — assuming lost", row["id"])
                exit_price = 0.0
        await self._close_row(user_id, row, exit_price, row["shares"],
                              event_type="resolve", status="resolved")
        log.warning("position %s finalized post-resolution at %.0f (market died "
                    "before exit)", row["id"], exit_price)

    # --- shared persistence (used by both fast and reconcile paths) ---------
    async def _insert_open(self, user_id, trader_address, condition_id, token_id,
                           slug, title, outcome, shares, trader_shares, entry_price,
                           notional) -> str | None:
        pid = uuid.uuid4().hex
        try:
            await self.db.execute(
                "INSERT INTO copy_positions(id, user_id, trader_address, condition_id, "
                "token_id, market_slug, market_title, outcome, shares, trader_shares, "
                "entry_price, notional_usd, status, opened_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                (pid, user_id, trader_address, condition_id, token_id, slug, title,
                 outcome, shares, trader_shares, entry_price, notional, now_iso()))
        except aiosqlite.IntegrityError:
            log.info("open skipped (already open): %s %s", user_id, token_id)
            return None
        await self._event(user_id, pid, "open", notional, None)
        return pid

    async def _close_row(self, user_id, row, exit_price, filled_shares,
                         *, event_type="close", status="closed") -> None:
        pnl = (exit_price - row["entry_price"]) * filled_shares
        async with self.db.transaction(write=True) as tx:
            changed = await tx.execute(
                "UPDATE copy_positions SET status=?,exit_price=?,realized_pnl=?,closed_at=? "
                "WHERE id=? AND user_id=? AND status='closing'",
                (status, exit_price, pnl, now_iso(), row["id"], user_id))
            if changed != 1:
                raise RuntimeError("full SELL finalization lost closing fence")
            await self._event(
                user_id, row["id"], event_type, row["notional_usd"], pnl, store=tx)

    async def _event(self, user_id, position_id, event_type, amount_usd, pnl,
                     *, store=None) -> None:
        store = store or self.db
        inserted = await store.execute(
            "INSERT INTO trade_events(id, user_id, position_id, event_type, amount_usd, pnl, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, user_id, position_id, event_type, amount_usd, pnl, now_iso()))
        if inserted != 1:
            raise RuntimeError("trade event insertion did not affect exactly one row")

    # --- per-wallet risk settings -----------------------------------------
    @staticmethod
    def _follow_risk(follow: dict) -> dict:
        """Effective risk/sizing settings for one copied wallet (NULL = default)."""
        def _f(key, default):
            v = follow.get(key)
            return float(v) if v is not None else default

        slip = follow.get("max_slippage_pct")
        exp = follow.get("max_total_exposure_usd")
        lim = follow.get("daily_loss_limit_usd")
        mo = follow.get("max_open_positions")
        return {
            "paused": bool(follow.get("paused")),
            "slippage": validate_slippage_pct(
                slip if slip is not None else MAX_COPY_SLIPPAGE_PCT,
                "followed_traders.max_slippage_pct"),
            "max_exposure": float(exp) if exp is not None else None,
            "daily_limit": float(lim) if lim is not None else None,
            # ratio-of-leader sizing + entry filters (screenshot settings).
            # Fallbacks here MUST match the WalletRiskCard UI defaults so a
            # never-touched slider shows the exact number the engine enforces.
            "ratio_pct": _f("copy_ratio_pct", DEFAULT_COPY_RATIO_PCT),
            "max_per_trade": _f("max_position_usd", DEFAULT_MAX_POSITION_USD),
            "min_leader": _f("min_leader_usd", 0.0),
            "ignore_below": _f("ignore_below_usd", DEFAULT_IGNORE_BELOW_USD),
            "max_open": int(mo) if mo is not None else None,   # NULL/0 = unlimited
            "min_price": _f("min_price", DEFAULT_MIN_PRICE),
            "max_price": _f("max_price", DEFAULT_MAX_PRICE),
        }

    async def _opens_blocked(self, user_id: str, trader_address: str,
                             daily_limit: float | None, store=None) -> bool:
        """True if today's realized loss on THIS trader's copies hit the limit."""
        if daily_limit is None:
            return False
        start = dt.datetime.now(dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()
        store = store or self.db
        val = await store.fetchval(
            "SELECT COALESCE(SUM(e.pnl), 0) FROM trade_events e "
            "JOIN copy_positions p ON p.id = e.position_id "
            "WHERE p.user_id = ? AND p.trader_address = ? AND e.pnl IS NOT NULL AND e.ts >= ?",
            (user_id, trader_address, start))
        return float(val or 0.0) <= -daily_limit

    # --- default collaborators (overridable for tests) --------------------
    async def _get_client(self, user: dict):
        cid = user["id"]
        if cid not in self._clients:
            self._clients[cid] = await self._client_factory(user)
        return self._clients[cid]

    async def _default_client_factory(self, user: dict):
        pk = wallet.decrypt_private_key(user["private_key_enc"], ENCRYPTION_SECRET)
        return await wallet.make_clob_client(pk, funder=user["id"])

    async def _default_collateral(self, client) -> float:
        bal = await client.get_balance_allowance(asset_type="COLLATERAL")
        return bal.balance / 1e6
