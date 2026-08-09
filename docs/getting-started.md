# Getting Started

Create a PolyTrade wallet, fund it, choose a source wallet, set limits, and monitor the first real copy.

## Before you begin

You need access through the Telegram Mini App or same-origin web app, a supported asset/network for funding, legal eligibility to use Polymarket, and an amount you can afford to lose completely.

The website geoblock check is advisory by default. The exchange still makes the final order decision.

## 1. Create or restore your session

Open PolyTrade from Telegram. Signed Mini App data lets the backend restore a linked wallet and issue a fresh 12-hour session cookie.

For a new account:

1. Select **Create Wallet**.
2. Wait while PolyTrade creates a signer and deposit wallet.
3. Save the displayed public address.
4. Do not assume the wallet is trade-ready only because an address appeared; upstream indexing or approvals can finish late.

Plain-browser creation exists, but durable recovery and key export depend on a linked Telegram identity. Telegram is the supported user path.

## 2. Back up ownership

1. Open **User**.
2. Choose **Export private key**.
3. Complete fresh Telegram verification.
4. Store the key offline in a secure vault.

Anyone with the key controls the signer. PolyTrade cannot revoke a copied key. Never send it in chat, email, logs, screenshots, or support tickets.

## 3. Fund the wallet

Open **User** and request deposit addresses. The Bridge API can return EVM, Solana, Bitcoin, and Tron address families. Send only a displayed supported asset/network combination.

Funding is converted into Polymarket USD (pUSD). Conversion and indexing are not instant. Start with a small transfer and verify the balance before sending more.

See [Wallet and Funding](wallet-and-funding.md).

## 4. Choose a trader

On **Home**, use the wallet screener to:

- browse discovered wallets;
- select a 7d, 30d, or 90d window;
- sort by consistency, PnL, win rate, volume, or PnL quality;
- search a profile or paste a full `0x` address.

Windowed statistics are local estimates from public history and can be incomplete. Historical profit does not predict future profit.

## 5. Configure the copy

Review:

- **Copy ratio:** percentage of the leader's current position value.
- **Maximum position:** aggregate cap for one copied outcome.
- **Ignore below:** skip tiny calculated copies.
- **Entry price range:** reject entries outside the share-price band.
- **Maximum slippage:** reject execution too far from reference price.
- **Trader exposure:** cap recorded/reserved notional for that trader.
- **Daily loss:** stop new buys after the UTC realized-loss limit.
- **Maximum open positions:** cap concurrent positions for that trader.

Defaults are small, but not a safety guarantee.

## 6. Follow and monitor

Following starts fast detection now. Full reconciliation can still discover and copy positions already held by the source wallet. Treat following as permission to align with current holdings, not only future trades.

Monitor Home, Positions → Open, Positions → Activity, and User. The UI is periodic, not a tick-by-tick trading terminal.

## 7. Pause or stop safely

Pausing the account, pausing a followed wallet, or unfollowing blocks new buys. It does **not** sell existing positions. Existing positions can continue through reductions, exits, and resolution handling.

For an immediate exit, close the position manually and choose an acceptable slippage limit. Manual closes use FOK market orders with a signed price floor.

## First-trade checklist

- [ ] Telegram restored the correct wallet.
- [ ] Private key is backed up offline.
- [ ] A small deposit appears in the balance.
- [ ] Copy ratio and position cap are understood.
- [ ] Slippage, price band, exposure, and daily loss are configured.
- [ ] Source-wallet concentration and history were reviewed.
- [ ] Open, uncertain, and claimable positions are monitored.
- [ ] You know pause/unfollow is not liquidation.
- [ ] You know resolved winnings may need redemption on Polymarket.

## Next step

Read [Core Concepts](core-concepts.md), then [Copy Trading](copy-trading.md).
