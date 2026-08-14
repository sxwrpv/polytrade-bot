# Getting Started

Use PolyTrade through [@cpolytrade_bot](https://t.me/cpolytrade_bot). This guide takes you from a verified Telegram launch to a carefully configured first copy.

## Why use PolyTrade?

PolyTrade brings wallet discovery, configurable copy sizing, risk limits, and position monitoring into one Telegram Mini App. You choose which public wallet to follow and set your own limits. PolyTrade then checks detected changes and submits real orders only when its checks pass.

It cannot guarantee a copy, a matching price, or a profit.

## Before you begin

You need:

- access to the Telegram account opening `@cpolytrade_bot`;
- legal eligibility to use Polymarket and PolyTrade;
- a supported asset and network for funding; and
- an amount you can afford to lose completely.

PolyTrade is custodial and executes real-money orders. The website geoblock check can be advisory; Polymarket still makes the final exchange decision. Review [Risk and Security](risk-and-security.md) before funding.

## 1. Open the Telegram bot

1. Go to [@cpolytrade_bot](https://t.me/cpolytrade_bot).
2. Use the bot's launch button to open PolyTrade.
3. Confirm that Telegram shows the expected bot username before continuing.

Signed Telegram Mini App data lets PolyTrade verify the launch, restore the wallet linked to your Telegram identity, and issue a fresh 12-hour session cookie. Do not paste Telegram launch data into chats, support tickets, or third-party sites.

## 2. Review and create your wallet

For a new account:

1. Read and acknowledge the custody, risk, eligibility, and security information shown in the Mini App.
2. Select **Create Wallet**.
3. Wait while PolyTrade creates a signer and deposit/funder wallet.
4. Save the displayed public address.

Wallet creation is available only from a verified Telegram launch. PolyTrade records the exact terms version you accepted, your verified Telegram identity, and the acceptance time together with account creation. If Telegram proof is missing, invalid, or expired, no signer is generated.

An address appearing does not guarantee that every upstream indexing or approval step has finished. If readiness is unclear, do not fund more or repeatedly retry orders; check [Troubleshooting](troubleshooting.md).

## 3. Back up ownership

1. Open **User**.
2. Choose **Export private key**.
3. Complete fresh Telegram verification.
4. Store the key offline in a secure vault.

Anyone with this key can control the signer. PolyTrade cannot revoke a copied key. Never send it in Telegram, email, logs, screenshots, or support requests. Understand the gasless wallet/funder relationship described in [Wallet and Funding](wallet-and-funding.md).

## 4. Make a small test deposit

1. Open **User** and request deposit addresses.
2. Choose only an asset/network combination explicitly displayed as supported.
3. Verify the full address, asset, and network.
4. Send a small test amount first.
5. Wait for conversion and indexing, then confirm the cash balance before sending more.

Funding is converted into Polymarket collateral (shown as pUSD in PolyTrade). Conversion and indexing are not instant. A matching address format alone does not prove that a token or network is supported.

PolyTrade has no withdrawal API or in-app withdrawal workflow. Read [Wallet and Funding](wallet-and-funding.md) before depositing.

## 5. Choose a source wallet

On **Home**, you can browse discovered wallets, select a 7d/30d/90d window, sort statistics, or paste a full `0x` address.

Statistics are estimates built from public history and can be delayed or incomplete. Historical profit does not predict future results, and the source may have hidden hedges or positions elsewhere.

## 6. Set limits before following

Review at least:

- **Copy ratio:** percentage used to calculate your target from the source position.
- **Maximum position:** cap for one copied outcome.
- **Ignore below:** skip tiny calculated copies.
- **Entry price range:** reject entries outside your chosen share-price band.
- **Maximum slippage:** reject execution too far from the reference price.
- **Trader exposure:** cap recorded or reserved notional for that source.
- **Daily loss:** stop new buys after the UTC realized-loss limit.
- **Maximum open positions:** cap concurrent copied positions for that source.

Defaults are intentionally small, but no default or limit makes trading safe or guarantees against loss.

## 7. Follow and monitor

Following authorizes PolyTrade to align toward the source wallet's current holdings, not just future trades. Fast detection starts immediately, while full reconciliation can discover positions the source already holds.

Monitor **Home**, **Positions → Open**, **Positions → Activity**, and **User**. The display updates periodically and is not a tick-by-tick trading terminal. Check skipped, failed, uncertain, closed, resolved, and claimable states rather than assuming every source change copied.

## 8. Pause or exit deliberately

Pausing the account, pausing a followed wallet, or unfollowing blocks new buys. It does **not** sell existing positions. Existing holdings can continue through reductions, manual closes, market resolution, and redemption.

For an immediate exit, close the position manually and choose an acceptable slippage limit. A fill-or-kill sell can fail when the full amount is unavailable at or above its price floor.

Resolved winning positions are not automatically redeemed by PolyTrade. They may remain claimable until redeemed through Polymarket.

## First-copy checklist

- [ ] I launched from `@cpolytrade_bot` and verified the bot username.
- [ ] I understand that PolyTrade is custodial and can lose my full deposit.
- [ ] I am eligible to use the service and Polymarket.
- [ ] I backed up the private key offline without sharing it.
- [ ] I verified a small test deposit before sending more.
- [ ] I understand my copy ratio, position cap, price, slippage, exposure, and loss limits.
- [ ] I reviewed the source wallet's concentration and data limitations.
- [ ] I know that pausing does not sell existing positions.
- [ ] I know there is no in-app withdrawal and no automatic redemption.

## Next step

Read [How PolyTrade Works](core-concepts.md), then [Copy Trading](copy-trading.md).
