# polymarket-copybot — Polished Build Plan & Self-Prompt

> Working spec for the rebuild. Supersedes the original `CLAUDE_CODE_PROMPT.md` where
> they conflict. Everything below is grounded in the live Polymarket API docs (verified
> 2026-06-30), not assumptions.

---

## 0. Locked decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Venue | Polymarket only | Per spec. No other venues, no mock, no paper. |
| Trading | Real orders via `py-clob-client` | Only order library. |
| Wallet model | **EOA (`signature_type` 0) by default; proxy/safe (1/2) configurable** | ⚠️ REVISED phase 5: the docs' Deposit Wallet / POLY_1271 (type 3) is **not in either published Python SDK** (stable py-clob-client = types 0/1/2 only; v2 README also lacks it). EOA is concrete + verifiable now; signer == funder. |
| Gas / approvals | **CLOB trades are off-chain (no per-trade gas); EOA needs a one-time on-chain allowance approval (small MATIC)** | The fully-gasless deposit-wallet/relayer path isn't available in the published SDK. Switching to proxy/safe (type 1/2) later removes the allowance step but needs a derived funder address. |
| Collateral | **pUSD** (not USDC) | Polymarket's current collateral token. Rename everywhere. |
| Custody | **Custodial** — server holds encrypted signer keys, engine auto-trades | Confirmed by owner. Required for hands-off copytrading. |
| Region | Non-US (permitted) | Owner + users outside geoblocked regions. Still call the geoblock check before placing orders. |
| Key encryption | AES-256-GCM with server `ENCRYPTION_SECRET` (at rest); passphrase = export-only second factor | Engine must decrypt autonomously; passphrase can't gate background trades. |

---

## 1. Hard rules (unchanged from original)

1. **Single venue** — delete every legacy venue adapter, key, config, comment.
2. **No paper / mock / simulation** — no `PaperExecutor`, `PaperSettler`, `MockVenue`, `PAPER_RESOLVE_SECONDS`.
3. **No Kelly arb sizing** — replace with portfolio-aware proportional copy sizing.
4. **Real orders only**, via `py-clob-client`.
5. **One venue, one SDK, one API.**

---

## 2. Logic corrections baked into this build

These fix real defects in the original prompt:

1. **Keys encrypted with `ENCRYPTION_SECRET`, not a user passphrase** — the background engine must sign without the user present. Passphrase is an extra factor for `/export-key` only.
2. **Diff state lives in the DB, not memory** — each tick diffs a trader's live positions against the user's own `open` rows in `copy_positions`. A restart must not re-open held positions. In-memory map is a cache only.
3. **`py-clob-client` is synchronous** — every client call wrapped in `asyncio.to_thread(...)` so the engine loop and FastAPI never block.
4. **Funding is real and gasless** — deposit-wallet deploy → bridge-deposit pUSD → one-time relayer approval batch. Onboarding is not "create wallet, done."
5. **Leaderboard is seeded from Polymarket** — `trader_cache` is populated from the official leaderboard endpoint on a schedule; without it, Home is empty.
6. **Portfolio-aware sizing** — copy size = trader's fraction of *their own* bankroll, capped by `max_position_usd`, available pUSD balance, and a per-user portfolio cap. Enforce `MAX_COPY_SLIPPAGE_PCT`. Handle FOK failure / partial fills in thin books.
7. **Resolution handled** — positions with `redeemable: true` from the data API mean the market resolved; realize PnL and redeem (CTF). Not only trader-initiated closes.
8. **Auth hardened** — `/export-key` gated behind passphrase; design for `X-Signature` over a nonce, not address-only, for any fund-affecting route.
9. **Verify-before-code** — endpoints below are confirmed against live docs; reconfirm response shapes with one probe per endpoint before wiring.

---

## 3. Verified Polymarket API reference

Hosts: `clob.polymarket.com` · `gamma-api.polymarket.com` · `data-api.polymarket.com`. Chain 137.

