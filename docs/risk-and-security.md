# Risk and Security

PolyTrade makes copy trading easier to configure and monitor. It cannot make real-money prediction-market trading safe or guarantee a result.

## Material risks for users

PolyTrade executes real orders. You can lose the full amount you deposit or allocate.

- **Market and resolution risk** — prices move against you; a market can resolve against your expectation.
- **Liquidity and execution risk** — an order can fail, be skipped, or execute at a different time or price than the source's.
- **Copy divergence** — the source may use hedges, other accounts, transfers, or strategies PolyTrade cannot see. Copying a profitable wallet does not reproduce its return.
- **Custody risk** — PolyTrade stores an encrypted signer and can sign unattended orders. Compromise of the application, operator, database, secrets, sessions, or backups puts funds at risk.
- **Infrastructure risk** — networks, indexers, APIs, RPC, the database, and the engine can fail, lag, or return incomplete data.
- **Smart-contract and counterparty risk** — exchange, bridge, relayer, contract, or external-tool failures can affect access or value.
- **Eligibility risk** — legal and geographic eligibility is your responsibility. The geoblock signal is advisory; the exchange decides.
- **Operator and key-management risk** — misconfiguration, duplicate engines, lost secrets, leaked keys, or wrong recovery steps can cause loss.

No setting removes these risks. Use only funds you can afford to lose completely.

## What the controls can and cannot do

Per source: copy ratio, maximum copied position, minimum source position, ignore-below threshold, maximum open positions, maximum source exposure, entry price range, maximum slippage, daily realized-loss limit, and paused state.

Per account: master pause, an account-wide open/reserved notional cap, and revision fencing that stops in-flight buys after settings change.

These controls reject or limit new orders. They do not protect against loss, adverse resolution, custody failure, stale data, or every race and outage.

## Pause does not close positions

Pause, disable, unfollow, and the daily-loss limit block new buys and increases. They never sell holdings: existing positions continue through reductions, manual closes, resolution, and redemption.

A manual close can also fail — it is a fill-or-kill order with a price floor. Confirm the resulting position state instead of assuming the button liquidated anything.

## Funding, withdrawal, and redemption boundaries

- Deposit only through a displayed supported asset/network route, and test with a small amount first.
- There is no withdrawal API and no in-app withdrawal workflow.
- Exporting a private key is not a withdrawal; it requires understanding the signer/funder relationship.
- Resolved winnings are not redeemed automatically and can stay claimable on Polymarket.

See [Wallet and Funding](wallet-and-funding.md).

## Protect your account and key

- Start from [@cpolytrade_bot](https://t.me/cpolytrade_bot) and verify the username.
- Keep the exported signer key offline in a secure vault.
- Never share private keys, session cookies, Telegram launch data, or deposit credentials — including in support chats, screenshots, shell history, and issue reports.
- Treat anyone asking for your private key as hostile.
- Monitor open, uncertain, closed, and claimable positions.

Key export requires a linked Telegram identity and verification no older than five minutes. Anyone holding an exported key controls the signer, and PolyTrade cannot revoke a copied key.

## Current default limits

| Setting | Default |
|---|---|
| Copy ratio | `1%` |
| Maximum copied position | `$15` |
| Minimum source position | `$0` |
| Ignore own copy below | `$2` |
| Maximum open positions | unlimited |
| Maximum source exposure | unlimited |
| Entry range | `0.10–0.98` |
| Maximum slippage | `2%` |
| Daily loss | none |
| Copying | enabled |

Defaults are product behavior, not investment advice or a safety guarantee. Review every setting before following a wallet.

## Authentication details

Sessions use a random 12-hour `polytrade_session` cookie with `Secure`, `HttpOnly`, and `SameSite=Strict`; only a SHA-256 digest is stored server-side. Legacy Bearer and `X-API-Token` authentication are rejected.

Telegram login validates signed, fresh `initData`. A new wallet additionally requires acknowledgement of the exact current terms version, stored separately from the user record. An existing browser session stays usable but cannot create a new unlinked wallet. Logout revokes the exact server-side session named by the cookie hash before clearing it. Reopening the Mini App re-authenticates the linked account automatically.

## Custody and operator security

Signer keys are encrypted at rest with AES-256-GCM under a key derived from `ENCRYPTION_SECRET` via HKDF.

The central failure domain is material: database plus encryption secret exposes signer keys; losing the secret makes stored signers unusable; application compromise can act through valid sessions; and backups need production-grade protection.

Minimum operator controls: a high-entropy secret generated outside the repository, owner-only files or a secret manager, restricted SSH and cloud access, encrypted and tested backups, credential rotation after suspected exposure, non-root execution, and read-only filesystems where practical.

## Execution safety mechanisms

Full-book preflight, fill-or-kill orders, signed price ceilings and floors, slippage and price bands, balance and exposure clamps, durable claims, database uniqueness, uncertain-state reconciliation, and one active engine per database.

These reduce unsafe and duplicate execution. They do not guarantee uptime, execution, or profit.

## Browser and data security

Authenticated access is same-origin by design. CORS is off by default and does not allow credentialed cross-origin use. Supabase is backend-only. Caddy limits framing to PolyTrade and Telegram and adds HSTS, `nosniff`, and a referrer policy. The CSP mainly controls `frame-ancestors`, so cross-site scripting remains high impact.

## Operator incident response

1. Pause new buys or disable engine autostart.
2. Confirm only one engine is running.
3. Preserve sanitized logs and timestamps.
4. Compare claims and recorded positions with live holdings.
5. Invalidate sessions if required.
6. Rotate affected credentials.
7. Move funds only with verified authority and a clear wallet model.
8. Reconcile, then run a small controlled test before resuming.

Never blindly delete uncertain claims or replay failed money-moving requests. Continue in the [Operators hub](operators.md).
