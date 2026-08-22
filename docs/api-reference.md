# API Reference

Production base URL: `https://polytradebot.live`

- Swagger UI: `GET /api/docs`
- ReDoc: `GET /api/redoc`
- OpenAPI JSON: `GET /api/openapi.json`

Generated OpenAPI is the field/schema reference. This page describes workflow and security behavior.

## Authentication

Most routes require the same-origin `polytrade_session` cookie. It expires after 12 hours and is Secure, HttpOnly, and SameSite=Strict.

Do not use Bearer tokens or `X-API-Token`; legacy header authentication is rejected.

Local cookie-jar example:

```bash
curl -sS -c cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"init_data":"<fresh Telegram Mini App initData>"}' \
  http://127.0.0.1:8080/api/auth/telegram

curl -sS -b cookies.txt http://127.0.0.1:8080/api/user/me
```

Never paste production `initData` or cookies into docs, shell history, tickets, or chat.

## Public routes

### `GET /api/health`

Returns `{"status":"ok"}`. This is process liveness only; it does not verify database, engine, RPC, upstream APIs, or order readiness.

### `GET /api/public/screener/wallets`

Anonymous, read-only wallet discovery for the standalone Wallet Screener. No session, no cookie.

Query: `period` (`7d` | `30d` | `90d`, default `30d`), `sort` (`pnl` | `winrate` | `volume`), `search`, `limit` (≤100), `offset`, `pnl_min`, `winrate_min` (a fraction, so 0.6 is 60%), `volume_min`, `consistency_ratio_min` (the 0..1 positive close-day ratio), `fill_exit_ratio_min`, `fill_exit_ratio_max`, and `complete_history_only`. An unsupported `period` or `sort` is rejected with 422 rather than silently defaulting, so a response can never be labelled with the wrong window. If both sell/buy count thresholds are supplied, the minimum cannot exceed the maximum; contradictory ranges return 422.

Each response includes the current-page `count`, filtered-result `total`, requested `limit` and `offset`, and `has_more`. Clients should advance by the returned `limit` while `has_more` is true; `count` alone is not the size of the complete filtered result set.

Reads only precomputed `trader_cache` columns. It never recomputes a wallet, never calls Polymarket, and never writes — those belong to the authenticated `/api/traders/*` routes and the background stats loop.

Every wallet carries `period_days`, `history_days`, `history_partial` and `stats_refreshed_at`. A metric that has not been computed is `null`, never `0`, and `active_positions` is `null` until stats have actually been refreshed for that wallet. There is no composite "copyability" score.

### `GET /api/public/screener/wallets/{address}`

One wallet from the cache. A wallet the background loop has never seen returns 404 with an explanatory message — it is not fetched on demand, so an anonymous caller cannot spend upstream API budget.

### `GET /api/public/screener/provenance`

Where the numbers come from and what they do not claim.

Rate limiting: these three routes share a per-client budget of 60 requests per minute and answer 429 with `Retry-After` beyond it.

### `POST /api/auth/telegram`

Validates Telegram `initData`, finds the linked user, and issues a fresh session.

### `POST /api/auth/logout`

Revokes the exact persisted session identified by the cookie hash, then clears the cookie. A failed response must be treated as still logged in and retried.

### `POST /api/user/create-wallet`

Creates a signer/wallet and issues a session. New wallet creation is Telegram-only: the request must carry fresh, valid signed `init_data`, `terms_accepted: true`, and the exact current `terms_version` (`2026-08-14`). Missing proof, an unconfigured Telegram token, invalid/expired proof, or stale/missing consent is rejected before signer generation. The accepted version, Telegram identity, and timestamp are persisted with the new user in one transaction. An already-linked Telegram user receives the existing wallet rather than a duplicate.

Request shape:

```json
{
  "init_data": "<fresh Telegram Mini App initData>",
  "terms_accepted": true,
  "terms_version": "2026-08-14",
  "display_name": null
}
```

Rate limiting is process-local and resets on restart. Never submit wallet creation from a public browser form or fabricate Telegram proof.

## User routes

- `GET /api/user/me?balance=true|false`
- `GET /api/user/deposit-address`
- `GET /api/user/activity?limit=<n>`
- `GET /api/user/pnl?period=7d|30d|all`
- `GET /api/user/equity-series?period=7d|30d|all`
- `GET /api/user/pnl/by-wallet`
- `GET /api/user/settings`
- `POST /api/user/settings`
- `POST /api/user/export-key`

Export requires a linked Telegram identity and `initData` no older than five minutes. Treat the response as a secret.

## Trader routes

- `GET /api/traders/following`
- `GET /api/traders/{address}`
- `POST /api/traders/{address}/follow`
- `POST /api/traders/{address}/settings`
- `DELETE /api/traders/{address}/follow`

Unfollow does not liquidate holdings.

Example settings shape:

```json
{
  "copy_ratio_pct": 1,
  "max_position_usd": 15,
  "min_leader_position_usd": 0,
  "ignore_below_usd": 2,
  "max_open_positions": null,
  "max_total_exposure_usd": null,
  "min_price": 0.10,
  "max_price": 0.98,
  "max_slippage_pct": 2,
  "daily_loss_limit_usd": null,
  "enabled": true
}
```

Use `/api/openapi.json` for validation ranges and nullability.

## Position routes

- `GET /api/positions/open`
- `GET /api/positions/closed`
- `POST /api/positions/close-external`
- `POST /api/positions/{position_id}/close`

Manual closes accept 0–10% slippage and submit FOK sells with a signed floor.

## Status and errors

- `400` — invalid or unsafe state/request.
- `401` — missing, expired, malformed, or legacy auth.
- `403` — action not permitted.
- `404` — resource not found.
- `409` — conflict or reconciliation required.
- `422` — validation failed.
- `429` — wallet-creation rate limit.
- `5xx` — app/upstream failure; order outcome can require reconciliation.

Do not automatically retry a money-moving request after timeout or `5xx`. Check Activity, claims, positions, and live holdings first.

## Freshness and pagination

There is no universal pagination model. Some routes use `limit`; others return bounded current data. Trader statistics and indexer data can lag. API success does not mean every upstream dataset is complete.

## Same-origin policy

Cookie authentication is for the PolyTrade frontend. CORS defaults off and does not support credentialed third-party browser integrations. Add explicit scoped service authentication before building external integrations.

## Schema workflow

When changing an endpoint: update models/routes, run tests, inspect `/api/openapi.json`, update this workflow guide, and never document response fields the implementation does not return.
