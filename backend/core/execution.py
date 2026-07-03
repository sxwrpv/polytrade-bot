"""Real order execution via polymarket-client's AsyncSecureClient.

No paper mode, no simulation — every code path here places real orders. Safety
comes from real preconditions checked *before* submission, not from a fake mode:
  1. geoblock check (region permitted?)
  2. liquidity check (can the book fully fill it locally, per our own quote?)
  3. slippage guard (avg fill vs reference price within MAX_COPY_SLIPPAGE_PCT)
Only if both pass do we submit. Balance/allowance is NOT pre-checked separately —
the SDK's own RejectedOrder(code='not_enough_balance') covers that cleanly, so a
doomed order just gets a clean rejection instead of costing an extra API call.

`amount` semantics: for BUY it is the pUSD notional to spend; for SELL it is the
number of shares to sell. The client (AsyncSecureClient) is natively async — no
asyncio.to_thread needed, unlike the old py-clob-client(-v2)-based version.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from backend.config import MAX_COPY_SLIPPAGE_PCT, POLYMARKET_BUILDER_CODE
from backend.core.polymarket import Level, PolymarketClient

log = logging.getLogger("execution")

_EPS = 1e-9

# Stamped on every order: attributes routed volume to the owner's builder
# account (and carries the builder fee rates configured on Polymarket's side).
# None when unset — the SDK treats that as "no builder".
_BUILDER_CODE = POLYMARKET_BUILDER_CODE or None


# ---------------------------------------------------------------------------
# Pure book-walking / quoting (no IO — unit-testable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fill:
    shares: float
    avg_price: float
    usd: float
    fully: bool          # could the requested size be fully filled by the book?


def quote_buy(asks: tuple[Level, ...], amount_usd: float) -> Fill:
    """Spend up to amount_usd buying across ascending ask levels (best first)."""
    remaining, shares, spent = amount_usd, 0.0, 0.0
    for lvl in asks:
        if lvl.price <= 0:
            continue
        level_cost = lvl.price * lvl.size
        if level_cost >= remaining - _EPS:
            shares += remaining / lvl.price
            spent += remaining
            remaining = 0.0
            break
        shares += lvl.size
        spent += level_cost
        remaining -= level_cost
    avg = spent / shares if shares > 0 else 0.0
    return Fill(shares, avg, spent, remaining <= _EPS)


def quote_sell(bids: tuple[Level, ...], shares: float) -> Fill:
    """Sell `shares` across descending bid levels (best first)."""
    remaining, sold, proceeds = shares, 0.0, 0.0
    for lvl in bids:
        take = min(remaining, lvl.size)
        proceeds += take * lvl.price
        sold += take
        remaining -= take
        if remaining <= _EPS:
            break
    avg = proceeds / sold if sold > 0 else 0.0
    return Fill(sold, avg, proceeds, remaining <= _EPS)


def slippage_ok(side: str, avg_price: float, reference: float, max_pct: float) -> bool:
    if reference <= 0:
        return True  # no reference to compare against
    if side == "BUY":
        return avg_price <= reference * (1 + max_pct / 100) + _EPS
    return avg_price >= reference * (1 - max_pct / 100) - _EPS


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    ok: bool
    reason: str = ""
    side: str = ""
    token_id: str = ""
    order_id: str = ""
    status: str = ""
    filled_shares: float = 0.0
    avg_price: float = 0.0
    amount_usd: float = 0.0
    limit_price: float = 0.0
    raw: dict = field(default_factory=dict)


def round_to_tick(price: float, tick: float, mode: str) -> float:
    if tick <= 0:
        return price
    import math
    steps = price / tick
    if mode == "floor":
        return round(math.floor(steps) * tick, 6)
    if mode == "ceil":
        return round(math.ceil(steps) * tick, 6)
    return round(round(steps) * tick, 6)


def _finalize(res: "OrderResult", resp, side: str) -> None:
    """Parse an AcceptedOrder/RejectedOrder (polymarket-client) into the result.

    making_amount/taking_amount are from the order's own perspective: BUY offers
    pUSD (making) for shares (taking); SELL offers shares (making) for pUSD
    (taking).

    VERIFIED AGAINST A REAL FILL (2026-07-03): an accepted FOK market order can
    come back with making/taking = 0 even though the fill genuinely executed on
    the exchange (confirmed via data-api: the wallet received the shares at the
    quoted price). So the response amounts are used only when present; when
    they're 0/absent, the caller's PRE-FLIGHT QUOTE (already in res.filled_shares
    / res.avg_price) stands — correct for FOK by construction: all-or-nothing at
    the quoted book, and the order was accepted.
    """
    res.raw["order"] = resp.model_dump() if hasattr(resp, "model_dump") else {"resp": str(resp)}
    if not getattr(resp, "ok", False):
        code = getattr(resp, "code", "unknown")
        message = getattr(resp, "message", "")
        res.ok = False
        res.reason = f"order_rejected: {code} - {message}"
        return
    res.ok = True
    res.order_id = resp.order_id
    res.status = resp.status
    making = float(resp.making_amount)
    taking = float(resp.taking_amount)
    if side == "BUY":
        shares, avg = taking, (making / taking if taking else 0.0)
    else:
        shares, avg = making, (taking / making if making else 0.0)
    if shares > _EPS and avg > _EPS:
        res.filled_shares = shares
        res.avg_price = avg
        return
    # Zero amounts on an ACCEPTED order is ambiguous — seen live 2026-07-03
    # meaning BOTH "filled at the quote" (KBO/ITF fills, confirmed on-exchange)
    # AND "killed, never executed" (the Troyes phantom: accepted response, no
    # trade ever happened). Discriminate on the response's own evidence: a fill
    # produces trade ids / tx hashes; a kill produces neither.
    trade_ids = tuple(getattr(resp, "trade_ids", ()) or ())
    tx_hashes = tuple(getattr(resp, "transactions_hashes", ()) or ())
    if trade_ids or tx_hashes:
        res.raw["fill_amounts_missing"] = {"making": making, "taking": taking}
        log.warning("accepted order %s: zero amounts but %d trade(s)/%d tx — "
                    "using pre-flight quote (%.4f sh @ %.4f)",
                    res.order_id, len(trade_ids), len(tx_hashes),
                    res.filled_shares, res.avg_price)
    else:
        res.ok = False
        res.reason = "accepted_but_unfilled (zero amounts, no trades/txs)"
        res.filled_shares = 0.0
        res.avg_price = 0.0
        log.warning("accepted order %s carried no fills — treating as killed: %s",
                    res.order_id, res.raw["order"])


def _to_units(v) -> float:
    """Polymarket balances come as integers in 1e6 base units (pUSD/CTF both
    have 6 decimals)."""
    try:
        return float(v) / 1e6
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def place_market_order(
    client,
    pm: PolymarketClient,
    token_id: str,
    side: str,                 # "BUY" | "SELL"
    amount: float,             # pUSD for BUY, shares for SELL
    *,
    reference_price: float | None = None,
    max_slippage_pct: float = MAX_COPY_SLIPPAGE_PCT,
    check_geoblock: bool = True,
) -> OrderResult:
    """Market order (FOK — all-or-nothing). Used for ALL engine trades (owner
    switched copy BUYs from price-capped limit to market, 2026-07-03); the
    pre-flight slippage check vs reference_price is the price gate."""
    side = side.upper()
    res = OrderResult(ok=False, side=side, token_id=token_id, amount_usd=amount)

    if check_geoblock:
        try:
            geo = await pm.get_geoblock()
            if geo.get("blocked"):
                res.reason = f"geoblocked ({geo.get('country')}/{geo.get('region')})"
                return res
        except Exception as e:  # best-effort; don't hard-fail on geoblock probe error
            res.raw["geoblock_error"] = str(e)

    book = await pm.get_orderbook(token_id)
    if side == "BUY":
        fill = quote_buy(book.asks, amount)
        ref = reference_price if reference_price else (book.best_ask.price if book.best_ask else 0)
    else:
        fill = quote_sell(book.bids, amount)
        ref = reference_price if reference_price else (book.best_bid.price if book.best_bid else 0)

    if not fill.fully:
        res.reason = "insufficient_liquidity"
        return res
    if fill.shares < book.min_order_size - _EPS:
        res.reason = f"below_min_size ({book.min_order_size})"
        return res
    if not slippage_ok(side, fill.avg_price, ref, max_slippage_pct):
        res.reason = f"slippage_exceeded (avg={fill.avg_price:.4f} ref={ref:.4f})"
        return res
    res.filled_shares = fill.shares
    res.avg_price = fill.avg_price

    try:
        if side == "BUY":
            resp = await client.place_market_order(
                token_id=token_id, side="BUY", amount=amount, order_type="FOK",
                builder_code=_BUILDER_CODE)
        else:
            resp = await client.place_market_order(
                token_id=token_id, side="SELL", shares=amount, order_type="FOK",
                builder_code=_BUILDER_CODE)
    except Exception as e:
        res.reason = f"api_error: {e}"
        return res

    _finalize(res, resp, side)
    return res


async def place_capped_order(
    client,
    pm: PolymarketClient,
    token_id: str,
    side: str,                       # "BUY" | "SELL"
    *,
    reference_price: float,          # the LEADER's fill price — the anchor
    target_usd: float | None = None,     # BUY notional
    target_shares: float | None = None,  # SELL share count
    max_slippage_pct: float = MAX_COPY_SLIPPAGE_PCT,
    check_geoblock: bool = True,
    order_type: str = "FAK",
) -> OrderResult:
    # NOTE: no longer used by the copy engine — the owner switched copies to
    # market orders (2026-07-03). Kept tested and working for an easy revert:
    # price-capped FAK strictly bounds every share at leader*(1+slippage%) and
    # partial-fills, vs the market path's skip-or-fill-at-quote semantics.
    """Price-capped order anchored to the leader's fill price, via the SDK's
    native max_price/min_price (server-side enforced, not just a local check).

    BUY: cap = leader_price * (1 + slippage), rounded down to tick. SELL: floor
    = leader_price * (1 - slippage), rounded up to tick. Posted FAK by default —
    fills only what's available at/within the cap right now and kills the rest.
    So we never pay more than the cap: if the market ran away past it we
    partial-fill or skip (`no_liquidity_within_cap`, from our own local
    pre-check) rather than chase a >10c-worse entry.
    """
    side = side.upper()
    res = OrderResult(ok=False, side=side, token_id=token_id, amount_usd=target_usd or 0.0)

    if check_geoblock:
        try:
            geo = await pm.get_geoblock()
            if geo.get("blocked"):
                res.reason = f"geoblocked ({geo.get('country')}/{geo.get('region')})"
                return res
        except Exception as e:
            res.raw["geoblock_error"] = str(e)

    book = await pm.get_orderbook(token_id)
    tick = book.tick_size or 0.01

    if side == "BUY":
        if target_usd is None:
            res.reason = "missing target_usd"
            return res
        cap = reference_price * (1 + max_slippage_pct / 100)
        limit_price = round_to_tick(cap, tick, "floor")
        if limit_price <= 0:
            res.reason = "cap_below_tick"
            return res
        in_cap = tuple(lvl for lvl in book.asks if lvl.price <= limit_price + _EPS)
        fill = quote_buy(in_cap, target_usd)
    else:
        if target_shares is None:
            res.reason = "missing target_shares"
            return res
        floor = reference_price * (1 - max_slippage_pct / 100)
        limit_price = round_to_tick(floor, tick, "ceil")
        in_cap = tuple(lvl for lvl in book.bids if lvl.price >= limit_price - _EPS)
        fill = quote_sell(in_cap, target_shares)

    res.limit_price = limit_price
    if fill.shares <= _EPS:
        res.reason = "no_liquidity_within_cap"   # don't chase — partial-fill or skip
        return res
    if fill.shares < book.min_order_size - _EPS:
        res.reason = f"below_min_size ({book.min_order_size})"
        return res
    res.filled_shares = fill.shares       # expected; refined from resp by _finalize
    res.avg_price = fill.avg_price

    try:
        if side == "BUY":
            resp = await client.place_market_order(
                token_id=token_id, side="BUY", amount=target_usd,
                max_price=limit_price, order_type=order_type,
                builder_code=_BUILDER_CODE)
        else:
            resp = await client.place_market_order(
                token_id=token_id, side="SELL", shares=target_shares,
                min_price=limit_price, order_type=order_type,
                builder_code=_BUILDER_CODE)
    except Exception as e:
        res.reason = f"api_error: {e}"
        return res

    _finalize(res, resp, side)
    return res
