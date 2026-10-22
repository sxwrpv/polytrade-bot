"""Real order execution via polymarket-client's AsyncSecureClient.

No paper mode, no simulation — every code path here places real orders. Safety
comes from real preconditions checked *before* submission, not from a fake mode:
  1. liquidity check (can the book fully fill it locally, per our own quote?)
  2. price band + slippage guard (avg fill vs reference price within
     MAX_COPY_SLIPPAGE_PCT), also signed into the order as a price cap/floor
Only if these pass do we submit. The frontend geoblock probe is advisory unless
ENFORCE_FRONTEND_GEOBLOCK=1. Balance/allowance is NOT pre-checked separately —
the exchange's own not_enough_balance refusal covers that cleanly
(_terminal_rejection), so a doomed BUY just gets a clean rejection instead of
costing an extra API call.

`amount` semantics: for BUY it is the pUSD notional to spend; for SELL it is the
number of shares to sell. The client (AsyncSecureClient) is natively async — no
asyncio.to_thread needed, unlike the old py-clob-client(-v2)-based version.
wallet.make_clob_client builds every client as a TradingClient, which signs
every market order's amounts on the exchange's grid (market_buy_amounts,
market_sell_amounts).
"""
from __future__ import annotations

import logging
import math
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from fractions import Fraction

from polymarket import AsyncSecureClient
from polymarket.errors import RequestRejectedError

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
    # Set (with ok=False, submission_uncertain=False) when the exchange received
    # this BUY and refused it outright: see _terminal_rejection. Unlike a
    # pre-submission skip, the order did go on the wire.
    exchange_rejection: str = ""
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


# ---------------------------------------------------------------------------
# Market order amounts on the exchange's grid
# ---------------------------------------------------------------------------
# The CLOB takes a market (FOK/FAK) BUY only when the pUSD it offers, the maker
# amount, has at most 2 decimals and the shares it asks for, the taker amount,
# at most 4. Otherwise POST /order answers 400 with _INVALID_AMOUNTS (the same
# text and status are in github.com/Polymarket/rs-clob-client/issues/261).
# polymarket-client 0.6.0 floors the spend to cents, then asks for
# spend / max_price shares rounded up to the decimals its table gives the
# market's tick size (_ROUNDING_BY_TICK, polymarket/_internal/actions/orders/
# context.py): 3 at tick 0.1 and 4 at 0.01, both within the rule; 5 at 0.005
# and 0.001 and 6 at 0.0025 and 0.0001. On those finer ticks 81-98% of spends
# from $1 to $20 broke the rule across every price: $10.00 capped at 0.987
# asked for 10.13172 shares. Production, 2026-10-22 15:20Z: two accounts, one
# token, $10.00 each, both refused and fenced as uncertain.
#
# The direction the shares round in decides which asks the order can take.
# The exchange prices a BUY as maker / taker and matches it only with asks at
# or below that price. Rounded up, as the SDK rounds every capped BUY, the
# shares put the price a hair under the cap whenever spend / cap is inexact:
# 85% of spends from $1 at tick 0.01, 97% at 0.001 and over 99% at 0.0001. An
# ask resting exactly at the cap is then out of reach and the FOK is killed
# (github.com/Polymarket/py-sdk/issues/291, two thirds of one bot's FAK orders
# for a month; fixed upstream in 6a77970b by rounding down). On a 0.01 tick a
# copy often needs that ask. With the default 2% slippage the cap equals the
# leader's own price for every price from 0.10 to 0.49 and at the 0.98
# maximum, so the copy needs the rest of the level the leader just lifted;
# from 0.50 up the cap is one tick above it, where the best ask sits once a
# leader's resting bid is hit. In public fills of the 25 leaderboard wallets
# PolyTrade seeds (2026-08-30 to 2026-10-22), 36% of their BUYs on 0.01-tick
# markets put a copy's cap on such an ask.
#
# So the shares are spend / cap rounded down to 0.0001. The price lands at or
# a hair above the cap, the order lifts an ask there, and the fill runs at the
# resting price. It stays under cap + 0.0001, the next price on the finest
# grid the CLOB lists, so no ask above the cap can match, even after a
# market's tick turns finer (upstream 1ed8b7cd checks the same): rounding down
# drops under 0.0001 shares, which keeps spend / shares under cap + 0.0001 for
# any spend of at least cap^2 + 0.0001 x cap, so for every spend from $1
# (copy_engine.MIN_NOTIONAL_USD). A smaller spend that could reach it rounds
# up. Shaving the spend until spend / max_price fit 4 decimals was measured
# too: at caps 0.961-0.999 it cost over 2% of the order for one BUY in ten and
# over 15% for one in a hundred, and some spends had no fit at all.
#
# A market SELL offers shares (maker) for pUSD (taker). The SDK floors the
# shares to 0.01 and asks for shares x min_price pUSD, which carries up to 5
# decimals on a 0.001 or 0.005 tick and 6 on a 0.0025 or 0.0001 tick: 10.37
# shares floored at 0.973 ask for 10.09001. No SELL refused for its amounts is
# on record (2026-10-22 production logs, the SDKs' issue trackers), and every
# official client signs these amounts. The exchange's limit for a SELL's pUSD is still
# unknown, and a close refused that way would stay uncertain and go out again
# after each stuck-closing reopen, so a SELL goes on the same grid: its shares
# keep their 2 decimals and its pUSD rounds up to 0.0001. That lifts the
# order's price (taker / maker) by under 0.0001 / shares, so a SELL stays at or
# above its floor; at ticks 0.01 and 0.1 nothing changes. On the finer ticks it
# also puts a bid resting exactly at the floor out of reach, the SELL side of
# py-sdk#291: the exchange matches only once the order's price reaches the
# resting one.
_MAKER_STEP = 10_000   # 2 decimals in base units (both amounts carry 6 decimals)
_TAKER_STEP = 100      # 4 decimals in base units
_FINEST_TICK = Fraction(1, 10_000)


