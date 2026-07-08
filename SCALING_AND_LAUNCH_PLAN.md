# Scaling & Launch Plan — Hosting polytrade for Mass Use

> Scope: what it takes to move polytrade from a single-laptop, invite-only build
> to a hosted product that many users can safely use — plus the go-to-market
> content plan. Written against the current codebase (FastAPI + SQLite +
> `polymarket-client`, custodial keys, home tunnel).

---

## 0. Reality check (read this first)

polytrade holds **real user money** and **custodies private keys**. That makes
"mass use" fundamentally different from scaling a normal SaaS: the blockers are
legal, custody-security, and Polymarket's rate limits — **not** servers. You can
buy compute in an afternoon; you cannot buy your way out of a key-honeypot
breach or a regulator.

**Decisions locked (owner, 2026-07):** it ships as a **hosted, custodial
product** (not open-source, not self-host). **Legal/compliance is deferred** —
parked in §2 as a known item to revisit before public/mass launch, not resolved.

That leaves two **active** engineering gates before scale: **key-custody
security** and **Polymarket rate-limit capacity** (§2). Because custody is now a
locked-in product decision, the key-security gate is *more* important, not less —
you're the one holding the honeypot. Everything in §3–§6 and §9 is standard
engineering and can proceed now.

---

## 1. Where the current build breaks under load

Specific chokepoints in the code as it stands today:

| Component | Today | Breaks at scale because |
|---|---|---|
| DB | SQLite (`copybot.db`, aiosqlite) | single-writer; concurrent users serialize/lock. No PITR backups. |
| Process model | API + copy engine + snapshot loop + stats crawler all in **one** uvicorn process (`main.py` lifespan) | can't scale API replicas without double-running the engine; one crash takes everything. |
| Per-user CLOB client | built lazily, cached in-process (`app.state.clients`, `engine._clients`) | in-memory, per-process; lost on restart (expensive rebuild); un-shareable across replicas. |
| Copy engine | one loop over all active follows every 2s/30s | linear in follows; unbounded as users grow. |
| Equity snapshots | loop over **all** users every 5 min, each = 1 balance + 1 positions call | 1,000 users = ~3–4 Polymarket calls/sec just for snapshots, on top of copy polling. |
| Polymarket API | shared data-api / CLOB / relayer | **already 429s at concurrency 8** (crawler runs at 4). This is the real ceiling. |
| Hosting | laptop/Mac mini behind localhost.run/ngrok/Tailscale tunnel, launchd | home network, single point of failure; not production-grade. |
| Secrets | all signer keys AES-GCM under **one** `ENCRYPTION_SECRET` in a laptop `.env` | one box/DB/secret compromise = every user drained. |

---

## 2. Gates before "mass"

### Gate 1 — Legal / regulatory  *(DEFERRED — owner call, 2026-07)*
> **Parked, not resolved.** Revisit before any public / mass launch; safe to
> defer while the product is invite-only / small and hosted on the owner's own
> setup. Left here so it isn't forgotten. Custody model is **decided: custodial**
> (which is why Gate 2 is now firmly in scope).

When it's picked back up, the checklist:
- Custodying funds + trading on users' behalf can constitute **money
  transmission / VASP / investment-advisory** activity depending on
  jurisdiction. Get a crypto/fintech lawyer before mass onboarding. Not legal
  advice.
- Polymarket geoblocks the US and other regions. The engine already checks
  geoblock per order (`execution.place_market_order`), but **signup must be
  jurisdiction-gated too** before public launch.
- Legal entity; Terms of Service; Privacy Policy; **risk disclosure** as a signup
  gate (the copy already exists in the LEGAL folder); KYC/AML decision.

### Gate 2 — Key-custody security  *(ACTIVE — now the top risk)*
Current model: every user's signer key is AES-256-GCM encrypted with a single
server-side `ENCRYPTION_SECRET` and stored in the DB. At scale that DB is a
**catastrophic honeypot**. Requirements before mass:
- Move `ENCRYPTION_SECRET` (or the wrapping key) into a **KMS/HSM** (AWS KMS,
  GCP KMS, or HashiCorp Vault). Envelope-encrypt each user key; the master key
  never sits on the app box.
