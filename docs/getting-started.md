# Getting Started

Use PolyTrade through [@cpolytrade_bot](https://t.me/cpolytrade_bot). This guide takes you from a verified Telegram launch to a configured first copy.

## Why use PolyTrade?

PolyTrade puts wallet discovery, copy sizing, risk limits, and position monitoring into one Telegram Mini App. You choose the public wallet to follow and set the limits; PolyTrade submits real orders only when its checks pass.

It cannot guarantee a copy, a matching price, or a profit.

## Before you begin

You need a Telegram account, legal eligibility to use Polymarket and PolyTrade, a supported funding asset and network, and an amount you can afford to lose completely.

PolyTrade is custodial and executes real-money orders. The website geoblock check is advisory — Polymarket makes the final exchange decision. Read [Risk and Security](risk-and-security.md) before funding.

## 1. Open the Telegram bot

Open [@cpolytrade_bot](https://t.me/cpolytrade_bot), confirm the username is exactly right, and use its launch button.

Signed Telegram launch data lets PolyTrade verify the launch, restore the wallet linked to your Telegram identity, and issue a 12-hour session. Never paste that launch data into chats, support tickets, or third-party sites.

## 2. Review and create your wallet

Acknowledge the custody, risk, eligibility, and security terms, select **Create Wallet**, then save the public address shown.

Wallet creation works only from a verified Telegram launch. PolyTrade records the terms version, your Telegram identity, and the acceptance time. Without valid Telegram proof, no signer is generated.

A visible address does not prove every upstream indexing and approval step finished. If readiness is unclear, don't fund more or retry orders repeatedly — see [Troubleshooting](troubleshooting.md).

## 3. Back up ownership

In **User**, choose **Export private key**, complete fresh Telegram verification, and store the key offline.

Anyone holding that key controls the signer, and PolyTrade cannot revoke a copy of it. Never send it through Telegram, email, logs, screenshots, or support. The gasless wallet and funder relationship is explained in [Wallet and Funding](wallet-and-funding.md).

## 4. Make a small test deposit

Request deposit addresses in **User**, use only an asset and network shown as supported, verify address, asset, and network, then send a small test amount and confirm the balance before sending more.

Deposits convert into Polymarket collateral (shown as pUSD). Conversion and indexing are not instant, and a matching address format is not proof that a token or network is supported.

There is no withdrawal API and no in-app withdrawal workflow. Read [Wallet and Funding](wallet-and-funding.md) before depositing.

## 5. Choose a source wallet

On **Home**, browse discovered wallets over a 7d/30d/90d window, sort the statistics, or paste a full `0x` address.

Statistics are estimates from public history and may be delayed or incomplete. Past profit does not predict future results, and a source may hold hedges or positions elsewhere.

## 6. Set limits before following

Review each one:

- **Copy ratio** — percentage of the source position used to size yours.
- **Maximum position** — cap for a single copied outcome.
- **Ignore below** — skip copies too small to be worth placing.
- **Entry price range** — reject entries outside your share-price band.
- **Maximum slippage** — reject execution too far from the reference price.
- **Trader exposure** — cap recorded and reserved notional per source.
- **Daily loss** — stop new buys after the UTC realized-loss limit.
- **Maximum open positions** — cap concurrent copies per source.

Defaults are deliberately small. No default and no limit makes trading safe or prevents loss.

## 7. Follow and monitor

Following aligns you toward the source wallet's *current* holdings, not only its future trades: fast detection starts immediately, and reconciliation can pick up positions the source already held.

Watch **Home**, **Positions → Open**, **Positions → Activity**, and **User**. The display refreshes periodically and is not a tick-by-tick terminal. Check the skipped, failed, uncertain, closed, resolved, and claimable states rather than assuming every source change was copied.

## 8. Pause or exit deliberately

Pausing the account, pausing a wallet, or unfollowing blocks new buys. It does **not** sell what you hold — existing positions continue through reductions, manual closes, resolution, and redemption.

To exit now, close the position manually and pick an acceptable slippage limit. A fill-or-kill sell fails when the full amount isn't available at or above its price floor.

Resolved winning positions are not redeemed automatically and stay claimable until redeemed through Polymarket.

## First-copy checklist

- [ ] I launched from `@cpolytrade_bot` and verified the username.
- [ ] I understand PolyTrade is custodial and can lose my full deposit.
- [ ] I am eligible to use the service and Polymarket.
- [ ] I backed up the private key offline without sharing it.
- [ ] I verified a small test deposit before sending more.
- [ ] I understand my ratio, position cap, price, slippage, exposure, and loss limits.
- [ ] I reviewed the source wallet's concentration and data limitations.
- [ ] I know pausing does not sell existing positions.
- [ ] I know there is no in-app withdrawal and no automatic redemption.

## Next step

Read [How PolyTrade Works](core-concepts.md), then [Copy Trading](copy-trading.md).
