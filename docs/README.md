# PolyTrade Help Center

PolyTrade lets you follow Polymarket wallets you choose and copy eligible position changes using limits you control. The supported consumer experience starts in Telegram.

[Open Telegram bot](https://t.me/cpolytrade_bot) · [Getting Started](getting-started.md) · [Official Links](links.md)

## What you can do

- Find and review public Polymarket wallets.
- Choose how much of a source wallet's position to copy.
- Set position, exposure, price, slippage, and loss limits.
- Monitor copied positions and account activity from the Telegram Mini App.
- Pause new buying when you want to stop adding exposure.

PolyTrade watches a selected source wallet, checks each detected position change against your settings and available balance, and submits a real order when the checks pass. Timing, price, liquidity, and source-wallet activity can cause your result to differ from the source.

## Start in Telegram

1. Open [@cpolytrade_bot](https://t.me/cpolytrade_bot).
2. Launch PolyTrade from the bot.
3. Review the custody, eligibility, and risk information before creating a wallet.
4. Create your custodial wallet in the Telegram Mini App and back up the private key securely.
5. Make a small test deposit using a displayed supported route.
6. Review already-copied wallets, set conservative limits, and monitor positions.

Read the complete [Getting Started](getting-started.md) walkthrough before funding.

## Before funding: material facts

- **Real money and total-loss risk:** PolyTrade submits real orders. You can lose the full amount deposited or allocated.
- **Custody:** PolyTrade creates and stores an encrypted signer so the service can place unattended orders. This is custodial, not self-custodial.
- **Eligibility:** You are responsible for being legally eligible to use Polymarket and PolyTrade. A website geoblock signal may be advisory; the exchange makes its own order decision.
- **Execution can differ:** Copying a profitable wallet does not reproduce its price, timing, size, hedges, or return. Orders may be skipped or fail.
- **No in-app withdrawal:** PolyTrade does not expose a withdrawal API or in-app withdrawal workflow. Key export requires fresh Telegram verification, and using an exported signer requires understanding the wallet model.
- **No automatic redemption:** Resolved winnings can remain claimable. PolyTrade does not automatically redeem them.
- **Pause is not liquidation:** Pausing, disabling, or unfollowing blocks new buys; it does not sell existing positions.

Only fund an amount you can afford to lose completely. See [Risk and Security](risk-and-security.md) and [Wallet and Funding](wallet-and-funding.md).

## Consumer guides

- [Getting Started](getting-started.md) — bot-first setup, funding, and monitoring existing copied wallets.
- [How PolyTrade Works](core-concepts.md) — plain-language market, wallet, order, and balance concepts.
- [Copy Trading](copy-trading.md) — detection, sizing, order submission, pauses, exits, and divergence.
- [Wallet and Funding](wallet-and-funding.md) — custody, deposits, balances, key export, and redemption.
- [Risk and Security](risk-and-security.md) — material risks, controls, and security practices.
- [Glossary](glossary.md) — product and trading terminology.

## Other audiences

- [Developers](developers.md) — API guide, live Swagger, OpenAPI, architecture, and source repository.
- [Operators](operators.md) — configuration, deployment, monitoring, troubleshooting, and incident guidance.
- [Official Links](links.md) — the canonical public PolyTrade destinations by audience.