- Strict IAM / least-privilege service accounts; access **audit logging** on
  every decrypt; secret rotation.
- Encrypted, access-controlled DB backups (the backup contains the keys — the
  KMS key must be protected *and* recoverable).
- Independent **penetration test** + a bug bounty before open launch; documented
  key-compromise incident-response runbook; evaluate insurance.
- (Custody is a locked product decision, so "don't hold keys" is off the table —
  which makes KMS + audit + pen-test non-negotiable rather than optional.)

### Gate 3 — Polymarket API capacity  *(ACTIVE — hard ceiling)*
The entire product rides Polymarket's public APIs, which already 429 at
concurrency 8. Before mass:
- **Contact Polymarket** for a partnership / higher rate limits / a builder tier.
  Without this there is a firm cap on concurrent active copiers.
- Move leader detection and user-position updates from **polling to websockets**
  (the SDK exposes `clob_user_ws_url` / `clob_market_ws_url` / `rtds_ws_url`).
- One **shared leader feed per trader**, fanned to all its copiers (the engine
  already fans one detector call per trader per tick — extend into a global
  leader registry so 500 people copying one wallet cost one subscription).
- Global **token-bucket rate limiter** (Redis) across every process that touches
  Polymarket hosts.

---

## 3. Target hosting architecture

- **Compute:** containerize (Docker). Fast path: Fly.io / Render / Railway.
  Scale path: AWS ECS-Fargate / GCP Cloud Run. Multiple **stateless API
  replicas** behind a load balancer.
- **Split the processes** (biggest structural change): 
  - *API tier* — stateless, N replicas, serves the SPA + REST.
  - *Engine/worker tier* — singleton (or sharded by `hash(user_id)`) that owns
    the copy engine, snapshot job, and stats crawler. Today these live in the
    API's lifespan; separate them so the API scales without double-running the
    engine.
- **Database:** managed **PostgreSQL** (RDS / Cloud SQL / Neon / Supabase).
  Migrate off SQLite; add pgbouncer pooling; replace the ad-hoc `MIGRATIONS`
  list in `db/models.py` with **Alembic**.
- **Redis:** global rate-limit tokens, shared leader-position cache, and a job
  queue (arq/RQ/Celery) for snapshots + stats refresh.
- **Frontend:** build once, serve via CDN (Cloudflare Pages / Vercel /
  S3+CloudFront). (FastAPI StaticFiles is fine for now.)
- **Edge:** real domain + managed TLS; set the Telegram Mini App URL once in
  BotFather (kills the tunnel-URL-rotation supervisor entirely).
- **Secrets:** KMS/Vault, never `.env` on the box (see Gate 2).

---

## 4. Engine-scaling specifics (this codebase)

- **CLOB clients** can't be shared across replicas → keep them in the worker
  tier; shard users across workers by `hash(user_id)`; warm-rebuild on assign.
- **Snapshots:** replace the tight 5-min all-users loop with a **rate-limited
  queue**; skip paused / unfunded / idle users; the `prune_snapshots` retention
  already bounds storage to chart resolution.
- **Leader detection:** global registry — one websocket per leader wallet, fan
  fills to all followers; drop per-follower polling.
- **Backpressure:** central token bucket + exponential backoff already partially
  present (`PolymarketClient._get` retries 429) — promote it to a shared,
  cross-process limiter.

---

## 5. Reliability & operations

- **Observability:** structured logs → aggregator (Datadog / Grafana Loki);
  metrics (Prometheus): fill latency, 429 rate, engine tick duration, snapshot
  lag, per-user PnL drift; alerting (PagerDuty).
- **Backups:** Postgres PITR; test restores; encrypted (holds keys).
- **Health:** `/api/health` exists; add readiness/liveness probes; graceful
  shutdown already closes clients + engine in the lifespan `finally`.