def market_buy_amounts(maker: int, taker: int, cap: Decimal | None) -> tuple[int, int]:
    """A market BUY's (maker, taker) base units on the CLOB's grid. The pUSD
    offered rounds down to a cent. With a cap the shares are maker / cap
    rounded down to 0.0001, which prices the order (maker / taker) at or just
    above the cap and under cap + 0.0001; a spend under $1 that rounding down
    would carry to cap + 0.0001 rounds up and prices at or below the cap.
    Without one (an unprotected BUY, which the SDK prices off the book) the
    SDK's shares round down to 0.0001."""
    maker = maker // _MAKER_STEP * _MAKER_STEP
    if cap is None:
        return maker, taker // _TAKER_STEP * _TAKER_STEP
    price = Fraction(cap)
    steps = maker / (price * _TAKER_STEP)
    down = math.floor(steps) * _TAKER_STEP
    if down and Fraction(maker, down) < price + _FINEST_TICK:
        return maker, down
    return maker, math.ceil(steps) * _TAKER_STEP


def market_sell_amounts(maker: int, taker: int) -> tuple[int, int]:
    """A market SELL's (maker, taker) base units on the CLOB's grid: the shares
    offered round down to 2 decimals and the pUSD asked up to 4. A SELL is
    priced taker / maker, so its price can only rise and it keeps its floor.
    On a 0.01 or 0.1 tick the SDK's amounts are already on the grid and come
    back unchanged."""
    return maker // _MAKER_STEP * _MAKER_STEP, -(-taker // _TAKER_STEP) * _TAKER_STEP


# The cap of the market BUY being prepared, for _sign_order, whose draft holds
# the amounts the SDK priced and no price. A context variable belongs to one
# task, so concurrent orders on one client each see their own cap.
_market_buy_cap: ContextVar[Decimal | None] = ContextVar("market_buy_cap", default=None)


class TradingClient(AsyncSecureClient):
    """AsyncSecureClient that signs every market order on the CLOB's grid, a
    BUY priced to lift an ask at its cap (market_buy_amounts,
    market_sell_amounts). wallet.make_clob_client builds every client as one.

    The amounts are part of the signed order, so _sign_order is the last place
    they can change. In polymarket-client 0.6.0 it is the one signing step for
    limit (GTC/GTD) and market (FOK/FAK) orders, and it receives the draft the
    SDK priced; _prepare_and_sign_market_order, which create_market_order and
    place_market_order both call, carries a BUY's cap to it. Only FOK/FAK
    drafts change here.
    tests/test_execution_price_band.py sends real BUYs and SELLs through this
    class and reads the orders that reach POST /order, so an SDK release that
    signs or prices market orders somewhere else fails those tests."""

    async def _prepare_and_sign_market_order(self, **order):
        cap = order.get("max_price") if order.get("side") == "BUY" else None
        token = _market_buy_cap.set(None if cap is None else Decimal(str(cap)))
        try:
            return await super()._prepare_and_sign_market_order(**order)
        finally:
            _market_buy_cap.reset(token)

    async def _sign_order(self, draft, *, post_only: bool):
        if draft.order_type in ("FOK", "FAK"):
            if draft.side == "BUY":
                maker, taker = market_buy_amounts(draft.offered_amount, draft.requested_amount,
                                                  _market_buy_cap.get())
            else:
                maker, taker = market_sell_amounts(draft.offered_amount, draft.requested_amount)
            draft = replace(draft, offered_amount=maker, requested_amount=taker)
        return await super()._sign_order(draft, post_only=post_only)


