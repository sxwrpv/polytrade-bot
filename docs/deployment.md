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

## The screener

`/screener` is served by the dedicated **trader-screener** service, not by the
app container. Caddy proxies `/screener/*` to it (`handle_path` strips the
prefix, so the service's own routes stay `/`, `/api/*` and `/trader/*`).

Its Polycopy cohort snapshot is host state in `./screener-data`, deliberately
not image state: seeded from the image on first start only, refreshed in place
by a daily systemd timer, and re-read by the service when its mtime changes. A
snapshot baked into the image went stale two days after every deploy and could
only be cleared by another deploy.

```bash
sudo systemctl list-timers polytrade-screener-refresh.timer
sudo systemctl status polytrade-screener-refresh.service
```

The board prints its snapshot's generation date and warns once it is more than
two days old, so a stalled timer is visible on the page rather than silent.

`/screener` is not a page in the app bundle and not a Vite entry: the service
has its own container, Dockerfile and snapshot volume.

`/api/public/screener/*` on the app remains anonymous, read-only and rate
limited. It reads only precomputed `trader_cache` columns, so a public request
can never trigger an upstream Polymarket call or a cache write. Authenticated
`/api/traders/{address}` remains the on-demand route that spends upstream API
budget.

An earlier arrangement built a React screener into `frontend/dist` and served
it same-origin from FastAPI, with notes on optionally publishing it at
`screener.polytradebot.live`. Both the page and those notes were removed: it
duplicated the service, and its cohort snapshot was frozen at image-build time,
so it went stale two days after every deploy. If the service ever needs its own
hostname, give the container its own Caddy host block — nothing has to be built
differently for that.


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

## Production checklist

- [ ] DNS and trusted HTTPS work.
- [ ] SSH/cloud access is restricted.
- [ ] `.env` is owner-only and absent from Git.
- [ ] DB backup/restore was tested.
- [ ] Builder and Telegram credentials work.
- [ ] Gasless flow was tested with a small amount.
- [ ] Base Compose keeps autostart off.
- [ ] Exactly one production engine is enabled.
- [ ] The screener is served from one place only: `/screener` proxied to the
      trader-screener service, with no second copy built into the app bundle.
- [ ] The screener snapshot refresh timer is enabled and last ran successfully.
- [ ] Engine, claims, disk, logs, DNS, and TLS are monitored.
- [ ] Telegram menu targets production HTTPS.
- [ ] Pause and rotation procedures are documented.