- **Incident runbooks:** key compromise, stuck engine, Polymarket outage,
  DB failover.
- **CI/CD:** test suite + staging env + deploy pipeline (none today).

---

## 6. Cost & capacity model

- **Capacity is gated by Polymarket rate limits, not compute.** Back out max
  concurrent active copiers per API key from: copy poll cadence (2–30s) +
  snapshot cadence (5 min) + crawler, held under the safe concurrency (~4–8).
  More keys / a partnership is the lever to raise the ceiling.
- **Revenue** = builder-code fees on routed volume (already live, stamped on
  every order). Model **break-even** = infra + support cost vs builder fees per
  *funded* user. Free users who never fund are pure cost — gate acquisition spend
  on funded-user unit economics.

---

## 7. Content / go-to-market plan

> **Positioning (2026-07): personal dev brand behind a real product.** It stays
> a **hosted, closed-source, custodial product** — the brand is *you as the dev
> who built it*, not an open-source repo. Build-in-public as marketing (share the
> architecture, the live-money bugs, the design decisions) while the product
> itself stays proprietary.

### Positioning
- **You, as the dev**: "I build real, on-chain, real-money systems." The bot is
  the flagship artifact — the copy-engine design, the live-money bugs you fixed,
  the screener math, the equity infra. Tell the build story; keep the code closed.
- **The product**: "copy proven Polymarket traders." Transparency-first — real
  PnL, wins *and* losses. Never promise returns.
- **Cheap, always-on hygiene** (not legal-gated, just sane): no guaranteed
  returns; a visible risk line; "not investment advice." Full compliance rides
  with the deferred Gate 1.
- Funnels into **G7 Systems** — the brand doubles as agency lead-gen.

### Channels (prediction-market / crypto native)
- **X (primary)** — the `hermessxd` handle: trader spotlights, transparency
  posts (own live PnL — wins *and* losses), "how copytrading works," reactions to
  big market resolutions.
- **Telegram** — public channel as top-of-funnel; the Mini App bot *is* the
  conversion surface; a users' group for retention/support.
- **Farcaster + Polymarket's own community**; short-form video ("I copied the #1
  Polymarket trader for 7 days" — honest results, including losses).

### Funnel (instrument every step)
Awareness (X/TG) → open Mini App → **create wallet** (frictionless, gasless) →
**fund** → **first copy** → retain (activity feed + equity chart). Track
signup → funded → first-copy → active-copier conversion.

### Cadence (example week)
- **Mon** — market preview / trader spotlight
- **Wed** — transparency post (a real win and a real loss)
- **Fri** — education (how a screener filter or risk cap works)
- **Ongoing** — react to leaderboard movers and big resolutions
- **Launch beats** — waitlist → closed beta (deposit caps) → open

### Metrics
Signups, funded-rate, first-copy-rate, active copiers, D7/D30 retention, avg
deposit, builder-fee revenue, CAC vs LTV.

---

## 8. Phased roadmap

**Phase 1 — Get off the laptop (now; still invite-only).**
Dockerize; deploy api + worker to Fly; Supabase Postgres; `ENCRYPTION_SECRET` →
Fly/Vault secret (KMS later); Cloudflare domain + TLS; Sentry + uptime; backups.
Hosted, not yet mass. **Nothing here is blocked** by the deferred legal gate.

**Phase 2 — Make it scalable (closed beta, deposit caps).**
Split API / worker tiers; Upstash rate-limiter + shared leader feed; websockets;
Supabase-CLI migrations; **Gate 2 hardening (KMS + audit + pen test)**; runbooks;
CI/CD + staging.

**Phase 3 — Mass.**
Polymarket rate-limit partnership (Gate 3) + full Gate 2. **Un-park Gate 1 here**
— entity, ToS, jurisdiction gating, KYC/AML — *before* opening the §7 acquisition
engine to the public. Deferred ≠ skipped; it's the toll gate on the public-launch
lane.

---

