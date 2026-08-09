# PolyTrade Documentation

Run and understand a real-money Polymarket copy-trading service.

[Open the app](https://polytradebot.live) · [Interactive API](https://polytradebot.live/api/docs) · [Health endpoint](https://polytradebot.live/api/health)

> [!WARNING]
> PolyTrade is custodial and submits real orders. Funds can be lost in full. Read [Risk and Security](risk-and-security.md) before depositing.

## Getting started

- [Getting Started](getting-started.md) — create a wallet, fund it, follow a trader, and verify the first copy.
- [Core Concepts](core-concepts.md) — shares, prices, positions, reconciliation, and custody.

## Copy trading

- [Copy Trading](copy-trading.md) — detection, sizing, order submission, pauses, exits, and divergence.
- [Wallet and Funding](wallet-and-funding.md) — gasless wallets, bridge deposit addresses, balances, export, and redemption.
- [Risk and Security](risk-and-security.md) — loss risks, controls, authentication, custody, and operator duties.

## Build and operate

- [API Reference](api-reference.md) — routes, cookie authentication, request models, and operational notes.
- [Configuration](configuration.md) — environment variables, defaults, and safe production settings.
- [Deployment](deployment.md) — Docker, Caddy, database, copy-engine safety, upgrades, and rollback.
- [Troubleshooting](troubleshooting.md) — symptoms, diagnosis, safe resolutions, and escalation points.
- [Glossary](glossary.md) — product and trading terminology.

## How to read these docs

**Users:** Getting Started → Core Concepts → Copy Trading → Wallet and Funding → Risk and Security.

**Operators:** Risk and Security → Configuration → Deployment → Troubleshooting.

**Developers:** Core Concepts → API Reference → Configuration, then FastAPI's generated `/api/openapi.json`.

## Product boundaries

PolyTrade is Polymarket-only, custodial, real-money only, designed for one active copy engine per database, and authenticated through the web or Telegram Mini App.

PolyTrade is not a self-custodial wallet, paper-trading simulator, withdrawal service, automatic redemption service, or promise of identical execution.

## Documentation principles

- Describe the code that exists, not planned features.
- Put risk and custody limitations near every money-moving workflow.
- Use placeholders; never commit addresses, tokens, private keys, Telegram init data, or production cookies.
- Treat OpenAPI as the schema reference and these pages as the workflow reference.
- Update docs and `.env.example` whenever defaults, routes, states, or deployment behavior change.