# The CLOB answers to POST /order that prove the order never executed. Each is
# the exchange's verdict on the order we sent, given before anything matched:
# a FOK that cannot fill completely is killed whole, and the balance /
# allowance check and the check of a market order's amounts (see
# market_buy_amounts) run before matching. polymarket-client 0.6.0 raises them
# as RequestRejectedError(status=400) carrying the server's text, or returns
# the first two as RejectedOrder(code='fok_not_filled' / 'not_enough_balance')
# (polymarket/models/clob/order_response.py). It has no code for the amounts
# text: in a 200 body that text would come back as RejectedOrder(code=
# 'unknown'), the code a delayed order gets too, so only its 400 counts. The
# SDK's only retry re-posts the SAME signed order once, after a 400 allowance
# shortfall (polymarket/_internal/actions/orders/place.py), so the error we see
# is the last of at most two refusals. Production, 2026-10-19: all five fences
# behind the first two texts held 0 shares on-chain and in the indexer.
#
# Everything else after submission stays uncertain: transport errors and
# timeouts, rate limits, 5xx, a 4xx with any other text, and the "rejected"
# shapes that can still execute. A delayed order whose response carries an
# errorMsg normalizes to RejectedOrder(code='unknown'), and 'unmatched' is a
# placement status, not a refusal.
_FOK_NOT_FILLED = "order couldn't be fully filled. FOK orders are fully filled or killed."
_NOT_ENOUGH_BALANCE = "not enough balance / allowance"
_INVALID_AMOUNTS = ("invalid amounts, the market buy orders maker amount supports a max "
                    "accuracy of 2 decimals, taker amount a max of 4 decimals")


def _terminal_rejection(message: str) -> str:
    """'fok_not_filled', 'not_enough_balance' or 'invalid_amounts' when
    `message` is one of those verdicts, else ''. The FOK and amounts texts must
    match exactly (a trailing period is optional); the balance text is a
    prefix, because the server may append the balance or allowance figures. A
    reworded message stays uncertain."""
    text = (message or "").strip()
    if text.rstrip(".") == _FOK_NOT_FILLED.rstrip("."):
        return "fok_not_filled"
    if text.startswith(_NOT_ENOUGH_BALANCE):
        return "not_enough_balance"
    if text.rstrip(".") == _INVALID_AMOUNTS:
        return "invalid_amounts"
    return ""


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
        # response is not, in general, evidence that no shares arrived (a
        # delayed order can come back as RejectedOrder(code='unknown')). The
        # SDK's codes for the FOK and balance verdicts are, but only on a BUY.
        # It has none for the amounts verdict (see _terminal_rejection).
        rejection = _terminal_rejection(message) if side == "BUY" else ""
        if rejection and code == rejection:
            res.exchange_rejection = rejection
            return
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
        # We crossed the SDK's submission boundary, so an exception is ambiguous
        # (the engine retains its durable claim and reconciles before any
        # retry) unless it is the exchange's own terminal verdict on a BUY: a
        # 400 carrying one of the texts in _terminal_rejection, meaning the
        # order was refused outright and nothing executed. A 4xx with any other
        # text stays ambiguous: the 2026-08-23 cap incident (three BUYs
        # reported failed, all filled) is why that is the default. A SELL's
        # refusal stays ambiguous whatever its text: stuck-closing recovery
        # settles it from the wallet, and the exchange's wording for a SELL
        # off the amounts grid has never been seen. Preflight failures above
        # still return cleanly without setting either flag.
        label = "insufficient_liquidity" if type(e).__name__ == "InsufficientLiquidityError" else "api_error"
        res.reason = f"{label}: {e}"
        rejection = ""
        if side == "BUY" and isinstance(e, RequestRejectedError) and e.status == 400:
            rejection = _terminal_rejection(str(e))
        if rejection:
            res.exchange_rejection = rejection
        else:
            res.submission_uncertain = True
        return res

    _finalize(res, resp, side)
    return res
