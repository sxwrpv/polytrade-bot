# Deployment

## Production topology

```text
Internet / Telegram
        ↓ HTTPS
      Caddy
        ↓ internal HTTP
FastAPI + React + one copy engine
        ↓
Postgres/Supabase and Polymarket services
```

The Docker image runs one Uvicorn worker as non-root. Caddy is the only published ingress. The app is read-only except for data and temporary mounts.

## Prerequisites

- Linux VPS or equivalent container host;
- Docker Engine and Compose;
- production DNS and open ports 80/443;
- restricted SSH/cloud access;
- Postgres/Supabase;
- Telegram bot and Builder credentials;
- tested backup and rollback access.

## Build and validate

```bash
docker compose build
docker compose config
docker compose run --rm --no-deps app python -m pytest -q
```

Base Compose keeps `COPY_ENGINE_AUTOSTART=0`. Preserve that safe default.

## First start: engine disabled

```bash
docker compose up -d
docker compose ps
```

Verify one app, one Caddy, no public backend port, successful migrations, working frontend/API over HTTPS, Telegram framing, and no engine active elsewhere.

`GET /api/health` checks process liveness only.

## Enable the production engine

Set `COPY_ENGINE_AUTOSTART=1` only in the production environment/override, then recreate the app once:

```bash
docker compose up -d --no-deps --force-recreate app
```

Verify one engine and fresh reconciliation logs. Never run local and cloud engines against the same users/database simultaneously.

## Deploying a Caddyfile change

The Caddyfile is bind-mounted as a single FILE. Any sync that replaces it —
rsync and scp both do, writing a temp file then renaming — gives the host a new
inode while the container keeps holding the old one. `caddy reload` then
succeeds, reports the new config, and changes nothing, because it is re-reading
the stale inode through the mount.

So a Caddyfile change needs the container recreated, not reloaded:

```bash
docker compose up -d --force-recreate --no-deps caddy
```

Verify against the container, never the host:

```bash
docker compose exec caddy grep -c Cache-Control /etc/caddy/Caddyfile
```

Editing the file in place on the box (`nano`, `sed -i` without a rename) keeps
the inode and does work with a plain reload. Syncing does not.

## The screener snapshot

The Wallet Screener service (`trader-screener/`) serves its board from a
cohort snapshot in `./screener-data`, mounted at `/app/data`. On a fresh host
the directory is empty, so the container seeds it once from the copy bundled
in the image; an existing snapshot is never overwritten by a redeploy.

`scripts/refresh-screener-data.sh` re-runs the ingest into that directory, and
the service picks the new file up by mtime (`SNAPSHOT_RELOAD_SECONDS`), with no
rebuild or restart. On the VPS it runs daily from the systemd units in
`deploy/`:

```bash
sudo cp deploy/polytrade-screener-refresh.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polytrade-screener-refresh.timer
```

The board prints the snapshot's generation date and warns once it is more than
two days old, so a stalled refresh degrades visibly rather than silently.

## Caddy and TLS

The Caddyfile serves `polytradebot.live` and `www.polytradebot.live`, obtains and renews certificates, redirects HTTP, proxies internally, permits Telegram framing, and adds HSTS, `nosniff`, and referrer policy.

Before reload:

```bash
docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

Reload Caddy without recreating the app or engine.

## Database and migrations

Use Postgres/Supabase in production. Apply migrations in order before enabling the engine. Browser roles are denied; access goes through FastAPI.

Before upgrade:

1. back up the database;
2. record the image and Git revision;
3. inspect migrations;
4. pause new buys if compatibility is uncertain;
5. migrate once;
6. start one app/engine and reconcile.

## Monitoring

Monitor more than `/api/health`:

- app container and database health;
- recent engine reconcile activity;
- unresolved/uncertain claims;
- failed orders and alert delivery;
- upstream API/RPC latency;
- disk and logs;
- DNS/TLS;
- exactly one engine.

Logs may contain public wallet metadata but must never contain keys, cookies, Telegram init data, or secrets.

## Upgrade

1. Fetch and review target revision.
2. Run backend tests and frontend build.
3. Back up database and deployment files.
4. Sync committed source only—never `.env`, databases, logs, caches, or `.git` accidentally.
5. Build the image.
6. Disable engine during incompatible migration/handover.
7. Recreate the app once.
8. Verify UI, auth, DB, one engine, and reconciliation.
9. Run a controlled small-value check.

## Rollback

1. Pause new buys or engine autostart.
2. Stop the failed app without starting another engine.
3. Restore previous image/source.
4. Restore DB only when migration compatibility requires it.
5. Start one app in safe mode.
6. Reconcile claims and holdings.
7. Enable one engine only after consistency is restored.

Never roll code back across an incompatible migration while orders are being submitted.

## Wallet Screener hosting

`https://polytradebot.live/screener/` is the **Wallet Screener service**
(`trader-screener/`, its own container in `compose.yaml`). Caddy strips the
`/screener` prefix and proxies to `trader-screener:4310`, so the FastAPI app
never sees those requests. The service sits on its own Docker network and has
no route to the app or its database. There is no React screener entry in the
app bundle and no FastAPI screener route: keep this as the only research UI,
rather than adding a fallback that could shadow the service. A future dedicated
hostname should proxy this same service, not revive a separate frontend build.

`/api/public/screener/*` on the main app is a separate, anonymous, read-only
and rate-limited API. It reads only precomputed `trader_cache` columns, so a
public request can never trigger an upstream Polymarket call or a cache write.
Authenticated `/api/traders/{address}` remains the on-demand route that spends
upstream API budget.

## Production checklist

- [ ] DNS and trusted HTTPS work.
- [ ] SSH/cloud access is restricted.
- [ ] `.env` is owner-only and absent from Git.
- [ ] DB backup/restore was tested.
- [ ] Builder and Telegram credentials work.
- [ ] Gasless flow was tested with a small amount.
- [ ] Base Compose keeps autostart off.
- [ ] Exactly one production engine is enabled.
- [ ] `/screener/` answers from the trader-screener service and its snapshot
      is less than two days old.
- [ ] Engine, claims, disk, logs, DNS, and TLS are monitored.
- [ ] Telegram menu targets production HTTPS.
- [ ] Pause and rotation procedures are documented.
