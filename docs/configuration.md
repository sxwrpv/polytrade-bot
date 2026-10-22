# Configuration

Copy `.env.example` to `.env`, set owner-only permissions, and keep it out of version control.

```bash
cp .env.example .env
chmod 600 .env
```

Never put real values in docs, commits, issues, screenshots, or chat.

## Required production settings

### `ENCRYPTION_SECRET`

High-entropy root secret used to protect all stored signers. Losing it can make wallets unrecoverable; exposing it with the database can expose keys.

### `DATABASE_URL`

Async Postgres/Supabase URL. If absent, PolyTrade uses SQLite at `DB_PATH`.

### Builder credentials

- `POLYMARKET_BUILDER_API_KEY`
- `POLYMARKET_BUILDER_SECRET`
- `POLYMARKET_BUILDER_PASSPHRASE`
- `POLYMARKET_BUILDER_CODE`

The first three enable the intended gasless path. Without them, PolyTrade falls back to EOA behavior.

### `TELEGRAM_BOT_TOKEN`

Used for Telegram login validation, account linking, export step-up, and alerts.

## Trading and detection

- `DETECTION_POLL_SECONDS` — fast activity cadence; default `2`.
- `COPY_ENGINE_POLL_SECONDS` — full reconciliation cadence; default `30`.
- `POLYGON_RPC_URL` — optional HTTP RPC for on-chain polling, and the only evidence that lets the engine release an uncertain buy claim (it reads the wallet's own share balance). Without it, uncertain claims are only adopted or kept.
- `UNCERTAIN_CLAIM_EVIDENCE_SECONDS` — how long after its last change an uncertain claim waits before the on-chain balance may settle it; default `900`.
- `LEADER_CASH_CACHE_SECONDS` — how long one reading of a source wallet's free cash (pUSD, USDC.e and native USDC) serves cash-share sizing; default `30`. The reading comes from `POLYGON_RPC_URL` when set, otherwise the SDK's public Polygon endpoint.
- `COPY_ENGINE_AUTOSTART` — direct runtime defaults on; base Compose defaults off for safety.
- `ENFORCE_FRONTEND_GEOBLOCK` — `1` makes website geoblock a hard order veto; default advisory.

## Risk defaults

- `DEFAULT_COPY_RATIO_PCT` — `1`.
- `DEFAULT_CAPITAL_MULTIPLIER` — `1`, for wallets sized by cash share.
- `DEFAULT_MAX_POSITION_USD` — `15`.
- `DEFAULT_MIN_PRICE` — `0.10`.
- `DEFAULT_MAX_PRICE` — `0.98`.
- `DEFAULT_IGNORE_BELOW_USD` — `2`.
- `MAX_COPY_SLIPPAGE_PCT` — `2`.

Existing follows persist their own settings; changing an environment default does not rewrite saved rows.

## Server and browser

The listen address and port are set only by uvicorn's command-line flags
(`--host`, `--port`; see the Dockerfile `CMD`).

- `LOG_LEVEL`
- `HTTP_LOG_LEVEL`
- `HTTP_USER_AGENT`
- `HTTP_TIMEOUT`
- `CORS_ALLOW_ORIGINS`

Prefer same-origin. Do not use wildcard CORS for credentialed workflows.

## Background jobs

- `SEED_ON_START`
- `STATS_REFRESH_AUTOSTART`
- `TRADER_STATS_REFRESH_SECONDS`
- `TRADER_STATS_REFRESH_LIMIT`
- `TRADER_STATS_REFRESH_CONCURRENCY`
- `DISCOVER_WALLETS_TARGET`
- `EQUITY_SNAPSHOT_AUTOSTART`
- `EQUITY_SNAPSHOT_SECONDS` — default five minutes.
- `REDEEM_AUTOSTART` — default off. `1` redeems resolved winning positions back into pUSD through the gasless relayer, which needs the builder credentials. It moves real tokens, so enable it on the single production worker only. `/api/version` reports whether the loop is running (`redeem.auto`), and the Mini App and the homepage word redemption from it.
- `REDEEM_LOSSES` — default `1`. Lost positions are redeemed as well, for $0, so they leave the wallet.
- `REDEEM_SECONDS` — default 30 minutes.
- `REDEEM_CONCURRENCY` — users redeemed at once; default 2.
- `REDEEM_RETRY_SECONDS` — wait before retrying a failed or unconfirmed redeem; default six hours.
- `DEPOSIT_ALERTS_AUTOSTART` — default on wherever `TELEGRAM_BOT_TOKEN` is set. The bot messages a user once for each deposit that lands in their wallet, with the amount and a Polygonscan link (`backend/core/deposits.py`). It only reads Polygon, through `POLYGON_RPC_URL` when set and otherwise the SDK's public endpoint. `0` turns it off.
- `DEPOSIT_POLL_SECONDS` — how often it reads newly finalized blocks; default 30 seconds.

## Withdrawals

Withdrawals send cash out as native USDC on Polygon through Polymarket's bridge (`backend/core/withdraw.py`). They need the builder credentials and run on the single production worker only.

- `WITHDRAWALS_ENABLED` — default off. `1` offers withdrawals and starts their reconcile loop; `0` again is the kill switch. `/api/version` reports `withdrawals.open`, and the homepage and Mini App word withdrawals from it.
- `WITHDRAW_ALLOWED_TELEGRAM_IDS` — comma-separated Telegram user ids. When set, only those accounts can withdraw and `withdrawals.open` stays false; use it for a first real withdrawal.
- `WITHDRAW_MIN_USD` — floor under the bridge's own minimum; default `2`. The higher of the two applies.
- `WITHDRAW_MAX_USD` — per withdrawal; default `500`.
- `WITHDRAW_DAILY_MAX_USD` and `WITHDRAW_DAILY_COUNT` — per user over a rolling 24 hours; defaults `1000` and `3`. Failed withdrawals do not count.
- `WITHDRAW_RATE_LIMIT` and `WITHDRAW_PREVIEW_RATE_LIMIT` — per user, `N/seconds`; defaults `5/3600` and `30/3600`.
- `WITHDRAW_STEP_UP_MAX_AGE` — maximum age of the Telegram launch data that authorizes a withdrawal; default 300 seconds.
- `WITHDRAW_BRIDGE_TIMEOUT` — how long the bridge may take to deliver before a withdrawal needs an operator; default 1800 seconds.

`POLYGON_RPC_URL` is used for receipts and balances when set; otherwise the SDK's public Polygon endpoint is.

## Database

- `DATABASE_URL` — async Postgres connection string.
- `DB_PATH` — SQLite path when Postgres is absent.

SQLite uses one shared WAL connection and fits one-node local use. Postgres is the production path for concurrent API and engine work.

## Rate limiting

- `CREATE_WALLET_RATE_LIMIT` — process-local wallet-creation limit.

This is not distributed and resets on restart.

## Safe production baseline

Keep base Compose in safe mode. Set `COPY_ENGINE_AUTOSTART=1` only in the production override/environment after confirming no other engine is active.

## Validation

```bash
docker compose config

docker compose run --rm --no-deps app \
  python -c 'from backend.config import settings; print("config loaded")'
```

Do not print the settings object; it can contain secrets.
