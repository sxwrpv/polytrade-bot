# Risk and Security

PolyTrade can make copy trading easier to configure and monitor. It cannot make real-money prediction-market trading safe or guarantee a result.

## Material risks for users

PolyTrade executes real orders. You can lose the full amount deposited or allocated.

Important risks include:

- **Market and resolution risk:** prices can move against you, and a market may resolve differently from your expectation.
- **Liquidity and execution risk:** an order can fail, be skipped, or execute at a different time or price than the source wallet's order.
- **Copy divergence:** the source may use hidden hedges, other accounts, transfers, or strategies PolyTrade cannot observe. Copying a profitable wallet does not reproduce its return.
- **Custody risk:** PolyTrade stores an encrypted signer and can sign unattended orders. Application, operator, database, secret, session, or backup compromise can put funds at risk.
- **Infrastructure risk:** networks, indexers, APIs, RPC services, the database, and the copy engine can fail, lag, or return incomplete data.
- **Smart-contract and counterparty risk:** exchange, bridge, relayer, contract, or external-tool failures can affect access or value.
- **Eligibility risk:** you are responsible for legal and geographic eligibility. A website geoblock signal can be advisory, and the exchange makes its own decision.
- **Operator and key-management risk:** configuration mistakes, duplicate engines, lost secrets, leaked keys, or incorrect recovery actions can cause loss.

No setting removes these risks. Use only funds you can afford to lose completely.

## What the controls can and cannot do

Per-source controls include copy ratio, maximum copied position, minimum source position, ignore-below threshold, maximum open positions, maximum source exposure, entry price range, maximum slippage, daily realized-loss limit, and enabled/paused state.

Account controls include a master pause, an account-wide open/reserved notional cap, and revision fencing intended to stop in-flight buys after settings change.

These controls can reject or limit new orders. They do not guarantee against loss, bad resolution, custody failure, stale data, or every race and outage.

## Pause does not close positions

Pause, disable, unfollow, and daily-loss protection block new buys and increases. They do not sell holdings. Existing positions can continue through reductions, manual closes, market resolution, and redemption.

A manual close can also fail because it uses a fill-or-kill order with a price floor. Confirm the resulting position state rather than assuming a button press liquidated it.

## Funding, withdrawal, and redemption boundaries

- Deposit only through a displayed supported asset/network route and test with a small amount first.
- PolyTrade does not expose a withdrawal API or in-app withdrawal workflow.
- Exporting a private key is not the same as a simple in-app withdrawal and requires understanding the signer/funder wallet relationship.
- PolyTrade does not automatically redeem resolved winnings; value can remain claimable on Polymarket.

See [Wallet and Funding](wallet-and-funding.md).

## Protect your account and key

- Start from [@cpolytrade_bot](https://t.me/cpolytrade_bot) and verify the username.
- Back up the exported signer key offline in a secure vault.
- Never share private keys, session cookies, Telegram Mini App data, or deposit credentials.
- Do not paste secrets into support chats, screenshots, shell history, or issue reports.
- Treat anyone requesting your private key as unsafe.
- Monitor open, uncertain, closed, and claimable positions.

Private-key export requires a linked Telegram identity and Telegram verification no older than five minutes. Anyone with an exported key may control the signer, and PolyTrade cannot revoke a copied key.

## Current default limits

- Copy ratio: `1%`
- Maximum copied position: `$15`
- Minimum source position: `$0`
- Ignore own copy below: `$2`
- Maximum open positions: unlimited
- Maximum source exposure: unlimited
- Entry range: `0.10–0.98`
- Maximum slippage: `2%`
- Daily loss: none
- Copying: enabled

Defaults are product behavior, not investment advice or a safety guarantee. Review every setting before following a wallet.

## Authentication details

PolyTrade uses a random 12-hour `polytrade_session` cookie with `Secure`, `HttpOnly`, and `SameSite=Strict`. Only a SHA-256-prefixed digest is stored server-side. Legacy Bearer and `X-API-Token` authentication are rejected.

Telegram login validates signed, fresh `initData`. New wallets require that proof plus acknowledgement of the exact current terms version; consent is persisted separately from the user record. Existing valid browser sessions remain usable, but cannot create a new unlinked wallet. Logout revokes the exact server-side session identified by the cookie hash before clearing the browser cookie. Reopening the Mini App from Telegram authenticates the linked account again automatically.

## Custody and operator security

Signer keys are encrypted at rest with AES-256-GCM using a key derived from `ENCRYPTION_SECRET` through HKDF.

The central failure domain remains material:

- database plus encryption secret can expose signer keys;
- losing the encryption secret can make stored signers unusable;
- application compromise can act through valid sessions; and
- backups need the same protection as production data.

Minimum operator controls include a high-entropy secret generated outside the repository, owner-only files or a secret manager, restricted SSH/cloud access, encrypted and tested backups, credential rotation after suspected exposure, non-root execution, and read-only filesystems where practical.

## Execution safety mechanisms

The engine uses full-book preflight, fill-or-kill orders, signed price ceilings/floors, slippage and price bands, balance/exposure clamps, durable claims, database uniqueness, uncertain-state reconciliation, and a one-active-engine-per-database operating model.

These mechanisms reduce unsafe or duplicate execution; they do not guarantee uptime, execution, or profit.

## Browser and data security

Authenticated access is intended to be same-origin. CORS defaults off and does not allow credentialed cross-origin use. Supabase is backend-only. Caddy limits framing to PolyTrade/Telegram and adds HSTS, `nosniff`, and a referrer policy. The current CSP mainly controls `frame-ancestors`, so cross-site scripting remains high impact.

## Operator incident response

1. Pause new buys or disable engine autostart.
2. Confirm that only one engine is running.
3. Preserve sanitized logs and timestamps.
4. Compare claims and recorded positions with live holdings.
5. Invalidate sessions when required.
6. Rotate affected credentials.
7. Move funds only with verified authority and a clear wallet model.
8. Reconcile and run a controlled small-value test before resuming.

Do not blindly delete uncertain claims or replay failed money-moving requests. Continue in the [Operators hub](operators.md).
