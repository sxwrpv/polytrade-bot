# polymarket-copybot

A real-money Polymarket copytrading platform. Follow any wallet — from the
official leaderboard or pasted in manually — and the engine mirrors their
opens, resizes, and exits with your own capital, in your own custodial wallet.

Polymarket only. No paper mode, no simulation, no other venue. Every trade is
a real order signed and submitted via `polymarket-client`'s AsyncSecureClient
(gasless deposit wallets via the Builder program). See `BUILD_PLAN.md` for
the full design history and the hard rules this was built against.

## What this is NOT

- Not paper trading. There is no mock mode and no simulated fills — trades
  execute for real, subject to real preconditions (geoblock, liquidity,
  slippage cap, balance) checked before submission.
- Not risk-free. Prediction markets carry risk of total loss, and you are
  responsible for the funds in your custodial wallet.

## Architecture

```
backend/
  main.py            FastAPI entrypoint — lifespan starts the DB + CopyEngine
  config.py           Polymarket-only config (hosts, risk defaults, secrets)
  db/                 SQLite (aiosqlite): users, followed_traders, copy_positions,
                       trade_events, trader_cache
  core/
    polymarket.py      read client: leaderboard, positions, orderbook, markets,
                        trade history, geoblock (see API_RECON.md)
    wallet.py           keypair generation + AES-256-GCM encryption (at-rest +
                        passphrase export) + CLOB client construction
    execution.py        real order placement — market FOK (exits) and
                        price-capped limit FAK (entries, anchored to the
                        leader's fill price so a slow reaction never means a
                        worse entry — see BUILD_PLAN §5.5)
    detection.py         fast per-trade leader detection (activity poll or
                        on-chain OrderFilled logs) feeding the copy engine
                        within seconds, instead of a single slow poll
    copy_engine.py       the tick loop: diff each followed trader's live
                        positions against the DB, open/resize/close/resolve
    trader_stats.py      leaderboard seeding + consistency scoring
    pnl.py               user equity curve + period PnL stats
  api/                routes_user / routes_traders / routes_positions
frontend/             React + Vite, cyber-brutalism UI (see BUILD_PLAN §"Frontend")
```

### Position lifecycle

`leader opens -> detected (fast poll or on-chain) -> price-capped copy order
placed -> position tracked in copy_positions -> leader resizes/exits or market
resolves -> position updated/closed, PnL realized`. Diff state lives in the
database, not memory, so a restart never re-opens a position that's already
held.

### Risk settings (per copied wallet)

Each wallet you follow is configured independently: allocation %, max
position size, max slippage vs. the leader's price, max total exposure to
that trader, a daily loss limit, and a pause switch. Set via
`POST /api/traders/{address}/settings`.

## Quick start

```bash
cd polymarket-copybot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set ENCRYPTION_SECRET at minimum

# backend
uvicorn backend.main:app --host 0.0.0.0 --port 8080

# frontend (separate terminal, dev mode)
cd frontend && npm install && npm run dev
```

For a production-style single-process run, build the frontend first
(`npm run build` — outputs `frontend/dist`) and FastAPI serves it directly at
`/`; no separate frontend server needed.

## Configuration

See `.env.example` for the full list. The load-bearing ones:

- `ENCRYPTION_SECRET` — encrypts signer private keys at rest (AES-256-GCM).
  Required; the app refuses to create wallets without it.
- `DETECTION_POLL_SECONDS` / `COPY_ENGINE_POLL_SECONDS` — fast per-trade
  detection cadence vs. the slower full-reconcile cadence.
- `MAX_COPY_SLIPPAGE_PCT` — default cap on how far a copy order's price may
  drift from the leader's fill price before it's skipped instead of chased.
- `POLYGON_RPC_URL` — optional; if set, leader-trade detection uses on-chain
  `OrderFilled` logs (faster, attributed) instead of polling the activity API.

## Wallets and custody

The app is custodial: onboarding is create-only (no import, no passphrase) —
it generates an Ethereum keypair per user, encrypts the private key at rest,
and signs orders on the user's behalf so copying can run unattended. Users can
export their private key at any time via `POST /api/user/export-key`, gated
only by wallet auth — there is no passphrase second factor. That's a
deliberate simplification, not an oversight: anyone who can authenticate as a
wallet (i.e. knows its address) can export its key.

**Funding** uses Polymarket's own bridge (`GET /api/user/deposit-address`):
send USDC/USDT (or other supported assets) from Ethereum, Polygon, Arbitrum,
Base, Optimism, BNB, Solana, Tron, or Bitcoin, and it's converted to pUSD in
the wallet automatically — no gas needed for this step, exactly like
Polymarket's own direct-deposit flow. Separately, the wallet still needs a
small amount of MATIC once, for the on-chain USDC allowance approval before
its first trade — this is a real constraint of the EOA wallet model (see
`BUILD_PLAN.md` §wallet model): Polymarket's relayer only covers gasless
transactions for Safe/Proxy wallet types, not plain EOAs, and implementing
proxy-wallet address derivation is deferred, not done.

## Going further

- `BUILD_PLAN.md` — the phase-by-phase build log, every design decision and
  why, and the verified API reference for the endpoints this depends on.
- Signature auth (`X-Signature` over a nonce) to replace the current
  MVP-level `X-Wallet-Address` header auth is the planned upgrade before
  handling meaningful real funds at scale.
