# Core Concepts

## Prediction-market shares

A Polymarket position is a quantity of outcome shares. A price from `$0.00` to `$1.00` roughly expresses implied probability. If an outcome resolves as the winner, winning shares are redeemable at `$1.00`; losing shares resolve to `$0.00`.

Price is not certainty. Resolution rules, liquidity, spread, timing, fees, and settlement affect the result.

## Source wallet and copy wallet

- **Source wallet:** the Polymarket wallet PolyTrade watches.
- **Copy wallet:** the custodial wallet PolyTrade controls for one user.
- **Follow:** the saved sizing and risk configuration for one source.
- **Copy position:** PolyTrade's durable managed position record.

Copying follows position changes. It does not replay an identical transaction.

## Detection and reconciliation

PolyTrade has two complementary paths:

1. **Fast detection** checks source activity frequently.
2. **Full reconciliation** compares source live positions with saved state.

Fast detection improves responsiveness. Reconciliation restores correctness after missed events, restarts, partial upstream data, and uncertain submissions.

Optional Polygon RPC detection polls `OrderFilled` logs over HTTP. It is not a WebSocket subscription and can miss taker-side activity until reconciliation.

## Position lifecycle

```text
not held
  → detected/reserved
  → submitting
  → open
  → resized or closing
  → closed
  → resolved/claimable
```

An ambiguous network or upstream result can leave a buy **uncertain**. PolyTrade fences duplicate action until live wallet holdings resolve it.

## Orders and execution

Active buys and sells use market **fill-or-kill (FOK)** orders:

- the entire requested size must be fillable;
- exchange minimum-size constraints must pass;
- buys include an exchange-enforced maximum price;
- sells include an exchange-enforced minimum price;
- slippage and price bands are checked before submission.

A failed FOK order does not partially fill. The source wallet may have a different outcome because its order arrived earlier or used another order type.

## Custodial model

PolyTrade creates and stores each user's signer. The private key is encrypted at rest with AES-256-GCM using a key derived from `ENCRYPTION_SECRET`. The server decrypts it for unattended signing.

This enables automated copying but creates central custody risk: database plus secret compromise can expose keys, secret loss can make keys unusable, and application compromise can initiate authenticated actions.

## Gasless and EOA modes

With Builder credentials, PolyTrade creates the intended gasless deposit/funder wallet and configures exchange approvals through the relayer.

Without them, PolyTrade falls back to a plain EOA. That path can require MATIC for allowance transactions and is not recommended for production.

## Balances and PnL

The app separates available cash, open-position value, resolved claimable value, equity, and PnL. Equity history is periodically sampled and downsampled for longer periods; it is an operational view, not an audited statement.

## Resolved versus redeemed

A resolved winning position becomes claimable. PolyTrade records it but does not automatically redeem outcome tokens. Redeem through Polymarket when required.

## Data authority

PolyTrade combines local state with Polymarket APIs and live wallet data. Indexers can lag or truncate history. Official Polymarket data remains authoritative for market rules, resolution, and lifetime leaderboard figures.

## Next step

Continue to [Copy Trading](copy-trading.md).