### Reads (no auth)
- **Leaderboard (seed traders):** `GET data-api/v1/leaderboard`
  params: `category` (OVERALL…), `timePeriod` (DAY|WEEK|MONTH|ALL), `orderBy` (PNL|VOL), `limit` (1–50), `offset` (0–1000).
  returns `[{rank, proxyWallet, userName, vol, pnl, profileImage, xUsername, verifiedBadge}]`.
- **Positions (copy-engine diff + live PnL):** `GET data-api/positions?user=0x…`
  key fields: `proxyWallet, asset (=token_id), conditionId, size (=shares), avgPrice (=entry), curPrice, currentValue, cashPnl, percentPnl, realizedPnl, redeemable, mergeable, outcome, outcomeIndex, title, slug, endDate, negativeRisk`.
  → unrealized PnL comes free (`cashPnl`/`currentValue`); `redeemable=true` ⇒ resolved.
- **Closed positions / trades / activity:** `data-api/.../get-closed-positions-for-a-user`, `.../get-trades-for-a-user-or-markets`, `.../get-user-activity` — for stats & history.
- **Order book / price:** `GET clob/book?token_id=…`, plus `clob` market-data price/midpoint/spread/tick-size endpoints.
- **Markets (hot):** Gamma `list-markets` / `get-clob-market-info`; sort by volume for the hot-markets grid.
- **Public profile:** Gamma `get-public-profile-by-wallet-address`.

### Trading (auth: derive API creds via `create_or_derive_api_key()`)
- **Place order:** `client.create_market_order(MarketOrderArgs(token_id, amount, side, order_type=OrderType.FOK))` → `client.post_order(signed, OrderType.FOK)`.
- **Client init:** `ClobClient(host, key=signer_pk, chain_id=137, creds=api_creds, signature_type=POLY_1271, funder=deposit_wallet_addr)`.
- **Funding:** Bridge API (`/trading/bridge/deposit` + quote/status) → pUSD. **Approvals:** relayer batch from the deposit wallet (one-time, gasless). **Geoblock:** check before first order.
- **Resolution payout:** CTF `redeem` for `redeemable` positions.

---

## 4. Database schema (deltas from original)

Same tables as the original spec, with these corrections:

- `users`: `id` = **deposit/funder wallet address** (the `proxyWallet` seen in positions); add `signer_address TEXT`; `private_key_enc` = AES-GCM(signer key, `ENCRYPTION_SECRET`); add `deposit_wallet_deployed INTEGER DEFAULT 0`. Keep `display_name, referral_code, referred_by, created_at`.
- `copy_positions`: `trader_address` = copied trader's `proxyWallet`; `token_id` = `asset`; `entry_price` = `avgPrice`; `notional_usd` → semantics = pUSD. Status `open|closed|resolved`.
- `trader_cache`: `address` = `proxyWallet`; add `username, profile_image, x_username, verified INTEGER, volume_usd`; computed `win_rate, consistency_score, total_trades`; `total_pnl`; `last_refreshed`.
- All `*_usdc` / "USDC" labels → **pUSD** (DB comments, API payloads, UI).

---

## 5. The 10-step build plan

