# Risk and Security

## Risk disclosure

PolyTrade executes real orders. You can lose the full amount deposited or allocated.

Risks include market loss, disputed or unexpected resolution, illiquidity, follower/source divergence, hidden source hedges, custodial compromise, infrastructure or indexer failure, smart-contract/counterparty failure, regulatory restrictions, and operator error.

No setting removes these risks.

## Risk controls

### Per-trader

- copy ratio;
- maximum copied position;
- minimum source position;
- ignore-below threshold;
- maximum open positions;
- maximum trader exposure;
- entry price range;
- maximum slippage;
- daily realized-loss limit;
- enabled/paused state.

### Account

- master pause;
- account-wide open/reserved notional cap;
- risk revision fencing in-flight buys after settings change.

### Current defaults

- Copy ratio: `1%`
- Maximum copied position: `$15`
- Minimum source position: `$0`
- Ignore own copy below: `$2`
- Maximum open positions: unlimited
- Maximum trader exposure: unlimited
- Entry range: `0.10–0.98`
- Maximum slippage: `2%`
- Daily loss: none
- Copying: enabled

Defaults are product behavior, not investment advice.

## What pause means

Pause, disable, unfollow, and daily-loss protection block new buys and increases. They do not sell holdings. Existing positions can continue through reductions, closes, and resolution.

## Authentication

PolyTrade uses a random 12-hour `polytrade_session` cookie with `Secure`, `HttpOnly`, and `SameSite=Strict`. Only a SHA-256-prefixed digest is stored server-side. Legacy Bearer and `X-API-Token` authentication are rejected.

Telegram login validates signed and fresh `initData`. Private-key export requires a five-minute Telegram step-up.

Logout clears the browser cookie but does not currently revoke the persisted session immediately. A copied cookie can remain valid until expiry.

## Custody and key protection

Signer keys are encrypted at rest with AES-256-GCM using a key derived from `ENCRYPTION_SECRET` via HKDF.

Central failure domain:

- database + encryption secret can expose all signer keys;
- losing the secret can lock stored signers;
- app compromise can act through sessions;
- backups require production-grade protection.

Minimum operator controls:

- generate a high-entropy secret outside the repo;
- use owner-only files or a secret manager;
- never put secrets in Git, logs, screenshots, support messages, or docs;
- restrict SSH and cloud access;
- encrypt and test backups;
- rotate affected credentials after suspected exposure;
- run non-root and read-only where possible.

## Execution safety

The engine uses full-book preflight, FOK orders, signed ceilings/floors, slippage and price bands, balance/exposure clamps, durable claims, database uniqueness, uncertain-state reconciliation, and one active engine per database.

These controls reduce unsafe execution; they do not guarantee uptime or profit.

## Geoblock behavior

Frontend geoblock output is advisory by default. `ENFORCE_FRONTEND_GEOBLOCK=1` makes it a hard backend veto. The exchange can independently accept or reject orders. Users/operators remain responsible for eligibility.

## Browser and data security

- Authenticated access is intended to be same-origin.
- CORS defaults off and does not allow credentialed cross-origin use.
- Supabase is backend-only; browsers should not query it directly.
- Caddy limits framing to PolyTrade/Telegram and adds HSTS, `nosniff`, and referrer policy.
- Current CSP mainly controls `frame-ancestors`, not every script/style/connect source. XSS remains high impact.

## Incident response

1. Pause new buys or disable engine autostart.
2. Confirm only one engine runs.
3. Preserve sanitized logs and timestamps.
4. Compare claims/positions with live holdings.
5. Invalidate sessions.
6. Rotate affected credentials.
7. Move funds with verified authority when safe.
8. Reconcile and run a controlled small-value test before resuming.

Do not blindly delete uncertain claims or replay failed requests.

## User checklist

- Back up the key offline.
- Start with a small deposit.
- Bound position and exposure.
- Set a daily loss limit.
- Review source history and concentration.
- Monitor uncertain and claimable states.
- Remember that pause is not liquidation.
- Never share cookies, Telegram init data, or private keys.
