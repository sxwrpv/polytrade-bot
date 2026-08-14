# Developers

Use this hub for PolyTrade architecture and API contracts. Consumer onboarding belongs in the Telegram Mini App and the [Help Center](README.md), not in direct API calls.

## API resources

- [API workflow and security guide](api-reference.md) — authentication, route groups, errors, freshness, and retry boundaries.
- [Live Swagger UI](https://polytradebot.live/api/docs) — generated interactive reference for the deployed API.
- [OpenAPI JSON](https://polytradebot.live/api/openapi.json) — authoritative deployed request and response schema.
- [ReDoc](https://polytradebot.live/api/redoc) — alternate generated schema view.

## Architecture and core concepts

- [How PolyTrade Works](core-concepts.md) — positions, detection, reconciliation, FOK execution, custody, and balances.
- [Copy Trading](copy-trading.md) — sizing, safety checks, durable claims, exits, and divergence.
- [Risk and Security](risk-and-security.md) — authentication, custody, execution controls, and material boundaries.
- [Configuration](configuration.md) — environment behavior relevant to local development and integration testing.

## Source repository

- [PolyTrade on GitHub](https://github.com/sxwrpv/polytrade-bot)

No license is granted unless the repository contains a license file that says otherwise. Do not describe the project as open source solely because the source is publicly visible.

## Integration boundary

Current browser authentication uses a same-origin, secure session cookie established from verified Telegram Mini App data. CORS defaults off, and credentialed third-party browser integrations are not supported. Add explicit, scoped service authentication before exposing an external integration.

Never copy production cookies, Telegram `initData`, private keys, database credentials, or operator secrets into examples, issues, logs, or chat.
