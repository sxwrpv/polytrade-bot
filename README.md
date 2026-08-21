# PolyTrade

Copy real Polymarket positions from wallets you choose, with your own allocation and risk limits. The supported consumer experience starts in Telegram.

**[Open Telegram bot](https://t.me/cpolytrade_bot)** · [Product homepage](https://polytradebot.live/) · [Help Center](https://polytradebot.live/docs) · [Official links](https://polytradebot.live/docs/links)

## Before you fund

PolyTrade is custodial, executes real orders, and can lose all funds deposited to it. Copying a profitable wallet does not reproduce that wallet's returns. Delay, liquidity, price movement, market resolution, infrastructure failure, and custody risk all matter.

## Start here

- **Consumers:** [Open `@cpolytrade_bot`](https://t.me/cpolytrade_bot), then follow the [bot-first Getting Started guide](docs/getting-started.md).
- **Understand execution:** [How copy trading works](docs/copy-trading.md).
- **Review material facts:** [Risk and Security](docs/risk-and-security.md) and [Wallet and Funding](docs/wallet-and-funding.md).
- **Developers:** [Developers hub](docs/developers.md).
- **Operators:** [Operators hub](docs/operators.md).
- **Verify destinations:** [Official Links](docs/links.md).

The website is the **Product homepage**, not the recommended wallet-creation path. Consumer wallet setup and recovery are tied to verified Telegram identity.

## What PolyTrade does

PolyTrade watches selected Polymarket wallets, detects changes in their positions, calculates a copy size from your settings, and submits a real order from your PolyTrade wallet when all safety checks pass.

The platform includes:

- a React product homepage and Telegram Mini App;
- a FastAPI backend;
- a durable copy engine with duplicate-execution fences and reconciliation;
- per-trader and account-level risk controls;
- a custodial wallet encrypted at rest;
- Docker, Caddy, HTTPS, and database deployment support.

## What PolyTrade does not do

- It has no paper-trading or simulated-fill mode.
- It does not guarantee that every source trade will be copied.
- It does not guarantee the same entry price, size, timing, or return as the source wallet.
- Pausing or unfollowing does not liquidate existing positions.
- It has no in-app withdrawal workflow.
- Resolved winnings are not automatically redeemed by PolyTrade.
- The public health endpoint is not proof that the copy engine or upstream services are healthy.

## Execution at a glance

```text
source wallet changes position
        ↓
fast detector observes the change
        ↓
copy engine validates user, strategy, balance, exposure and price
        ↓
durable claim prevents duplicate submission
        ↓
market FOK order is submitted with a signed price ceiling/floor
        ↓
fill is persisted, alerted and continuously reconciled
```

The 30-second full-position reconciliation path remains the correctness layer even when faster detection is enabled.

## Repository map

```text
backend/
  main.py              FastAPI app, documentation routes, and task lifecycle
  api/                 auth, user, trader and position routes
  core/                detection, execution, copy engine, wallet and PnL logic
  db/                  SQLite/Postgres access and schema
frontend/
  src/                  React application
  dist/                 production build served by FastAPI
supabase/migrations/    Postgres schema and security migrations
tests/                  backend, safety and deployment contracts
docs/                   consumer help, developer, and operator documentation
compose.yaml            production service topology
Caddyfile               HTTPS reverse proxy and security headers
```

## Local development

Requirements: Python 3.12+, Node.js 20+, and npm.

```bash
git clone https://github.com/sxwrpv/polytrade-bot.git
cd polytrade-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

cd frontend
npm install
npm run build
cd ..

uvicorn backend.main:app --host 127.0.0.1 --port 8080
```

Before any wallet test, configure a strong `ENCRYPTION_SECRET`. Builder credentials are required for the intended gasless wallet path, and a Telegram bot token is required for supported consumer onboarding. See [Configuration](docs/configuration.md).

## Tests

```bash
source .venv/bin/activate
PYTHONPATH=. python -m pytest -q

cd frontend
npm run build
```

## Documentation

- [Consumer Help Center](docs/README.md)
- [Getting Started](docs/getting-started.md)
- [Core Concepts](docs/core-concepts.md)
- [Copy Trading](docs/copy-trading.md)
- [Wallet and Funding](docs/wallet-and-funding.md)
- [Risk and Security](docs/risk-and-security.md)
- [Developers hub](docs/developers.md)
- [API Reference](docs/api-reference.md)
- [Operators hub](docs/operators.md)
- [Configuration](docs/configuration.md)
- [Deployment](docs/deployment.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Glossary](docs/glossary.md)
- [Official Links](docs/links.md)

## License and responsibility

No license is granted unless a license file says otherwise. Do not infer an open-source license from public source visibility. Operators are responsible for access controls, secrets, legal eligibility, backups, monitoring, incident response, and all real-money trading consequences.
