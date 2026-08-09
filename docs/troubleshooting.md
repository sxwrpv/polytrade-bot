# Troubleshooting

For any real-money issue:

1. decide whether new buys must be paused;
2. check live holdings before retrying;
3. preserve sanitized evidence;
4. reconcile before deleting, replaying, or restarting.

## Source trade was not copied

**Causes:** pause state, tiny calculated size, exchange minimum, price/slippage rejection, balance/allowance, exposure/loss limits, missed fast event, uncertain claim, or indexer lag.

**Diagnose:** review settings and Activity, inspect engine logs around the UTC timestamp, compare live source/copy holdings, inspect claims/events, and allow a full reconciliation cycle.

**Do not:** manually replay before checking for an uncertain exchange result.

## Copy size was unexpected

Check maximum position, existing size, collateral, trader/account exposure, reserved claims, minimum/ignore thresholds, rounding, and current price. Maximum position caps the aggregate outcome position.

## Price or return differs

Compare source fill time/price, PolyTrade detection time, order-book depth, slippage/price band, size/order type, and whether reconciliation found an older holding. PolyTrade cannot recreate past liquidity.

## Position is uncertain

- Pause new buys if scope is unclear.
- Check actual holdings on Polymarket.
- Inspect claim and event state.
- Let reconciliation run.
- Escalate if unchanged across multiple full cycles.

Do not delete the claim; it is the duplicate-order fence.

## Manual close failed

Possible causes: insufficient full-book liquidity, changed wallet position, minimum size, exchange/allowance/network rejection, or resolved tokens that require redemption.

Re-read live state. Increase slippage only if you accept the lower floor.

## Deposit does not appear

Gather transaction hash, source/destination network, token, amount, timestamp, and displayed deposit address family. Check confirmation, Bridge support, conversion, and indexing.

Do not send another large transfer until the first is understood. Wrong-network or unsupported-token deposits may be unrecoverable.

## Wallet exists but trades fail

Gasless deployment or approvals may not have completed. Confirm Builder configuration, inspect readiness logs, verify funder indexing and approvals, check balance/exchange errors, and pause during repair.

Never expose Builder credentials or keys while collecting evidence.

## Telegram login fails

Check that the Mini App was opened from the correct bot, the menu uses production HTTPS, server time is correct, bot token matches, `initData` is fresh, and cookies work in the webview.

Never paste raw `initData` into support chat.

## Private-key export fails

Export needs an active session, linked Telegram identity, and Telegram data under five minutes old. Reopen the Mini App and authenticate again. Plain-browser accounts without Telegram linking cannot use this path.

## API returns 401

The session may be missing/expired, cookie not sent over same-origin HTTPS, legacy header auth may be used, or a legacy session was invalidated. Authenticate again through Telegram; do not weaken cookie flags.

## Health is ok but copying stopped

`/api/health` only checks FastAPI liveness. Check running `COPY_ENGINE_AUTOSTART`, recent reconcile logs, database and upstream health, one-engine-only condition, pause state, and unresolved claims.

## Caddy or HTTPS fails

Verify DNS, ports 80/443, Caddyfile validation, ACME logs, system time, apex and `www`, redirects, and certificate chain. Reload Caddy only; do not recreate the app unnecessarily.

## Duplicate engines suspected

1. Pause new buys.
2. Find local, cloud, launchd, Docker, and manual processes.
3. Stop all but intended production engine.
4. Inspect claims, events, and live holdings.
5. Reconcile before resuming.

## Safe operator report

Include UTC timestamps, route/status, redacted wallet prefix/suffix, public transaction/order IDs, image revision, state names, and sanitized logs.

Never include `.env`, keys, cookies, Telegram bot token/init data, Builder credentials, or database passwords.
