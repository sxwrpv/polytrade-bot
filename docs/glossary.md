# Glossary

**Account exposure** — Recorded and reserved copy notional across a user's open strategies.

**Activity** — Recent persisted copy-engine events shown in the app.

**Bridge API** — Polymarket service used for deposit address families and supported-asset conversion into pUSD.

**Claim** — Durable database reservation that fences a pending buy against duplicate submission.

**Copy engine** — Worker that detects source changes, applies controls, submits orders, and reconciles state.

**Copy ratio** — Percentage of source position value used as the raw copy target.

**Copy wallet** — Custodial wallet controlled by PolyTrade for automated signing.

**Daily loss limit** — Per-source realized-loss threshold from 00:00 UTC; blocks new buys, not exits.

**EOA** — Externally owned account controlled by one private key; PolyTrade's fallback mode.

**Equity** — Cash, open-position value, and claimable value represented by the app.

**Exposure** — Recorded/reserved cost notional, not necessarily mark-to-market value.

**FOK** — Fill or kill; the full order fills immediately within price protection or does not fill.

**Follow** — Saved relationship between a user and source wallet with copy/risk settings.

**Gasless wallet** — Builder/relayer wallet path intended to avoid user-funded MATIC for normal trading.

**Indexer** — Service turning exchange/blockchain activity into queryable history; it can lag or truncate data.

**Market resolution** — Final decision determining winning and losing outcome tokens.

**Maximum position** — Aggregate USD cap for one copied outcome, including later increases.

**pUSD** — Polymarket trading collateral representation produced through supported funding routes.

**Price band** — Minimum/maximum share price at which a new entry is allowed.

**Reconciliation** — Comparison of database state, source positions, and actual copy-wallet holdings.

**Risk revision** — Account setting version that fences in-flight buy planning after settings change.

**Signer** — Private-key identity authorizing wallet and exchange actions.

**Slippage** — Difference between reference price and worst permitted execution price.

**Source wallet** — Polymarket wallet observed and copied.

**Uncertain claim** — Buy whose exchange outcome is ambiguous; fenced until live holdings resolve it.

**Unfollow** — Stop new buys from a source wallet; it does not liquidate holdings.