| # | Phase | Output | Verify |
|---|-------|--------|--------|
| 1 | **Demolition + skeleton** | Delete legacy venue auth, `pairs.json`, `strategy.py`, `risk.py`, `settlement.py`, `executor.py`, `trader.py`, `telegram_ui.py`, `static/`. Scaffold `backend/`+`frontend/`. Rewrite `requirements.txt` (pin the `py-clob-client` version that exposes POLY_1271 / deposit wallets — check PyPI) + `.env.example`. | `git status` clean baseline commit |
| 2 | **API recon probes** | One live probe per endpoint in §3; freeze exact response shapes into `polymarket.py` docstrings. | curl/httpx returns expected fields |
| 3 | **Polymarket read layer** | Port `PolymarketVenue` → `backend/core/polymarket.py`: `get_orderbook`, `get_positions`, `get_leaderboard`, `get_market`, `get_hot_markets`, `get_trade_history`. No order placement yet. | unit-call each, real data back |
| 4 | **DB layer** | `db/models.py` (schema §4) + `db/database.py` (aiosqlite) + startup init. Source of truth for diff state. | tables created, round-trip insert/select |
| 5 | **Wallet + crypto** | `wallet.py`: `eth_account` signer keygen, deposit-wallet address derivation, AES-256-GCM (secret) + passphrase export layer, `get_clob_client` (POLY_1271+funder), relayer approval batch. All client calls via `asyncio.to_thread`. | create→encrypt→decrypt→client init round-trip |
| 6 | **Real order execution** | `place_market_order` (FOK) with slippage guard, balance check, partial-fill/failure handling + geoblock pre-check. Test in isolation against a funded deposit wallet. | one real small fill confirmed |
| 7 | **Copy engine** | `copy_engine.py`: tick = load active follows → fetch trader positions → **diff vs open `copy_positions`** → open/close/resize/realize-`redeemable`. Portfolio-aware sizing. | sim a follow, watch open/close rows |
| 8 | **Stats + PnL + seeding** | `trader_stats.py` (consistency score + scheduled `trader_cache` seed from leaderboard), `pnl.py` (equity curve, period stats; open-position PnL read live from data API). | leaderboard populated, scores sane |
| 9 | **FastAPI wiring** | `main.py` lifespan (DB init + engine task), `routes_user/traders/positions`, auth dep (address + passphrase-gated export, signature-ready), static mount. | all routes 200 with real data |
| 10 | **Frontend + funding UX + E2E** | Vite/React, `brutalism.css` verbatim, pages/components/Chart.js, onboarding **with fund-wallet (deposit collateral + MATIC) → one-time allowance approval**. Full E2E: leaderboard loads, create wallet, fund, copy a trader, verify a real fill. | end-to-end happy path green |

**Sequencing logic:** read-before-write (2–4 establish ground truth before money moves); **order execution proven in isolation (6) before the engine automates it (7)** — never debug autonomous trading and the CLOB client at once.

---

## 5.5 Low-latency copy path (phase 7.5)

Problem (from the old arb bot): the delay between a leader opening a position and
the bot executing is large, so by fill time the price has moved (>10c). Two
independent fixes, both shipped:

1. **Price-protected execution (the guarantee).** Copy BUYs are **price-capped
   limit (FAK) orders anchored to the leader's fill price** — limit =
   `leader_price * (1 + MAX_COPY_SLIPPAGE_PCT)`, tick-rounded, fill-and-kill. We
   take only liquidity at/within the cap and kill the rest, so we **never pay
   more than the cap**: if the market ran away we partial-fill or skip
   (`no_liquidity_within_cap`) rather than chase. Exits stay market (FOK) — they
   aren't spread-sensitive. (`execution.place_capped_order`)
2. **Faster, per-trade detection (shrinks the miss rate).** The engine runs two
   cadences: a **fast detection loop** (`DETECTION_POLL_SECONDS≈2`) that reacts
   to each leader trade with its exact price, and the **slow reconciler**
   (`COPY_ENGINE_POLL_SECONDS≈30`, position-diff) for missed trades, drift, and
   resolutions. Detection is behind `TradeDetector`:
   - `ActivityPollDetector` (default): polls `/activity?type=TRADE`, ~3–8s e2e.
   - `OnChainDetector` (shipped): polls Polygon `OrderFilled` logs on the v2
     settlement contracts (`0xE111…996B` binary, `0xe222…0F59` neg-risk; topic0
     `0xd543adfd…d8ee`), filtered by maker == leader → exact fill price, ~2–4s.
     Enabled by setting `POLYGON_RPC_URL`; decoding cross-checked against the
     data-api activity (prices match to the cent).

A partial-unique index `uq_open_position_per_token` lets both paths attempt an
open safely (the loser is skipped). Trade-off accepted: capped orders **miss
some copies** when price runs — correct for a copy bot (a 10c-worse entry usually
erases the edge).

## 6. Out of scope (unchanged)

No paper toggle · no mock venues · no other venues · no arb scanning · no Kelly · no Telegram · no email/password auth · no gradients/shadows/border-radius in UI.

