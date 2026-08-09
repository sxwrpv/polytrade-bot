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
- `POLYGON_RPC_URL` — optional HTTP RPC for on-chain polling.
- `COPY_ENGINE_AUTOSTART` — direct runtime defaults on; base Compose defaults off for safety.
- `ENFORCE_FRONTEND_GEOBLOCK` — `1` makes website geoblock a hard order veto; default advisory.

## Risk defaults

- `DEFAULT_COPY_RATIO_PCT` — `1`.
- `DEFAULT_MAX_POSITION_USD` — `15`.
- `DEFAULT_MIN_PRICE` — `0.10`.
- `DEFAULT_MAX_PRICE` — `0.98`.
- `DEFAULT_IGNORE_BELOW_USD` — `2`.
- `MAX_COPY_SLIPPAGE_PCT` — `2`.

Existing follows persist their own settings; changing an environment default does not rewrite saved rows.

## Server and browser

- `HOST`
- `PORT`
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
