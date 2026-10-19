"""Real order execution via polymarket-client's AsyncSecureClient.

No paper mode, no simulation — every code path here places real orders. Safety
comes from real preconditions checked *before* submission, not from a fake mode:
  1. liquidity check (can the book fully fill it locally, per our own quote?)
  2. price band + slippage guard (avg fill vs reference price within
     MAX_COPY_SLIPPAGE_PCT), also signed into the order as a price cap/floor
Only if these pass do we submit. The frontend geoblock probe is advisory unless
ENFORCE_FRONTEND_GEOBLOCK=1. Balance/allowance is NOT pre-checked separately —
the SDK's own RejectedOrder(code='not_enough_balance') covers that cleanly, so a
doomed order just gets a clean rejection instead of costing an extra API call.

`amount` semantics: for BUY it is the pUSD notional to spend; for SELL it is the
number of shares to sell. The client (AsyncSecureClient) is natively async — no
asyncio.to_thread needed, unlike the old py-clob-client(-v2)-based version.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from backend.config import (
    ENFORCE_FRONTEND_GEOBLOCK, MAX_COPY_SLIPPAGE_PCT, POLYMARKET_BUILDER_CODE,
)
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

class PreflightError(Exception):
    """A failure BEFORE the SDK submission boundary: provably nothing reached
    the exchange, so the caller may release its fence and retry later.

    Every exception from the order call itself is ambiguous (see
    place_market_order), but an order-book read that fails happens before any
    order exists. It used to escape as a bare exception, which the BUY path
    classifies as uncertain: a durable claim that no reconciliation can ever
    settle, because the wallet will never show shares for an order that was
    never sent. One 429 or timeout on /book froze that token for that user
    permanently.
    """

    def __init__(self, stage: str, cause: BaseException) -> None:
        response = getattr(cause, "response", None)
        self.stage = stage
        self.status_code = getattr(response, "status_code", None)
        super().__init__(f"{stage}: {type(cause).__name__}: {cause}")


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
    submission_uncertain: bool = False
    raw: dict = field(default_factory=dict)


def floor_decimal_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """Floor a BUY cap to an exact exchange tick without weakening the cap."""
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def ceil_decimal_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """Round a SELL floor upward without weakening the user's price limit."""
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


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
        # The production cap incident proved that a post-submit "rejected"
        # response is not authoritative evidence that no shares arrived.
        res.submission_uncertain = True
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
        res.submission_uncertain = True
        res.filled_shares = 0.0
        res.avg_price = 0.0
        log.warning("accepted order %s carried no fills — treating as killed: %s",
                    res.order_id, res.raw["order"])


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def _check_frontend_geoblock(pm, res: "OrderResult") -> bool:
    """Probe the FRONTEND geoblock and record the result on `res`.

    Returns True only when the order must be aborted, which now requires
    ENFORCE_FRONTEND_GEOBLOCK=1.

    Why this is advisory: `polymarket.com/api/geoblock` answers for the
    *website*, not the CLOB trading API — they are separate surfaces with
    separate restriction lists (Ireland is close-only on the frontend while the
    API stays reachable; verified live: clob.polymarket.com returns 200 and
    authenticated balance reads succeed while this probe says blocked). Using it
    as an order-level veto meant 290 consecutive orders were refused by our own
    code and never reached the exchange. The exchange rejects what it won't
    accept, so the signal is logged and attached to the result instead.
    """
    try:
        geo = await pm.get_geoblock()
    except Exception as e:            # probe failure was already non-fatal
        res.raw["geoblock_error"] = str(e)
        return False
    if not geo.get("blocked"):
        return False
    where = f"{geo.get('country')}/{geo.get('region')}"
    res.raw["frontend_geoblock"] = where
    if ENFORCE_FRONTEND_GEOBLOCK:
        res.reason = f"geoblocked ({where})"
        return True
    log.info("frontend geoblock reports %s — submitting anyway (the CLOB API is "
             "a separate surface; set ENFORCE_FRONTEND_GEOBLOCK=1 to block)", where)
    return False


