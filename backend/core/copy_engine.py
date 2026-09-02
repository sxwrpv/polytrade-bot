"""Copy engine — polls watched trader wallets and mirrors their positions.

Runs as an asyncio background task. Each tick, per (user, followed trader):
  1. fetch the trader's live positions (data-api)
  2. diff them against the user's OPEN copy_positions rows **in the DB**
     (the source of truth — so a restart never re-opens a held position)
  3. emit intents — OPEN / CLOSE / RESIZE / RESOLVE — and execute them

Sizing (ratio-of-leader, owner model 2026-07-06): each copy mirrors the
LEADER's own dollar position — notional = leader_position_value × copy_ratio_pct%
— clamped by MAX/TRADE (max_position_usd), available collateral, the per-trader
exposure cap, and finally the pre-submission verified-wallet gate.

`plan_actions` is pure (no IO) and exhaustively tested. The engine's collaborators
(client factory, order placement, collateral lookup) are injectable so the whole
diff→intent→persist path is verifiable without funds.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import aiosqlite
import httpx
from polymarket import errors as pm_errors

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
from backend.core.health import heartbeats
from backend.core.polymarket import Position
from backend.db.database import now_iso

log = logging.getLogger("copy_engine")

# Fallback minimum only — used when a caller supplies no per-wallet floor.
# The live minimum is per copied wallet (followed_traders.ignore_below_usd, the
# "IGNORE POSITIONS < $" slider); see _clamp_to_verified_position.
MIN_NOTIONAL_USD = 1.0
RESIZE_THRESHOLD = 0.25      # rebalance only when target drifts >25% from current
# Fill-or-kill budget: how many consecutive non-fills before an intent is
# abandoned. The reconcile tick (COPY_ENGINE_POLL_SECONDS, 5s in production)
# supplies the spacing, so this is ~3 tries over ~15s.
MAX_FILL_ATTEMPTS = 3
# Reconciliation age gates: long enough for the data-api indexer to reflect a
# fill (uncertain BUYs) and for any legitimately in-flight close to finish
# (stuck closings) before the wallet is treated as ground truth.
UNCERTAIN_CLAIM_MIN_AGE_SECONDS = 180.0
CLOSING_STUCK_MIN_AGE_SECONDS = 600.0
# How long a submitted BUY keeps counting against this token's cap even when
# the exchange reported failure and the indexer has not shown the shares yet.
#
# Why this exists (incident 2026-08-23): three BUYs of ~$15.54 went out for one
# token in 13 seconds against a $15 cap. Each attempt was reported as failed,
# so the claim was released and nothing was written; the retry budget
# (MAX_FILL_ATTEMPTS) then spent itself, and every attempt sized itself fresh
# because the two guards that should have stopped it were both blind:
# `row_basis` is 0 until a position row exists, and `wallet_cost` comes from an
# indexer that lags a fill by seconds. All three orders actually filled.
#
# So a submitted notional is remembered here and counted as basis until the
# indexer catches up. It is deliberately NOT a reclassification of the failure
# as uncertain — doing that froze six tokens behind unreconciled claims on
# 2026-07-11. This bounds the damage without touching that classification.
SUBMITTED_BASIS_TTL_SECONDS = 120.0
# A leader trade older than this is not worth copying: the price that made it
# worth mirroring is gone. leader_age was computed and logged since the fast
# path was written but never enforced, so a trade detected late — after a
# restart, a detector stall, or a funding change — was copied at whatever the
# book said hours later (incident 2026-08-23).
MAX_LEADER_TRADE_AGE_SECONDS = float(
    os.environ.get("MAX_LEADER_TRADE_AGE_SECONDS", "300"))

# Transport failures on a CACHED SDK client. The client keeps one long-lived
# HTTP/2 connection, and the edge closes it with a clean GOAWAY after 10,000
# streams (observed 7x in 3.4 days, always `last_stream_id:19999`). The SDK
# surfaces that as an exception on whatever read happened to be in flight,
# and the cached client stays poisoned until the process restarts — so a
# single connection recycle silently cost us every copy decision that tick.
#
# These are retried ONLY for idempotent reads (balance/positions/activity),
# and only after the client is rebuilt. Order submission is never retried
# here: execution.py owns that, and an ambiguous submission must reconcile
# rather than re-fire.
# How long an available-collateral reading may be reused across copy
# decisions for one user.
#
# _handle_leader_trade reads the balance for EVERY detected leader trade, via
# an authenticated CLOB round-trip, and does it before the dust-floor check.
# In the 30 Aug - 2 Sep window that was ~14,300 reads of which 10,126 were
# immediately discarded by that check, and it is the traffic that walks a
# connection into its 10,000-stream recycle.
#
# Reusing a reading is safe in the direction that matters. The balance only
# falls when WE spend, and every submission invalidates the entry
# (_note_submitted), so a cached value can only be stale-low -- which
# under-sizes, never over-sizes. An external withdrawal inside the window is
# the one gap, and it is still caught downstream: _prepare_buy re-derives
# every cap in its write transaction, _clamp_to_verified_position re-reads the
# wallet, and the exchange rejects on not_enough_balance.
COLLATERAL_CACHE_SECONDS = float(
    os.environ.get("COLLATERAL_CACHE_SECONDS", "5"))

CLIENT_TRANSPORT_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    pm_errors.TransportError,
    pm_errors.ConnectionLostError,
    pm_errors.TimeoutError,
)


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
    available_collateral: float,
    *,
    min_notional: float = MIN_NOTIONAL_USD,
    resize_threshold: float = RESIZE_THRESHOLD,
    max_total_exposure: float | None = None,
    block_opens: bool = False,
    ratio_pct: float = DEFAULT_COPY_RATIO_PCT,
    max_per_trade: float | None = None,
    min_leader: float = 0.0,
    ignore_below: float | None = None,
    max_open: int | None = None,
    min_price: float = DEFAULT_MIN_PRICE,
    max_price: float = DEFAULT_MAX_PRICE,
    positions_complete: bool = True,
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

    # trader exited (token no longer held) → close ours. Only when the fetched
    # position list is COMPLETE: absence from a truncated page proves nothing
    # (whale leaders hold 500+ positions; closing on a truncated diff force-
    # sold copies the leader still held).
    if positions_complete:
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
                 risk_lock: asyncio.Lock | None = None,
                 position_notifier=None) -> None:
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
        self._position_notifier = position_notifier
        self._clients: dict[str, object] = {}
        # fast-detection cursors / dedupe, per (user_id, trader_address).
        # _seen values are insertion-ordered dicts used as bounded sets.
        self._cursors: dict[tuple, int] = {}
        self._seen: dict[tuple, dict] = defaultdict(dict)
        # Fill-or-kill attempt budget. plan_actions is stateless — it re-derives
        # the same intent from live positions every tick — so an order that
        # cannot fill was retried forever (observed: a resize needing >2% over
        # mark on a ~10%-spread book, re-attempted every 5s indefinitely,
        # burning a geoblock probe + orderbook read each time against an API
        # that already 429s). Track consecutive non-fills and give up.
        # Value: [attempts, intent_fingerprint]
        self._attempts: dict[tuple, list] = {}
        # (user_id, token_id) -> [notional_submitted, monotonic_deadline]. Held
        # in memory on purpose: it guards a burst of retries within seconds, and
        # a process restart re-reads the wallet anyway.
        self._submitted: dict[tuple[str, str], list] = {}
        # (user_id, trader) -> tokens the leader ALREADY held when copying
        # started. The reconciler mirrors a leader's whole current book, so
        # without this a new follow back-fills positions opened long before —
        # which is exactly what happened on 2026-08-23, and the opposite of
        # what the UI promises. Mirrors how _cursors seeds the detector
        # ("first sight: start now, don't retro-copy the leader's history").
        # In memory on purpose: after a restart the safe answer is the same one.
        self._no_backfill: dict[tuple[str, str], set] = {}
        # user_id -> [available_usd, monotonic_deadline]. See
        # COLLATERAL_CACHE_SECONDS. In memory on purpose: a restart re-reads.
        self._collateral_cache: dict[str, list] = {}

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

    # --- uncertain-claim / stuck-closing reconciliation ---------------------
    async def _reconcile_uncertain_claims(self) -> None:
        """Settle BUY claims parked in 'uncertain' (submission outcome unknown).
        A wallet holding may prove a bounded fill and be adopted. Absence is not
        proof of non-fill, and an aggregate larger than the reserved claim is
        quarantined. This intentionally prefers a stuck token requiring manual
        review over another BUY that can breach the configured cap."""
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=UNCERTAIN_CLAIM_MIN_AGE_SECONDS)).isoformat()
        claims = await self.db.fetchall(
            "SELECT * FROM copy_open_claims WHERE state='uncertain' AND updated_at < ?",
            (cutoff,))
        by_user: dict[str, list[dict]] = defaultdict(list)
        for c in claims:
            by_user[c["user_id"]].append(c)
        for user_id, user_claims in by_user.items():
            try:
                positions, complete = await self.pm.get_all_positions(
                    user_id, size_threshold=0)
            except Exception:
                log.exception("uncertain reconciliation: position read failed for %s",
                              user_id[:10])
                continue
            held = {p.asset: p for p in positions if p.size > 0.01}
            for claim in user_claims:
                # Absence from a truncated wallet page proves nothing. Releasing
                # this claim would permit another BUY while the first may already
                # be held beyond the page boundary.
                if claim["token_id"] not in held and not complete:
                    log.warning("uncertain claim retained: wallet scan incomplete for %s %s",
                                user_id[:10], claim["token_id"])
                    continue
                try:
                    await self._settle_uncertain_claim(
                        user_id, claim, held.get(claim["token_id"]))
                except Exception:
                    log.exception("uncertain claim settlement failed: %s %s",
                                  user_id[:10], claim["token_id"])

    async def _settle_uncertain_claim(self, user_id: str, claim: dict, p) -> None:
        token = claim["token_id"]
        row = await self.db.fetchone(
            "SELECT * FROM copy_positions WHERE user_id=? AND token_id=? "
            "AND status IN ('open','closing','reconciliation_required')",
            (user_id, token))
        if claim.get("action") == "open":
            if row is not None:
                # a tracked row accounts for the shares — nothing left to adopt
                log.warning("uncertain OPEN claim released (tracked row exists): %s %s",
                            user_id[:10], token)
                await self._release_buy_claim(user_id, token, claim["claim_id"])
            elif p is None:
                # Data-api absence is not authoritative proof of non-fill.
                # Retain the durable fence for operator reconciliation.
                log.warning("uncertain OPEN claim retained (no authoritative fill status): %s %s",
                            user_id[:10], token)
            else:
                await self._adopt_uncertain_fill(user_id, claim, p)
            return
        # resize-increase: realign the row only when a bounded extra holding is
        # visible. Any inconclusive or oversized result retains the fence.
        if row is None or p is None:
            log.warning("uncertain RESIZE claim retained (row/holding inconclusive): %s %s",
                        user_id[:10], token)
            return
        extra = float(p.size) - float(row["shares"])
        if row["status"] == "open" and extra > 0.01:
            spent = round(extra * float(p.avg_price or 0), 2)
            reserved = float(claim.get("reserved_usd") or 0)
            tolerance = max(0.25, reserved * 0.02)
            if spent > reserved + tolerance:
                log.critical("uncertain RESIZE quarantined: observed $%.2f exceeds $%.2f claim for %s %s",
                             spent, reserved, user_id[:10], token)
                return
            async with self.db.transaction(write=True) as tx:
                deleted = await tx.execute(
                    "DELETE FROM copy_open_claims WHERE user_id=? AND token_id=? "
                    "AND claim_id=? AND state='uncertain'",
                    (user_id, token, claim["claim_id"]))
                if deleted != 1:
                    return   # settled elsewhere
                changed = await tx.execute(
                    "UPDATE copy_positions SET shares=?,entry_price=?,notional_usd=? "
                    "WHERE id=? AND status='open'",
                    (float(p.size), float(p.avg_price),
                     round(float(p.size) * float(p.avg_price), 2), row["id"]))
                if changed != 1:
                    raise RuntimeError("uncertain resize adoption lost position race")
                await self._event(user_id, row["id"], "partial", spent, None, store=tx)
            log.warning("uncertain RESIZE adopted from wallet: %s %s +%.2f shares",
                        user_id[:10], token, extra)
            await self._notify_position({
                "event": "increased", "user_id": user_id, "position_id": row["id"],
                "market_title": row.get("market_title", ""),
                "market_slug": row.get("market_slug", ""),
                "outcome": row.get("outcome", ""),
                "shares": extra, "entry_price": float(p.avg_price),
                "notional_usd": spent, "total_shares": float(p.size),
                "trader_address": row.get("trader_address"),
            })
        else:
            log.warning("uncertain RESIZE claim retained (no authoritative non-fill): %s %s",
                        user_id[:10], token)

    async def _adopt_uncertain_fill(self, user_id: str, claim: dict, p) -> None:
        """The BUY behind an uncertain claim demonstrably filled (the wallet
        holds the token and no tracking row explains it): book it as an open
        copy from the wallet snapshot and clear the claim atomically."""
        pid = uuid.uuid4().hex
        notional = round(float(p.size) * float(p.avg_price or 0), 2)
        reserved = float(claim.get("reserved_usd") or 0)
        tolerance = max(0.25, reserved * 0.02)
        if notional > reserved + tolerance:
            # Never attribute an aggregate/manual/duplicate holding larger than
            # the submitted budget to one copy claim. Keep it quarantined so it
            # cannot be retried or later auto-sold as engine-owned inventory.
            log.critical("uncertain BUY quarantined: observed $%.2f exceeds $%.2f claim for %s %s",
                         notional, reserved, user_id[:10], claim["token_id"])
            return
        async with self.db.transaction(write=True) as tx:
            user_sql = "SELECT id FROM users WHERE id=?" + (
                " FOR UPDATE" if self.db.is_pg else "")
            await tx.fetchone(user_sql, (user_id,))
            deleted = await tx.execute(
                "DELETE FROM copy_open_claims WHERE user_id=? AND token_id=? "
                "AND claim_id=? AND state='uncertain'",
                (user_id, claim["token_id"], claim["claim_id"]))
            if deleted != 1:
                return   # settled elsewhere
            await tx.execute(
                "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,token_id,"
                "market_slug,market_title,outcome,shares,entry_price,notional_usd,status,opened_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                (pid, user_id, claim["trader_address"], p.condition_id, claim["token_id"],
                 p.slug, p.title, (p.outcome or "").upper(), float(p.size),
                 float(p.avg_price), notional, now_iso()))
            await self._event(user_id, pid, "open", notional, None, store=tx)
        log.warning("uncertain BUY adopted from wallet: %s %s %.2f shares @ %.4f",
                    user_id[:10], claim["token_id"], p.size, p.avg_price)
        await self._notify_position({
            "event": "opened", "user_id": user_id, "position_id": pid,
            "market_title": p.title, "market_slug": p.slug,
            "outcome": (p.outcome or "").upper(), "shares": float(p.size),
            "entry_price": float(p.avg_price), "notional_usd": notional,
            "trader_address": claim["trader_address"],
        })

    async def _recover_stuck_closings(self) -> None:
        """Recover rows stuck mid-close: a 'closing' fence left behind by a
        crash or an uncertain SELL previously froze the position forever (the
        engine only manages 'open' rows and manual close 404s on 'closing').
        Age-gated so a legitimately in-flight close is never touched. Wallet =
        ground truth: shares still held means no SELL filled -> reopen; shares
        gone means the SELL (or a resolution) happened -> finalize from the
        user's own fill history / resolved price."""
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=CLOSING_STUCK_MIN_AGE_SECONDS)).isoformat()
        rows = await self.db.fetchall(
            "SELECT * FROM copy_positions WHERE status='closing' "
            "AND (closing_at IS NULL OR closing_at < ?)", (cutoff,))
        by_user: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_user[r["user_id"]].append(r)
        for user_id, user_rows in by_user.items():
            try:
                positions = await self.pm.get_positions(user_id, size_threshold=0)
            except Exception:
                log.exception("stuck-closing recovery: position read failed for %s",
                              user_id[:10])
                continue
            held = {p.asset: p for p in positions if p.size > 0.01}
            for row in user_rows:
                try:
                    await self._settle_stuck_closing(user_id, row,
                                                     held.get(row["token_id"]))
                except Exception:
                    log.exception("stuck-closing recovery failed for %s", row["id"])

    async def _settle_stuck_closing(self, user_id: str, row: dict, p) -> None:
        if p is not None and p.redeemable:
            exit_price = 1.0 if (p.cur_price or 0) >= 0.5 else 0.0
            await self._close_row(user_id, row, exit_price, row["shares"],
                                  event_type="resolve", status="resolved")
            log.warning("stuck closing row %s resolved at %.0f", row["id"], exit_price)
        elif p is not None and float(p.size) >= float(row["shares"]) - 0.01:
            # every share still in the wallet -> the SELL never filled
            if await self.db.try_transition(row["id"], "closing", "open"):
                log.warning("stuck closing row %s reopened (wallet still holds "
                            "the shares)", row["id"])
        elif p is None:
            await self._finalize_departed_closing(user_id, row)
        else:
            log.warning("stuck closing row %s: wallet holds %.2f of %.2f shares — "
                        "leaving for manual review", row["id"], p.size, row["shares"])

    async def _finalize_departed_closing(self, user_id: str, row: dict) -> None:
        """The wallet no longer holds a stuck-closing token: either the market
        resolved (finalize at the resolved price) or the SELL actually filled
        (finalize at the real exit price from the user's own fill history)."""
        try:
            prices = await self.pm.get_resolved_prices(row["condition_id"])
        except Exception:
            prices = {}
        if row["token_id"] in prices:
            await self._close_row(user_id, row, prices[row["token_id"]], row["shares"],
                                  event_type="resolve", status="resolved")
            log.warning("stuck closing row %s finalized at resolved price %.0f",
                        row["id"], prices[row["token_id"]])
            return
        trades = await self.pm.get_trade_history(user_id, limit=100)
        since = None
        if row.get("closing_at"):
            try:   # small grace window: fills can be stamped just before the fence
                since = dt.datetime.fromisoformat(row["closing_at"]).timestamp() - 60
            except ValueError:
                since = None
        sells = [t for t in trades
                 if t.asset == row["token_id"] and t.side.upper() == "SELL"
                 and (since is None or t.timestamp >= since)]
        if not sells:
            log.warning("stuck closing row %s: shares gone but no SELL fill found — "
                        "retrying next tick", row["id"])
            return
        sold = sum(t.size for t in sells)
        avg = sum(t.size * t.price for t in sells) / sold if sold else 0.0
        await self._close_row(user_id, row, avg, min(sold, float(row["shares"])))
        log.warning("stuck closing row %s finalized from fill history: %.2f sh @ %.4f",
                    row["id"], sold, avg)

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
        self._collateral_cache.clear()

    async def _loop(self, fn, interval: float, stop_event: asyncio.Event) -> None:
        name = getattr(fn, "__name__", str(fn)).strip("_")
        heartbeats.register(name, interval)
        while not stop_event.is_set():
            try:
                await fn()
                # A pass that completed is the only evidence the loop is alive;
                # /api/health could not tell a running engine from a dead one.
                heartbeats.mark(name)
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
            match = None
            try:
                if trade.condition_id:   # targeted read — fast and truncation-proof
                    tpos = await self.pm.get_positions(
                        trader, size_threshold=0, market=trade.condition_id)
                else:
                    tpos = await self.pm.get_positions(trader, size_threshold=0)
                match = next((p for p in tpos if p.asset == token), None)
                if match and match.size > 0:
                    trader_total = match.size
                    leader_price = match.cur_price or leader_price
            except Exception:
                log.exception("trader position lookup failed; using trade size")
            leader_notional = trader_total * (leader_price or float(trade.price or 0))
            # The on-chain detector's OrderFilled events carry NO market
            # metadata — fill condition/outcome/slug/title from the leader's
            # indexed position, or the row is booked blind and can never be
            # matched to its resolution later.
            condition_id = trade.condition_id or getattr(match, "condition_id", "") or ""
            outcome = (trade.outcome or getattr(match, "outcome", "") or "").upper()
            slug = trade.slug or getattr(match, "slug", "") or ""
            title = trade.title or getattr(match, "title", "") or ""

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
            available, client = await self._read_collateral(user, client)
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
            if leader_age > MAX_LEADER_TRADE_AGE_SECONDS:
                # Copying this now means entering at a price the leader never
                # paid, on a decision they made long enough ago that the edge
                # is gone. Skip rather than chase.
                log.info(
                    "fast-open skipped %s age=%.1fs (>%.0fs) reason=leader_trade_too_old trader=%s",
                    token, leader_age, MAX_LEADER_TRADE_AGE_SECONDS, trader[:10])
                return
            log.info(
                "fast-open candidate %s age=%.1fs side=%s notional=%.2f ref=%.4f trader=%s",
                token, leader_age, trade.side.upper(), notional, float(trade.price or 0), trader[:10])
            action = Action(
                kind="open", token_id=token, condition_id=condition_id,
                outcome=outcome, side="BUY", amount=notional,
                notional_usd=notional, reference_price=trade.price,
                trader_shares=trader_total,
                position=SimpleNamespace(
                    proxy_wallet=trader, asset=token, condition_id=condition_id,
                    size=trader_total, avg_price=float(trade.price or leader_price),
                    cur_price=leader_price, current_value=leader_notional,
                    redeemable=False, outcome=outcome, slug=slug,
                    title=title),
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
                    reference_price=trade.price,
                    max_slippage_pct=frisk["slippage"])
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
                await self._notify_position({
                    "event": "reduced", "user_id": user_id,
                    "position_id": existing["id"],
                    "market_title": existing.get("market_title", ""),
                    "market_slug": existing.get("market_slug", ""),
                    "outcome": existing.get("outcome", ""),
                    "shares": sold, "exit_price": result.avg_price,
                    "realized_pnl": pnl, "total_shares": new_shares,
                    "trader_address": existing.get("trader_address"),
                })
            elif not getattr(result, "submission_uncertain", False):
                await self.db.try_transition(existing["id"], "closing", "open")

    async def _reconcile_tick(self) -> None:
        try:
            # Also catches a process that restarted before a submitting claim
            # crossed the startup age gate; startup-only recovery can miss it.
            await self._recover_stale_claims()
        except Exception:
            log.exception("stale-claim recovery failed (continuing)")
        try:
            await self._reconcile_uncertain_claims()
        except Exception:
            log.exception("uncertain-claim reconciliation failed (continuing)")
        try:
            await self._recover_stuck_closings()
        except Exception:
            log.exception("stuck-closing recovery failed (continuing)")
        follows = await self.db.fetchall(
            "SELECT * FROM followed_traders WHERE is_active = 1")
        # Unfollow must not orphan open copies: deactivated follows whose
        # positions are still open keep being managed exactly like a paused
        # follow (closes/resolves/resize-downs run; opens stay blocked).
        follows += await self.db.fetchall(
            "SELECT f.* FROM followed_traders f WHERE f.is_active = 0 AND EXISTS("
            "SELECT 1 FROM copy_positions p WHERE p.user_id = f.user_id "
            "AND p.trader_address = f.trader_address AND p.status = 'open')")
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
        available, client = await self._read_collateral(user, client)
        # Capital at risk, not just settled positions: a row mid-close still
        # holds real shares, and a reconciliation_required row may represent an
        # order that DID fill. The fast-detection path already counted all
        # three; counting only 'open' here under-reported exposure, so the same
        # wallet produced two different numbers depending on which path ran and
        # MAX EXPOSURE could be exceeded via the reconciler. Single definition.
        open_rows_all = await self.db.fetchall(
            "SELECT * FROM copy_positions WHERE user_id = ? "
            "AND status IN ('open','closing','reconciliation_required')",
            (user_id,))

        for follow in follows:
            frisk = self._follow_risk(follow)
            trader = follow["trader_address"]
            inactive = not follow.get("is_active")
            open_rows = [r for r in open_rows_all if r["trader_address"] == trader]
            # PAUSE means "no new buys" — NOT "abandon the positions already
            # bought with the user's money". A paused follow with open rows
            # still gets closes/resolutions/resize-downs (block_opens
            # suppresses opens and resize-ups); only a paused follow with
            # nothing open is skipped entirely. (Surfaced live 2026-07-03:
            # the owner paused a wallet and its resolved positions sat
            # unmanaged forever.) Unfollowed (is_active=0) follows with open
            # rows are managed the same way — exit-only.
            if (frisk["paused"] or inactive) and not open_rows:
                continue
            positions, complete = await self.pm.get_all_positions(trader)
            # Backfill market metadata for rows opened blind by the on-chain
            # fast path (OrderFilled events carry none): without condition_id
            # a dead-market position can never be matched to its resolution.
            by_token = {p.asset: p for p in positions}
            for r in open_rows:
                p = by_token.get(r["token_id"])
                if p is not None and (not r.get("condition_id") or not r.get("market_title")):
                    await self.db.execute(
                        "UPDATE copy_positions SET condition_id=?, market_title=?, "
                        "market_slug=?, outcome=? WHERE id=? AND status='open'",
                        (p.condition_id, p.title, p.event_slug or p.slug,
                         (r.get("outcome") or p.outcome or "").upper(), r["id"]))
                    r.update(condition_id=p.condition_id, market_title=p.title)
            # A BUY the exchange reported as failed may still have filled. That
            # leaves shares in the wallet with no row, no claim and no alert —
            # the position is invisible and will never be managed or exited
            # (incident 2026-08-23). We only adopt what we can PROVE we
            # submitted for, so a user's own manual trades are never swept up.
            adopted = await self._adopt_untracked_submissions(
                user_id, trader, positions, open_rows)
            if adopted:
                open_rows = [r for r in await self.db.fetchall(
                    "SELECT * FROM copy_positions WHERE user_id=? AND status='open'",
                    (user_id,)) if r["trader_address"] == trader]

            block_opens = frisk["paused"] or inactive or await self._opens_blocked(
                user_id, trader, frisk["daily_limit"])

            # First sight of this follow: everything the leader is holding right
            # now predates us, so none of it may be opened. Tokens drop out of
            # the set once the leader exits them, so a genuine RE-entry later is
            # copied normally.
            key = (user_id, trader)
            held_now = {p.asset for p in positions if p.size > 0}
            if key not in self._no_backfill:
                self._no_backfill[key] = set(held_now)
                if held_now:
                    log.info("no-backfill seeded: %s %s holds %d position(s) that "
                             "predate copying", user_id[:10], trader[:10], len(held_now))
            else:
                self._no_backfill[key] &= held_now
            preexisting = self._no_backfill[key]

            actions = plan_actions(
                positions, open_rows, follow, available,
                max_total_exposure=frisk["max_exposure"], block_opens=block_opens,
                ratio_pct=frisk["ratio_pct"], max_per_trade=frisk["max_per_trade"],
                min_leader=frisk["min_leader"], ignore_below=frisk["ignore_below"],
                max_open=frisk["max_open"], min_price=frisk["min_price"],
                max_price=frisk["max_price"],
                positions_complete=complete)
            for action in actions:
                if action.kind == "open" and action.token_id in preexisting:
                    log.info("reconcile open skipped (predates copying): %s %s %s",
                             user_id[:10], trader[:10], action.token_id[:16])
                    continue
                action.trader_address = trader
                spent = await self._execute(user_id, client, action, slippage=frisk["slippage"])
                if action.side == "BUY":
                    available = max(0.0, available - spent)

    async def _adopt_untracked_submissions(
            self, user_id: str, trader: str, positions, open_rows) -> int:
        """Rescue shares that filled from a BUY reported as failed.

        Scope is deliberately narrow. Only tokens with a live _submitted record
        qualify — that is proof this engine put an order on the wire for them
        within the TTL. A holding we cannot tie to our own submission is the
        user's own trade and is left alone.

        Returns the number of rows created.
        """
        tracked = {r["token_id"] for r in open_rows}
        rescued = 0
        for p in positions:
            token = p.asset
            if p.size <= 0.01 or token in tracked:
                continue
            if self._submitted_basis(user_id, token) <= 0:
                continue
            claim = await self.db.fetchone(
                "SELECT claim_id FROM copy_open_claims WHERE user_id=? AND token_id=?",
                (user_id, token))
            if claim:
                continue          # the uncertain-claim path owns this one
            notional = round(float(p.size) * float(p.avg_price or 0), 2)
            pid = uuid.uuid4().hex
            try:
                async with self.db.transaction(write=True) as tx:
                    user_sql = "SELECT id FROM users WHERE id=?" + (
                        " FOR UPDATE" if self.db.is_pg else "")
                    await tx.fetchone(user_sql, (user_id,))
                    await tx.execute(
                        "INSERT INTO copy_positions(id,user_id,trader_address,condition_id,"
                        "token_id,market_slug,market_title,outcome,shares,entry_price,"
                        "notional_usd,status,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                        (pid, user_id, trader, p.condition_id, token,
                         p.event_slug or p.slug, p.title, (p.outcome or "").upper(),
                         float(p.size), float(p.avg_price), notional, now_iso()))
                    await self._event(user_id, pid, "open", notional, None, store=tx)
            except aiosqlite.IntegrityError:
                continue          # another worker rescued it first
            self._clear_submitted(user_id, token)
            rescued += 1
            log.warning("ADOPTED untracked fill: %s %s %.2f shares @ %.4f ($%.2f) — a "
                        "BUY reported as failed had actually filled",
                        user_id[:10], token[:16], p.size, p.avg_price, notional)
            await self._notify_position({
                "event": "opened", "user_id": user_id, "position_id": pid,
                "market_title": p.title, "market_slug": p.event_slug or p.slug,
                "outcome": (p.outcome or "").upper(), "shares": float(p.size),
                "entry_price": float(p.avg_price), "notional_usd": notional,
                "trader_address": trader,
            })
        return rescued

    # --- execution + persistence ------------------------------------------
    # --- fill-or-kill attempt budget --------------------------------------
    @staticmethod
    def _attempt_key(user_id: str, action: Action) -> tuple:
        return (user_id, action.token_id, action.kind, action.subkind)

    @staticmethod
    def _intent_fingerprint(action: Action) -> tuple:
        """Identity of *this* intent. When it changes the situation is new, so
        the budget re-arms — otherwise one temporarily-wide spread would freeze
        a position permanently, even after the leader moved again.

        Keyed on the LEADER's share count only. `action.amount` is deliberately
        excluded: _prepare_buy/_clamp_to_verified_position rewrite it from live
        collateral and MAX/TRADE headroom, so it drifts between ticks without
        the intent changing — including it would reset the budget every attempt
        and defeat the kill entirely.
        """
        return (round(action.trader_shares or 0.0, 4),)

    def _fill_budget_exhausted(self, user_id: str, action: Action) -> bool:
        key = self._attempt_key(user_id, action)
        state = self._attempts.get(key)
        if state is None:
            return False
        attempts, fingerprint = state
        if fingerprint != self._intent_fingerprint(action):
            self._attempts.pop(key, None)      # new intent -> fresh budget
            return False
        return attempts >= MAX_FILL_ATTEMPTS

    def _note_submitted(self, user_id: str, token_id: str, notional: float) -> None:
        """Remember a notional we actually put on the wire for this token."""
        key = (user_id, token_id)
        prior, deadline = self._submitted.get(key, [0.0, 0.0])
        now = time.monotonic()
        if deadline <= now:
            prior = 0.0
        self._submitted[key] = [prior + float(notional), now + SUBMITTED_BASIS_TTL_SECONDS]
        # money is on the wire — the cached balance is now stale-high
        self._invalidate_collateral(user_id)

    def _submitted_basis(self, user_id: str, token_id: str) -> float:
        """Notional submitted for this token inside the TTL, or 0."""
        entry = self._submitted.get((user_id, token_id))
        if not entry:
            return 0.0
        notional, deadline = entry
        if deadline <= time.monotonic():
            self._submitted.pop((user_id, token_id), None)
            return 0.0
        return float(notional)

    def _clear_submitted(self, user_id: str, token_id: str) -> None:
        """Called once the wallet or a tracked row can speak for the shares, so
        the remembered figure stops shadowing real basis."""
        self._submitted.pop((user_id, token_id), None)

    def _record_fill_outcome(self, user_id: str, action: Action, *,
                             filled: bool, reason: str = "") -> None:
        key = self._attempt_key(user_id, action)
        if filled:
            self._attempts.pop(key, None)
            return
        attempts, _ = self._attempts.get(key, (0, None))
        attempts += 1
        self._attempts[key] = [attempts, self._intent_fingerprint(action)]
        if attempts < MAX_FILL_ATTEMPTS:
            return
        # Budget spent. An abandoned ENTRY is a missed opportunity; an
        # abandoned EXIT means still holding something the leader already
        # left, so that one is escalated rather than logged quietly.
        is_exit = action.side == "SELL" or action.kind in ("close", "resolve")
        if is_exit:
            log.warning("KILLED after %d attempts — STILL HOLDING a position the "
                        "leader exited (%s %s): %s", attempts, action.kind,
                        action.token_id[:16], reason)
        else:
            log.info("killed after %d attempts (%s %s): %s", attempts,
                     action.kind, action.token_id[:16], reason)

    async def _execute(self, user_id: str, client, action: Action,
                       slippage: float = MAX_COPY_SLIPPAGE_PCT) -> float:
        if self._fill_budget_exhausted(user_id, action):
            return 0.0          # killed; re-arms if the leader's intent changes
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
            # An uncertain submission may still have reached the exchange, so it
            # is NOT a non-fill — the reconciler owns resolving it and must not
            # be starved of retries by the budget.
            if not getattr(result, "submission_uncertain", False):
                self._record_fill_outcome(user_id, action, filled=False,
                                          reason=result.reason)
            log.warning("order skipped (%s %s): %s", action.kind, action.token_id, result.reason)
            return 0.0
        self._record_fill_outcome(user_id, action, filled=True)
        self._invalidate_collateral(user_id)   # proceeds change the balance
        if action.kind == "close":
            await self._record_close(user_id, action, result)
        elif action.kind == "resize":
            recorded = await self._record_resize(user_id, action, result)
            return recorded
        return 0.0

    async def _execute_buy(self, user_id: str, client, action: Action) -> float:
        """Reserve, verify against the REAL wallet, fence, submit once, then
        atomically persist every BUY. The verify step (owner's call,
        2026-07-12: worth the extra read) re-reads the wallet's actual holding
        of the token pre-submission and clamps to MAX/TRADE headroom computed
        from that ground truth — DB bookkeeping drift (quote-recorded fills,
        adopted positions, anything untracked) can never grow a position past
        the cap. Fails CLOSED: if the wallet can't be read, the copy is
        skipped and the reconciler retries next tick."""
        async with self._risk_lock:
            prepared = await self._prepare_buy(user_id, action)
            if prepared is None:
                return 0.0
            action, risk = prepared
            clamped = await self._clamp_to_verified_position(user_id, action, risk)
            if clamped is None:
                await self._release_buy_claim(user_id, action.token_id, action.claim_id)
                return 0.0
            action = clamped
            if not await self._mark_claim_submitting(user_id, action):
                await self._release_buy_claim(user_id, action.token_id, action.claim_id)
                return 0.0
            # Recorded BEFORE the await, not after: if the process dies or the
            # call raises mid-flight the order may still have reached the
            # exchange, and the next attempt must size against it either way.
            self._note_submitted(user_id, action.token_id, action.amount)
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
                    # may have reached the exchange — reconciler owns it, so it
                    # must not consume the fill budget
                    await self._mark_claim_uncertain(user_id, action, result.reason)
                else:
                    self._record_fill_outcome(user_id, action, filled=False,
                                              reason=result.reason)
                    await self._release_buy_claim(user_id, action.token_id, action.claim_id)
                return 0.0
            self._record_fill_outcome(user_id, action, filled=True)
            try:
                if action.kind == "open":
                    return await self._record_open(user_id, action, result)
                return await self._record_resize(user_id, action, result)
            except Exception as exc:
                await self._mark_claim_uncertain(user_id, action, f"filled; persistence failed: {exc}")
                log.critical("BUY filled but persistence failed; claim retained: %s %s",
                             user_id[:10], action.token_id, exc_info=True)
                raise

    async def _wallet_position(self, user_id: str, token_id: str,
                               condition_id: str = "") -> tuple[float, float] | None:
        """(cost_usd, shares) the wallet REALLY holds for token_id, read fresh
        from the data-api. (0.0, 0.0) when the wallet provably doesn't hold it;
        None when it can't be determined (read failed, or the token wasn't in a
        truncated list) — callers must fail closed on None. With condition_id
        the read is a single market-filtered call (fast, truncation-proof);
        errors there fall back to the full scan before failing closed."""
        if condition_id:
            try:
                positions = await self.pm.get_positions(
                    user_id, size_threshold=0, market=condition_id)
                for p in positions:
                    if p.asset == token_id:
                        return float(p.size) * float(p.avg_price or 0), float(p.size)
                return 0.0, 0.0
            except Exception:
                log.exception("targeted wallet read failed for %s — falling back "
                              "to full scan", user_id[:10])
        try:
            positions, complete = await self.pm.get_all_positions(
                user_id, size_threshold=0)
        except Exception:
            log.exception("wallet position verify failed for %s", user_id[:10])
            return None
        for p in positions:
            if p.asset == token_id:
                return float(p.size) * float(p.avg_price or 0), float(p.size)
        return (0.0, 0.0) if complete else None

    async def _clamp_to_verified_position(self, user_id: str, action: Action,
                                          risk: dict) -> Action | None:
        """Hard per-position gate against exchange ground truth. Basis = the
        LARGEST of three figures, because each one is blind on its own:

          wallet_cost  what the wallet really holds — but the indexer lags a
                       fill by seconds, so it reads 0 right after a buy.
          row_basis    our tracked cost — but it is 0 until a position row
                       exists, so it says nothing about a FIRST open.
          submitted    what we have actually put on the wire for this token
                       inside the TTL — the only one that is true immediately,
                       and the one that stops a retry burst from sizing itself
                       fresh each time (incident 2026-08-23, see
                       SUBMITTED_BASIS_TTL_SECONDS).

        Returns the (possibly clamped) action, or None when there's no room /
        no proof."""
        verified = await self._wallet_position(
            user_id, action.token_id,
            action.condition_id or (action.row or {}).get("condition_id") or "")
        if verified is None:
            log.warning("buy skipped (%s %s): wallet position could not be "
                        "verified — failing closed", action.kind, action.token_id)
            return None
        wallet_cost, _ = verified
        row_basis = float((action.row or {}).get("notional_usd") or 0.0)
        submitted = self._submitted_basis(user_id, action.token_id)
        # The wallet is authoritative once it can see the shares; at that point
        # the remembered figure has done its job and must not double-count.
        if wallet_cost > 0 and wallet_cost >= submitted:
            self._clear_submitted(user_id, action.token_id)
            submitted = 0.0
        basis = max(wallet_cost, row_basis, submitted)
        allowed = min(float(action.amount),
                      max(0.0, risk["max_per_trade"] - basis))
        # SINGLE SOURCE OF TRUTH for minimum size: the per-wallet
        # "IGNORE POSITIONS < $" slider, for opens and resizes alike. No hidden
        # global override — a slider that silently can't go below a constant is
        # the same class of bug as the open/resize split it replaced. The real
        # protections live elsewhere: execution rejects anything under the
        # market's own min_order_size, and the fill-or-kill budget stops a
        # too-low setting from retrying forever.
        floor = risk["ignore_below"]
        if allowed < floor:
            # user_id included deliberately: without it this line cannot be
            # tied to an account, which cost real time during the 2026-08-23
            # investigation.
            log.info("buy skipped (%s %s %s): basis %.2f (wallet %.2f, row %.2f, "
                     "submitted %.2f) leaves no headroom under cap %.2f",
                     user_id[:10], action.kind, action.token_id, basis,
                     wallet_cost, row_basis, submitted, risk["max_per_trade"])
            return None
        if allowed < float(action.amount) - 0.005:
            log.warning("buy clamped by verified wallet position: %s %.2f -> %.2f "
                        "(wallet basis %.2f, cap %.2f)", action.token_id,
                        action.amount, allowed, basis, risk["max_per_trade"])
        return replace(action, amount=allowed, notional_usd=allowed)

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
                    # Same per-wallet floor as opens (see _clamp_to_verified_position).
                    # Splitting them left this reservation step laxer than the
                    # verified-size gate downstream, so a sub-threshold resize
                    # reserved a claim only to be rejected a step later.
                    floor = risk["ignore_below"]
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
        # A tracked row now carries the basis; the remembered figure retires.
        self._clear_submitted(user_id, action.token_id)
        await self._notify_position({
            "event": "opened", "user_id": user_id, "position_id": pid,
            "market_title": getattr(p, "title", ""),
            "market_slug": getattr(p, "slug", ""), "outcome": action.outcome,
            "shares": result.filled_shares, "entry_price": result.avg_price,
            "notional_usd": spent, "trader_address": action.trader_address,
        })
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
            await self._notify_position({
                "event": "increased", "user_id": user_id, "position_id": fresh["id"],
                "market_title": fresh.get("market_title", ""),
                "market_slug": fresh.get("market_slug", ""),
                "outcome": fresh.get("outcome", ""),
                "shares": result.filled_shares, "entry_price": result.avg_price,
                "notional_usd": spent, "total_shares": new_shares,
                "trader_address": fresh.get("trader_address"),
            })
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
            await self._notify_position({
                "event": "reduced", "user_id": user_id, "position_id": row["id"],
                "market_title": row.get("market_title", ""),
                "market_slug": row.get("market_slug", ""),
                "outcome": row.get("outcome", ""),
                "shares": sold, "exit_price": result.avg_price,
                "realized_pnl": pnl, "total_shares": new_shares,
                "trader_address": row.get("trader_address"),
            })
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
        if not row.get("condition_id"):
            # Opened blind (on-chain fast path, metadata never backfilled) —
            # the resolution cannot be looked up, so flag the row for review
            # instead of booking a fictional $0 loss.
            await self.db.execute(
                "UPDATE copy_positions SET status='reconciliation_required' "
                "WHERE id=? AND status='closing'", (row["id"],))
            log.error("position %s died without condition_id — marked "
                      "reconciliation_required", row["id"])
            return
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
        await self._notify_position({
            "event": "resolved" if status == "resolved" else "closed",
            "user_id": user_id, "position_id": row["id"],
            "market_title": row.get("market_title", ""),
            "market_slug": row.get("market_slug", ""), "outcome": row.get("outcome", ""),
            "shares": filled_shares, "entry_price": row["entry_price"],
            "exit_price": exit_price, "realized_pnl": pnl,
            "trader_address": row.get("trader_address"),
        })

    async def _notify_position(self, event: dict) -> None:
        if self._position_notifier is None:
            return
        try:
            await self._position_notifier(event)
        except Exception:
            # Trading persistence is already committed. Alert delivery is
            # best-effort and must never turn a successful fill into a failure.
            log.exception("position alert failed for %s", event.get("position_id"))

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

    async def _reset_client(self, user: dict):
        """Drop and rebuild a user's cached CLOB client.

        Called after a transport failure: the cached client holds a connection
        the server has already terminated, so every later read on it fails the
        same way until the process restarts.
        """
        cid = user["id"]
        stale = self._clients.pop(cid, None)
        if stale is not None:
            close = getattr(stale, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass          # the connection is gone anyway
        return await self._get_client(user)

    def _cached_collateral(self, user_id: str) -> float | None:
        entry = self._collateral_cache.get(user_id)
        if not entry:
            return None
        value, deadline = entry
        if deadline <= time.monotonic():
            self._collateral_cache.pop(user_id, None)
            return None
        return float(value)

    def _invalidate_collateral(self, user_id: str) -> None:
        """Called the moment we put money on the wire, so the next decision
        sizes against a fresh balance rather than the pre-spend one."""
        self._collateral_cache.pop(user_id, None)

    async def _read_collateral(self, user: dict, client, *,
                               allow_cached: bool = True) -> tuple[float, object]:
        """Available collateral, cached briefly and surviving one connection
        recycle.

        Returns (value, client) — the client may have been rebuilt, and the
        caller must use the returned one. Reading a balance is idempotent, so
        a transport failure is safe to retry; anything else propagates.
        """
        user_id = user["id"]
        if allow_cached and COLLATERAL_CACHE_SECONDS > 0:
            cached = self._cached_collateral(user_id)
            if cached is not None:
                return cached, client
        try:
            value = await self._collateral_fn(client)
        except CLIENT_TRANSPORT_ERRORS as exc:
            log.warning("collateral read hit a transport failure (%s: %s) — "
                        "rebuilding client for %s and retrying once",
                        type(exc).__name__, exc, str(user_id)[:10])
            client = await self._reset_client(user)
            value = await self._collateral_fn(client)
        if COLLATERAL_CACHE_SECONDS > 0:
            self._collateral_cache[user_id] = [
                float(value), time.monotonic() + COLLATERAL_CACHE_SECONDS]
        return value, client

    async def _default_client_factory(self, user: dict):
        pk = wallet.decrypt_private_key(user["private_key_enc"], ENCRYPTION_SECRET)
        return await wallet.make_clob_client(pk, funder=user["id"])

    async def _default_collateral(self, client) -> float:
        bal = await client.get_balance_allowance(asset_type="COLLATERAL")
        return bal.balance / 1e6