### One-line summary
It's a hosted custodial product with legal parked for later. Compute is easy;
the two live gates are **key-custody security** (you hold the honeypot) and
**Polymarket's rate limits** — harden those, ship §3–5 / §9, and keep legal on
the calendar for before public launch.

---

## 9. Recommended concrete stack (solo dev, Supabase-anchored)

Opinionated, minimal-ops, GitHub-native. Stand up the **bold** items first;
everything else is later.

### Containers — Docker
- **Multi-stage Dockerfile**: node stage builds `frontend/dist` → python-slim
  runtime runs uvicorn. One image; `CMD` selects role (`api` vs `worker`).
- **`docker-compose.yml`** for local dev: backend + redis (+ optional
  `supabase start` local stack). Reproduces prod locally.
- The copy engine is a **long-lived asyncio loop** — it must run as an
  always-on process, **not** serverless/Lambda/Vercel functions (they'd kill the
  loop). This is why the host below is container-based.

### Compute host — **Fly.io** (recommended)
- Docker-native, cheap, runs persistent workers. Two process groups:
  - **`api`** — FastAPI, scale to N replicas (stateless).
  - **`worker`** — count = 1: the copy engine + snapshot loop + stats crawler
    (must be a singleton, or sharded by `hash(user_id)` — never double-run).
- Alternatives: Railway / Render (similar). Avoid serverless for the worker.

### Database — **Supabase** (Postgres)
- Use the **pooled** connection string (Supavisor, port 6543) from containers.
- **Migrations via the Supabase CLI** (`supabase migration new …`): port
  `db/models.py` `SCHEMA_SQL` + the `MIGRATIONS` list into versioned SQL; retire
  the ALTER-on-boot pattern.
- PITR + daily backups on the Pro tier (the DB holds encrypted keys — protect
  and test-restore it).
- RLS optional while the backend is the only client (service-role key); enable
  it if the browser ever talks to Supabase directly.
- **Code work (contained but real):** swap `aiosqlite` → **`asyncpg`** behind the
  existing `Database` seam in `db/database.py`. Placeholders `?` → `$1..$n`;
  a few SQLite-isms to port (`INSERT OR IGNORE`, `AUTOINCREMENT` →
  `BIGSERIAL/IDENTITY`, `ON CONFLICT` is compatible). `try_transition`,
  `executemany`, `fetch*` keep their signatures — callers don't change.

### Secrets & keys
- **Fly secrets** (or **Supabase Vault**) for `ENCRYPTION_SECRET`, the
  Polymarket builder key set, and the Telegram token. Never baked into the image.
- Custody hardening later: envelope-encrypt user keys under a dedicated **KMS**
  (AWS/GCP KMS) — Vault/Fly secrets are fine for app config, KMS for the master
  wrapping key (see Gate 2).

### Redis — **Upstash**
- Serverless, generous free tier. Powers the cross-process **rate limiter**,
  the shared **leader-position cache**, and a lightweight **job queue** (`arq`)
  for snapshots/stats so they stop being a tight in-process loop.

### Frontend
- Simplest: keep serving `frontend/dist` from the container (FastAPI
  StaticFiles). CDN upgrade: deploy to **Cloudflare Pages** / Vercel later.

### Edge / domain — **Cloudflare**
- DNS + TLS + proxy. Point the Telegram Mini App button at the stable domain
  **once** — retires the whole tunnel-URL-rotation supervisor.

### CI/CD — **GitHub Actions**
- On push: build image → `flyctl deploy`. Add a **staging** app before prod.

### Observability
- **Sentry** (backend + frontend) — cheapest, highest-value error tracking.
- Fly logs + **BetterStack/UptimeRobot** pinging `/api/health`.
- Prometheus/Grafana only when scale warrants it.

### Minimal viable hosted stack (stand this up first)
**Fly.io (api + worker) · Supabase Postgres · Upstash Redis · Cloudflare ·
GitHub Actions · Sentry.** That's the whole thing — everything else is later.