async def place_market_order(
    client,
    pm: PolymarketClient,
    token_id: str,
    side: str,                 # "BUY" | "SELL"
    amount: float,             # pUSD for BUY, shares for SELL
    *,
    reference_price: float | None = None,
    max_slippage_pct: float = MAX_COPY_SLIPPAGE_PCT,
    min_price: float | None = None,
    max_price: float | None = None,
    check_geoblock: bool = True,
) -> OrderResult:
    """Market order (FOK — all-or-nothing). Used for ALL engine trades (owner
    switched copy BUYs from price-capped limit to market, 2026-07-03); the
    pre-flight slippage check vs reference_price is the price gate."""
    side = side.upper()
    res = OrderResult(ok=False, side=side, token_id=token_id, amount_usd=amount)

    if check_geoblock and await _check_frontend_geoblock(pm, res):
        return res

    try:
        book = await pm.get_orderbook(token_id)
    except Exception as exc:
        raise PreflightError("orderbook", exc) from exc
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
    # Enforce the configured absolute entry bracket against the actual
    # pre-flight fill, not only an earlier leader-price snapshot.
    if side == "BUY" and (
        (min_price is not None and fill.avg_price < min_price - _EPS)
        or (max_price is not None and fill.avg_price > max_price + _EPS)
    ):
        lower = min_price if min_price is not None else 0.0
        upper = max_price if max_price is not None else 1.0
        res.reason = f"price_out_of_band (avg={fill.avg_price:.4f} band={lower:.4f}-{upper:.4f})"
        return res
    if not slippage_ok(side, fill.avg_price, ref, max_slippage_pct):
        res.reason = f"slippage_exceeded (avg={fill.avg_price:.4f} ref={ref:.4f})"
        return res
    res.filled_shares = fill.shares
    res.avg_price = fill.avg_price

    # Encode the strictest BUY ceiling into the signed FOK order itself. This
    # closes the race where the book moves after our quote but before matching.
    exchange_cap: Decimal | None = None
    exchange_floor: Decimal | None = None
    if side == "BUY":
        tick = Decimal(str(book.tick_size))
        caps: list[Decimal] = []
        if max_price is not None:
            caps.append(Decimal(str(max_price)))
        if ref > 0:
            caps.append(Decimal(str(ref)) *
                        (Decimal("1") + Decimal(str(max_slippage_pct)) / Decimal("100")))
        raw_cap = min(caps) if caps else Decimal("1") - tick
        raw_cap = min(raw_cap, Decimal("1") - tick)
        exchange_cap = floor_decimal_to_tick(raw_cap, tick)
        if exchange_cap < tick:
            res.reason = f"invalid_exchange_price_cap ({exchange_cap})"
            return res
        res.limit_price = float(exchange_cap)
    else:
        # Protect the close against a book move after preflight by carrying the
        # user's slippage floor in the signed FOK order sent to the exchange.
        tick = Decimal(str(book.tick_size))
        raw_floor = Decimal(str(ref)) * (
            Decimal("1") - Decimal(str(max_slippage_pct)) / Decimal("100"))
        exchange_floor = ceil_decimal_to_tick(max(raw_floor, tick), tick)
        res.limit_price = float(exchange_floor)

    try:
        if side == "BUY":
            resp = await client.place_market_order(
                token_id=token_id, side="BUY", amount=Decimal(str(amount)),
                max_price=exchange_cap, order_type="FOK",
                builder_code=_BUILDER_CODE)
        else:
            resp = await client.place_market_order(
                token_id=token_id, side="SELL", shares=amount, order_type="FOK",
                min_price=exchange_floor,
                builder_code=_BUILDER_CODE)
    except Exception as e:
        # We crossed the SDK's submission boundary. Production evidence showed
        # exceptions labelled as FOK/4xx failures even though shares arrived in
        # the wallet. Treat every exception from this call as ambiguous: the
        # engine must retain its durable claim and reconcile before any retry.
        # Preflight failures above still return cleanly without setting this.
        label = "insufficient_liquidity" if type(e).__name__ == "InsufficientLiquidityError" else "api_error"
        res.reason = f"{label}: {e}"
        res.submission_uncertain = True
        return res

    _finalize(res, resp, side)
    return res
