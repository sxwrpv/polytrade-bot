# Operators

This hub groups the production material needed to configure, deploy, monitor, and recover PolyTrade safely. Operator documentation is not a substitute for consumer risk disclosure.

## Configure and deploy

- [Configuration](configuration.md) — required secrets, database, Builder credentials, Telegram, engine defaults, and validation.
- [Deployment](deployment.md) — Docker/Caddy topology, safe startup, migrations, upgrades, rollback, and the one-engine rule.
- [Troubleshooting](troubleshooting.md) — symptom-led diagnosis and safe recovery paths.

## Health and monitoring

- [Public health endpoint](https://polytradebot.live/api/health) — process liveness only. It does not prove database, copy-engine, RPC, upstream API, wallet, or order readiness.
- [Deployment monitoring checklist](deployment.md#monitoring) — engine reconciliation, uncertain claims, orders, alerts, upstream latency, disk, DNS/TLS, and engine count.

Do not publish or document IP-only operational access. Keep backend ports, dashboards, databases, SSH, secret stores, and internal health checks access-controlled.

## Risk and incident handling

- [Risk and Security](risk-and-security.md) — custody failure domain, auth behavior, execution safety, eligibility boundary, and incident response.
- [Wallet and Funding](wallet-and-funding.md) — signer/funder relationship, deposit boundaries, export, and redemption.
- [Troubleshooting: copy engine and orders](troubleshooting.md) — reconcile uncertain outcomes before any retry.

## Non-negotiable operating rules

- Protect `.env`, the database, backups, Telegram token, Builder credentials, and `ENCRYPTION_SECRET` as secrets.
- Run exactly one active copy engine for a database and its wallets.
- Keep the base deployment in safe mode until configuration and state are verified.
- Never print full settings or secrets during validation.
- Treat `/api/health` as liveness, not trading readiness.
- Pause new buys before uncertain migrations, handovers, or incident recovery.
- Preserve truthful consumer disclosures about custody, full-loss risk, eligibility, withdrawal, redemption, and pause behavior.

Use [Official Links](links.md) when publishing a public destination. It intentionally excludes direct-IP and private operational endpoints.
