# PolyTrade

Copy real Polymarket positions from wallets you choose, with your own allocation and risk limits.

[Open PolyTrade](https://polytradebot.live) · [Documentation](docs/README.md) · [API reference](docs/api-reference.md) · [Risk disclosure](docs/risk-and-security.md)

> [!WARNING]
> PolyTrade is custodial, executes real orders, and can lose all funds deposited to it. Copying a profitable wallet does not reproduce that wallet's returns. Delay, liquidity, price movement, market resolution, infrastructure failure, and custody risk all matter.

## Start here

- **Use the app:** [Create and fund a wallet](docs/getting-started.md)
- **Understand execution:** [How copy trading works](docs/copy-trading.md)
- **Configure limits:** [Risk and security](docs/risk-and-security.md)
- **Run the service:** [Deployment guide](docs/deployment.md)
- **Integrate with the backend:** [API reference](docs/api-reference.md)
- **Resolve a problem:** [Troubleshooting](docs/troubleshooting.md)

## What PolyTrade does

PolyTrade watches selected Polymarket wallets, detects changes in their positions, calculates a copy size from your settings, and submits a real order from your PolyTrade wallet when all safety checks pass.

The platform includes:

- a React web app and Telegram Mini App;
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
  main.py              FastAPI app and background task lifecycle
  api/                 auth, user, trader and position routes
  core/                detection, execution, copy engine, wallet and PnL logic
  db/                  SQLite/Postgres access and schema
frontend/
  src/                  React application
  dist/                 production build served by FastAPI
supabase/migrations/    Postgres schema and security migrations
deploy/macmini/         legacy local deployment tooling
tests/                  backend, safety and deployment contracts
docs/                   user, API and operator documentation
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

Before creating a wallet, configure a strong `ENCRYPTION_SECRET`. Builder credentials are required for the intended gasless wallet path. See [Configuration](docs/configuration.md).

## Tests

```bash
source .venv/bin/activate
python -m pytest -q

cd frontend
npm run build
```

## Documentation

The documentation is arranged like a product guide rather than a build log:

1. [Getting Started](docs/getting-started.md)
2. [Core Concepts](docs/core-concepts.md)
3. [Copy Trading](docs/copy-trading.md)
4. [Wallet and Funding](docs/wallet-and-funding.md)
5. [Risk and Security](docs/risk-and-security.md)
6. [API Reference](docs/api-reference.md)
7. [Configuration](docs/configuration.md)
8. [Deployment](docs/deployment.md)
9. [Troubleshooting](docs/troubleshooting.md)
10. [Glossary](docs/glossary.md)

## License and responsibility

No license is granted unless a license file says otherwise. Operators are responsible for access controls, secrets, legal eligibility, backups, monitoring, incident response, and all real-money trading consequences.