---

## 7. Open items for execution time

- ~~Pin exact `py-clob-client` version exposing POLY_1271~~ — RESOLVED phase 5: no published Python SDK (stable or v2) exposes POLY_1271/deposit wallets. Using stable `py-clob-client` with EOA (type 0) default. Env: Python 3.12 via uv (system 3.9.6 was too old for the SDK).
- A small funded EOA on Polygon (collateral + a little MATIC for the one-time allowance) is required to verify steps 6 & 10. Everything before that builds/tests with no funds.
- **Onboarding simplified (2026-07-01):** create-only (import removed), no passphrase collection. `/export-key` gated only by wallet auth as a result (real security tradeoff, done deliberately — see README §"Wallets and custody"). `export_blob`/`encrypt_for_export`/`decrypt_export` are now unused-but-harmless leftovers in `wallet.py`/`models.py`.
- **Gasless funding — resolved, partially.** Verified live against `bridge.polymarket.com/deposit`: any wallet address (including our EOA) can get real deposit addresses across EVM/Solana/Tron/Bitcoin (`{"evm","svm","tron","btc"}` — confirmed exact keys, not `"trx"` as docs summaries imply) that convert USDC/USDT/etc into pUSD automatically — no gas needed for that step. Wired in as `PolymarketClient.create_bridge_address` + `GET /api/user/deposit-address`. **BUT** checked Polymarket's relayer (`/api-reference/relayer/submit-a-transaction`) and confirmed it only accepts `type: "SAFE"` or `"PROXY"` — plain EOA wallets are excluded by design, so the one-time on-chain allowance approval still needs real MATIC gas on our EOA model. Making *that* gasless too requires actually implementing Safe/Proxy signature-type funder derivation (still deferred — see wallet model note above), not a quick fix.
- **CRITICAL — migrated from `py-clob-client` (v1) to `py-clob-client-v2` (2026-07-01).** While investigating gasless Safe/Proxy support, discovered via GitHub's API (not a summary) that `Polymarket/py-clob-client` was archived 2026-05-25 (`archived: true`, `pushed_at: 2026-05-25`) — permanently, no more fixes ever. Worse: diffing the ABIs the v2 client ships (`_EXCHANGE_V1_ABI_JSON` vs `_EXCHANGE_V2_ABI_JSON`) confirms the signed Order struct genuinely changed — v1 has `taker/expiration/nonce/feeRateBps`, v2 replaces those with `timestamp/metadata/builder`. Our entire execution layer had been signing the v1 struct this whole build; if the live exchange has moved order-matching to the V2 contract (plausible, unconfirmed without a funded-wallet test), **every real order we ever placed would likely have been rejected** — this was a bigger, more foundational risk than the original gasless-deposit ask. Migrated `wallet.py`/`execution.py`/`copy_engine.py`/`routes_user.py` to `py_clob_client_v2` (confirmed compatible: same constructor shape, `side` still a plain str field despite the README showing a `Side` enum, `MarketOrderArgsV2`/`OrderArgsV2` are literally the same objects as `MarketOrderArgs`/`OrderArgs`; only breaking change was `create_or_derive_api_creds` → `create_or_derive_api_key`). Full regression suite re-verified clean. v2 also exposes `SignatureTypeV2.POLY_1271` (confirmed via introspection) — the deposit-wallet type from the original docs quickstart, unavailable in v1 — but funder-address derivation/deployment for it is still unresearched, so `funder_for()` still only implements EOA. Real order placement against the live exchange is still unverified (needs funds) regardless of which client signs it — that gap doesn't close until a funded-wallet test happens.
- **Gasless deposit-wallet trading — investigated further, real blocker found, paused pending owner action (2026-07-01).** Both mainstream Python clients turned out to have real, currently-open, primary-source-verified bugs for anything other than plain EOA: `py-clob-client-v2`'s L1 auth always binds the derived API key to the EOA regardless of `signature_type`/`funder` (github.com/Polymarket/py-clob-client-v2 issues #70, #64, #53 — all open, real repro code, most recent independent confirmation dated 2026-06-30), so POLY_1271/deposit-wallet order placement is currently broken in it, and issue #53 additionally reports plain EOA order placement itself rejected on live mainnet ("maker address not allowed, please use the deposit wallet flow"). One independent user (comment on #70) confirmed a *different*, actively-developed package — `polymarket-client` (`Polymarket/py-sdk`, beta, `pip install --pre polymarket-client`) — correctly places real orders via `AsyncSecureClient`. Installed and introspected it directly: clean typed API (`AcceptedOrder`/`RejectedOrder` Pydantic models, `place_market_order` with native `max_price`/`min_price` params matching our price-cap design, `create()`/`place_market_order()` both natively async — no more `asyncio.to_thread` needed). Live-tested for free (client auth + balance read need no funds): EOA mode works cleanly (`wallet_type: EOA`, real balance/allowance read against the live exchange). **Gasless/deposit-wallet mode requires a Builder API Key or Relayer API Key** (`UserInputError: Gasless transactions require a Builder API Key or Relayer API Key`) — an account-level credential from Polymarket's own builder program (`polymarket.com/settings?tab=builder`; reportedly three parts: `POLYMARKET_BUILDER_API_KEY`/`_SECRET`/`_PASSPHRASE`, unverified beyond a search summary — confirm on the actual page), which only the account owner can obtain. **Paused here at the owner's choice** — owner is getting the key before the next migration step. `polymarket-client` is pip-installed in `.venv` but NOT yet wired into `wallet.py`/`execution.py`; the app still runs on the `py-clob-client-v2` migration from earlier today. Next session: once the Builder/Relayer key is in hand, migrate `wallet.py`/`execution.py`/`copy_engine.py`/`routes_user.py` to `polymarket-client`'s `AsyncSecureClient` (gasless mode with the key), reusing the design already scoped: `create()` derives+registers the deposit wallet in one call (`users.id` becomes `client.wallet`, learned post-creation, not precomputed), `setup_trading_approvals()` replaces `ensure_allowances`, `place_market_order(max_price=/min_price=)` replaces the hand-rolled FAK-limit-order plumbing in `execution.py` (keep `quote_buy`/`quote_sell`/`slippage_ok` as local pre-flight checks; parse `AcceptedOrder.making_amount/taking_amount` for fill accounting — BUY: making=pUSD spent, taking=shares received; SELL: making=shares given, taking=pUSD received, unverified assumption pending a real fill).
- **Gasless deposit-wallet trading — SHIPPED, confirmed working end-to-end (2026-07-01).** Owner obtained a Builder API Key from `polymarket.com/settings?tab=builder`. Migrated `wallet.py`/`execution.py`/`copy_engine.py`/`routes_user.py`/`deps.py` off `py-clob-client-v2` onto `polymarket-client`'s `AsyncSecureClient`, gasless by default. `make_clob_client(funder=None)` with the Builder key configured derives+deploys a fresh Deposit Wallet automatically (`AsyncSecureClient.create(wallet=None, api_key=BuilderApiKey(...))`); without a key it falls back to plain EOA (unverified beyond client-auth/balance-read, as before). Hit and resolved two more real issues along the way: (1) the **Relayer API Key is scoped to one specific wallet address** (the one used to generate it on the website) — `RelayerApiKey(key=..., address=...)` fails with an address-mismatch error for any other wallet, so it can't onboard arbitrary new users; switched to the Builder key, which works for arbitrary fresh wallets. (2) `setup_trading_approvals()` bundles approval for 7+ operators including `auto_redeem_operator`, which this Builder key's relayer allowlist rejects (`"operator ... is not in the allowed list"`) — read the SDK source (`_required_trading_approvals()`) to confirm exactly which operators `get_balance_allowance()` actually tracks (3: `standard_exchange`, `neg_risk_exchange`, `neg_risk_adapter`), then replaced the bundled call with `ensure_allowances()`, which approves just those 3 individually via `approve_erc20`/`approve_erc1155_for_all`, skipping `auto_redeem_operator` entirely (it's only needed for automatic claim-on-resolution, not for placing/filling orders). Also added `wait_wallet_ready()` — a freshly deployed wallet takes ~5-10s to be indexed by Polymarket's backend before balance/allowance reads succeed; every path that just created a wallet retries through that window rather than surfacing a false error. **Verified live, multiple times:** direct HTTP API test showed a real deposit-wallet address (`0x831A...`) distinct from its signer (`0x1C22...`), with a successful live balance read; `verify_wallet.py`'s full live test confirmed `MAX_UINT256` allowances actually set on-chain for all 3 core operators. `/api/user/create-wallet` and `/api/user/me` now return a `gasless` bool (funder != signer) so the frontend can show "GASLESS WALLET, no MATIC ever needed" and hide the EOA MATIC warning accordingly (`Onboarding.jsx`, `User.jsx`, `DepositAddresses.jsx`). Also hardened for the relayer's real rate limiting (hit repeatedly during heavy verification, self-inflicted from rapid repeat signups, not a code bug): `make_clob_client()` retries `RateLimitError` with backoff (2s/4s) before raising, and `create-wallet` wraps client creation in its own try/except returning a clean `503` instead of a raw `500` if it's still rate-limited after retries — confirmed working live (clean message shown, UI recoverable). **Still the one standing gap, unchanged from before this work:** a real trade/fill against actual funds has never been tested — needs a genuinely funded wallet; everything up to placing the order (wallet creation, deployment, approvals, balance reads) is now confirmed working live.
- **Security + mass-use hardening pass (2026-07-02), full-codebase review.** CRITICAL: the MVP X-Wallet-Address header auth was a real theft vector — wallet addresses become public on-chain the moment the bot trades, and `/export-key` returned the plaintext signer key behind that "auth". Replaced with secret per-user Bearer session tokens (`users.api_token`, issued once at create-wallet, startup-backfilled for old rows, unique-indexed); the address authenticates nothing anymore. **Telegram Mini App shipped**: `/api/auth/telegram` validates Telegram's signed initData (HMAC secret = HMAC_SHA256(key=b"WebAppData", msg=bot_token), freshness-checked) and re-issues the linked account's session — Telegram identity is the durable login (storage wipes can't lock a Telegram user out); create-wallet links `telegram_user_id` and is idempotent per Telegram account; frontend loads the WebApp SDK, auto-signs-in, and still works as a plain web app. Needs `TELEGRAM_BOT_TOKEN` from @BotFather + the Mini App URL configured there (HTTPS deploy required for real Telegram use). Copy-engine money-path bugs found in review and fixed with regression tests: (1) fast-path opens recorded the leader's single trade size as `trader_shares` instead of their total position — a small top-up to a large position read as a huge ratio increase and triggered runaway resize-ups; (2) any leader SELL full-exited the copy, which the reconciler then re-bought (churn, paying the spread twice) — exits are now proportional to the leader's reduction (≥95% = full close); (3) unbounded cursor/dedupe memory growth — pruned on unfollow + bounded; (4) clients closed on shutdown. Added per-IP create-wallet rate limiting (`CREATE_WALLET_RATE_LIMIT`, default 3/hr) since wallet creation hits the shared relayer. UX: balance actually loads now (was permanently "—"), CopyText feedback on all copyable values, live follow-count updates, 30s position auto-refresh, safe-area insets. Verified: 26-check TestClient API regression (incl. forged/valid initData, idempotent Telegram onboarding, address-as-auth rejected), new engine tests, full suite green, browser click-through clean incl. mobile viewport.
- **Caution on WebFetch summaries for anything money-critical:** one docs-page fetch during this investigation contained an empirically-false claim (that `clob.polymarket.com` reads were broken — disproven by a live call seconds later) alongside a separately-confirmed-true claim (the order struct change, verified via the actual shipped ABI). Don't trust a summarized fetch's specifics for anything that gates a real-money decision — verify primary sources (GitHub API, actual package introspection, live calls) before acting, exactly as this finding did.
