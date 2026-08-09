# Copy Trading

## Overview

PolyTrade converts source-wallet position changes into risk-checked orders for a custodial copy wallet.

```text
detect → validate → size → reserve → submit → persist → reconcile
```

No step guarantees identical execution to the source.

## What is copied

The engine reacts to:

- **Open:** source acquires a new outcome.
- **Resize up:** source increases an outcome.
- **Resize down:** source reduces it.
- **Close or resolve:** source exits or the market settles.

A reduction of at least 95% is treated as a full exit. Smaller reductions are copied proportionally. Resizes wait until source share-count drift exceeds 25% to avoid constant tiny orders.

## Signal timing

The default detector polls activity frequently while a slower full reconcile runs separately. Timing depends on indexing, network latency, engine load, and maker/taker attribution.

Following starts activity polling from that time, but reconciliation can discover positions already open. Following is permission to align with current holdings, not only future trades.

## Sizing

Initial target:

```text
source position value × copy ratio
```

The engine then clamps against maximum copied position, collateral, minimum order and ignore-below thresholds, trader exposure, account exposure, maximum open positions, verified wallet headroom, existing copied size, and reserved claims.

### Example

A source holds `$2,000`. Your ratio is `1%` and maximum position is `$15`.

```text
raw target = $2,000 × 1% = $20
final target = min($20, $15, other active limits) = at most $15
```

Maximum position is an aggregate outcome cap, not a fresh allowance on every resize.

## Risk validation

Before a buy, the engine re-reads account and follow settings, counts positions and claims, reserves exposure, checks actual holdings, and fences the operation with a risk revision.

Daily loss is per followed trader from realized PnL since `00:00 UTC`. It blocks buys and increases, not risk-reducing exits.

## Order behavior

Buys and sells use market FOK orders. The complete requested quantity must be fillable within the limit. Buys carry a signed ceiling; sells carry a signed floor.

An order can be skipped or rejected due to liquidity, minimum-size rules, price or slippage controls, collateral/allowance, exposure or loss limits, pause state, exchange rejection, incomplete upstream data, or an unresolved claim.

## Duplicate prevention

A durable claim reserves the user/outcome pair before submission. Database uniqueness prevents more than one active position or claim for that pair.

Ambiguous exchange responses become uncertain instead of being retried blindly. Reconciliation checks live holdings later. This favors preventing duplicate real-money orders over availability.

## Pause, disable, and unfollow

Account pause, trader pause, unfollow, and daily-loss protection stop **new buys**. They do not liquidate existing positions, which can still resize down, close, or resolve.

## Manual close

The Positions page can close a managed or external holding with 0–10% slippage. The backend submits a market FOK sell with an exchange-enforced floor.

## Why results diverge

Follower results can differ due to detection delay, liquidity, order type, size, rounding, collateral, risk controls, older holdings found during reconciliation, minimum-size rules, and redemption timing.

Historical source performance does not include these follower-specific effects.

## Monitoring

Use Home for KPIs and follows, Open Positions for managed/uncertain/external holdings, Activity for persisted events, User for equity/PnL, and server logs for operator diagnosis.

Telegram alerts are best-effort after confirmed persistence. Alert failure does not reverse a trade.

## Next step

Review [Wallet and Funding](wallet-and-funding.md) and [Risk and Security](risk-and-security.md).
