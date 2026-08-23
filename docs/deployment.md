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

The standalone Wallet Screener is a second Vite entry (`frontend/screener.html`),
built into the same `frontend/dist`. It ships **same-origin** by default:
FastAPI serves it at `https://polytradebot.live/screener`, and the browser
talks to `/api/public/screener/*` on the same origin. In that arrangement
nothing about the auth model changes — no CORS entry, no cookie relaxation, no
new certificate.

`/api/public/screener/*` is anonymous, read-only and rate limited. It reads
only precomputed `trader_cache` columns, so a public request can never trigger
an upstream Polymarket call or a cache write. Authenticated
`/api/traders/{address}` remains the on-demand route that spends upstream API budget.

### Optional: screener.polytradebot.live

Only worth doing if you want the screener on its own host. It is not required,
and same-origin is the safer default.

**DNS.** One record, pointed at the same host as the apex:

```
screener.polytradebot.live.  A  52.51.200.58
```

Let it resolve **before** the first Caddy start. Caddy provisions the
certificate over the HTTP-01 challenge on port 80; repeated failures count
against Let's Encrypt rate limits.

**Caddy.** Add a host block to the existing Caddyfile — one Caddy instance, not
a second one:

```
screener.polytradebot.live {
	encode zstd gzip

	header {
		-Server
		X-Content-Type-Options "nosniff"
		Referrer-Policy "strict-origin-when-cross-origin"
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		# Nothing embeds the screener, and it is not a Telegram Mini App.
		Content-Security-Policy "frame-ancestors 'none'"
	}

	root * /srv/screener
	try_files {path} /screener.html
	file_server
}
```

`/srv/screener` holds the built `dist`. Note the apex block already sends
`includeSubDomains`, so this name is inside the existing HSTS policy — it must
be served over HTTPS from the moment that header is first honoured.

**TLS.** Automatic, same as the apex. `includeSubDomains` means a subdomain
that cannot present a valid certificate becomes unreachable rather than falling
back to HTTP, so bring DNS up first.

**CORS and sessions.** Build the screener with

```
VITE_API_BASE=https://polytradebot.live/api
```

and add `https://screener.polytradebot.live` to `CORS_ALLOW_ORIGINS`. The
existing middleware sets `allow_credentials=False`; leave it that way. The
public screener client sends `credentials: 'omit'`, so no cookie crosses
origins and `SameSite` on the real session cookie never has to be weakened.

**Same-origin versus cross-origin.**

| | `polytradebot.live/screener` | `screener.polytradebot.live` |
| --- | --- | --- |
| Auth model | untouched | untouched *only if* credentials stay off |
| CORS | none needed | one origin, no credentials |
| Certificates | existing | one more name |
| Failure blast radius | shared with the app | isolated |
| Cost of a mistake | low | a credentialed CORS entry would expose the session cross-site |

Prefer same-origin unless the isolation is worth that last row.

**Rollback.** Remove the Caddy host block and reload Caddy
(`docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`); the
same-origin `/screener` route is untouched and keeps serving. Drop the origin
from `CORS_ALLOW_ORIGINS` and restart the app. Leave the DNS record in place
until HSTS `max-age` has lapsed for anyone who visited the subdomain, or point
it back at the same host — with `includeSubDomains` active, a name that stops
resolving is a hard failure for those clients, not a silent one.

## Production checklist

- [ ] DNS and trusted HTTPS work.
- [ ] SSH/cloud access is restricted.
- [ ] `.env` is owner-only and absent from Git.
- [ ] DB backup/restore was tested.
- [ ] Builder and Telegram credentials work.
- [ ] Gasless flow was tested with a small amount.
- [ ] Base Compose keeps autostart off.
- [ ] Exactly one production engine is enabled.
- [ ] The screener is served from one place only (same-origin `/screener`,
      or the subdomain — not both pointing at different builds).
- [ ] Engine, claims, disk, logs, DNS, and TLS are monitored.
- [ ] Telegram menu targets production HTTPS.
- [ ] Pause and rotation procedures are documented.
