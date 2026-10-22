# Operators

This hub groups the production material needed to configure, deploy, monitor, and recover PolyTrade safely. Operator documentation is not a substitute for consumer risk disclosure.

## Configure and recover

- [Configuration](configuration.md) — required secrets, database, Builder credentials, Telegram, engine defaults, and validation.
- [Troubleshooting](troubleshooting.md) — symptom-led diagnosis and safe recovery paths.

## Health and monitoring

- [Public health endpoint](https://polytradebot.live/api/health) — process liveness only. It does not prove database, copy-engine, RPC, upstream API, wallet, or order readiness.
- Monitor more than `/api/health`: app container and database health, recent engine reconcile activity, unresolved or uncertain claims, failed orders and alert delivery, upstream API and RPC latency, disk and logs, DNS and TLS, and that exactly one engine runs.
- Logs may contain public wallet metadata but must never contain keys, cookies, Telegram init data, or secrets.

Do not publish or document IP-only operational access. Keep backend ports, dashboards, databases, SSH, secret stores, and internal health checks access-controlled.

## Risk and incident handling

- [Risk and Security](risk-and-security.md) — custody failure domain, auth behavior, execution safety, eligibility boundary, and incident response.
- [Wallet and Funding](wallet-and-funding.md) — signer/funder relationship, deposit boundaries, export, and redemption.
- [Troubleshooting: copy engine and orders](troubleshooting.md) — reconcile uncertain outcomes before any retry.

## Withdrawals

Withdrawals are off until `WITHDRAWALS_ENABLED=1` (see [Configuration](configuration.md)). A withdrawal whose pUSD left the wallet without a confirmed delivery moves to `attention`: the app logs `WITHDRAW ATTENTION` at ERROR, Telegram tells the user PolyTrade is following up, and `/api/ready` counts it under `withdrawals_attention`. Two unresolved ones pause withdrawals for everyone. Unless the bridge reported the transfer failed, the app checks an `attention` withdrawal again every five minutes for a day: once the USDC payment to the destination shows up on Polygon, it moves to `confirmed` by itself and Telegram tells the user it is complete. The bridge's `/status` txHash is not always a Polygon transaction, so the payment is read from the chain.

Follow it up with the bridge provider using the bridge address and transaction hash from the log, then close it inside the app container:

    docker compose exec app python -m backend.core.withdraw list
    docker compose exec app python -m backend.core.withdraw resolve <id> --delivered <tx_hash> --yes
    docker compose exec app python -m backend.core.withdraw resolve <id> --refunded "<what came back, where>" --yes

`--delivered` checks the transaction on Polygon before it marks the withdrawal confirmed. Without `--yes` both are dry runs.

## Deposit alerts

The `deposits` loop reads each newly finalized Polygon block for pUSD that Polymarket's onramp minted into a user's wallet, and the bot messages that user once with the amount and a Polygonscan link. A transaction that also moves the wallet's positions out is a payout or a sale, and the log records it as `set aside`. Each deposit is a row in `deposits`, written before its alert, so a rescan, a restart or a second process sends nothing twice. `chain_cursors` holds the last block read: the scan resumes there after downtime, at most 24 hours back. A deposit logs `DEPOSIT <wallet prefix> +$<amount> pUSD tx=<hash>`. A loop that cannot read Polygon goes stale under `loops.deposits` in `/api/ready`. Apply `supabase/migrations/0013_deposits.sql` to lock both tables away from the Supabase API.

## Non-negotiable operating rules

- Protect `.env`, the database, backups, Telegram token, Builder credentials, and `ENCRYPTION_SECRET` as secrets.
- Run exactly one active copy engine for a database and its wallets.
- Keep the base deployment in safe mode until configuration and state are verified.
- Never print full settings or secrets during validation.
- Treat `/api/health` as a liveness check only.
- Pause new buys before uncertain migrations, handovers, or incident recovery.
- Preserve truthful consumer disclosures about custody, full-loss risk, eligibility, withdrawal, redemption, and pause behavior.

Use [Official Links](links.md) when publishing a public destination. It intentionally excludes direct-IP and private operational endpoints.
