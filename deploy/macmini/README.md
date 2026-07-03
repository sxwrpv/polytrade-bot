# Hosting on an always-on Mac mini

One-time setup that runs the bot 24/7 with auto-restart on crash and on
reboot, plus a public HTTPS URL for the Telegram Mini App.

## Why your other bot never needs URL changes (and this one does)

A plain Telegram bot only *polls* Telegram's servers — outbound traffic, no
public URL at all. A **Mini App** serves a web page inside Telegram, so
Telegram's servers must be able to reach it over HTTPS. That needs either a
tunnel or a host with a public address. The fix for "URL keeps changing" is a
tunnel with a **static hostname** (set it in BotFather once, never again):

- **ngrok with a free static domain (recommended)** — create a free account
  at ngrok.com, grab your authtoken, and claim your one free static domain
  (something like `polytrade.ngrok-free.app`). Permanent URL, survives
  restarts and network blips.
- **localhost.run fallback (no signup)** — rotating URLs, but the supervisor
  auto-repoints the bot's menu button on every rotation. Works, but share
  links (`t.me/...?startapp=`) go stale on each rotation.

## Setup

```bash
git clone <this repo> ~/polytrade && cd ~/polytrade
cp .env.example .env
# fill in .env: ENCRYPTION_SECRET (long random string), the POLYMARKET_BUILDER_*
# credentials, TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME
./deploy/macmini/setup.sh
```

With ngrok (recommended), export these before running setup:

```bash
export NGROK_AUTHTOKEN=...      # from dashboard.ngrok.com
export NGROK_DOMAIN=your-name.ngrok-free.app
./deploy/macmini/setup.sh
```

Then in @BotFather → your bot → Configure Mini App: set the URL to
`https://$NGROK_DOMAIN` — once, forever. The setup script also points the
bot's menu button there automatically.

## What setup.sh installs

- `uv` + Python 3.12 virtualenv + backend dependencies
- Node (user-space) + frontend build
- Two launchd agents (auto-start at boot, auto-restart on crash):
  - `com.polytrade.server` — the FastAPI app + copy engine on port 8123
  - `com.polytrade.tunnel` — ngrok (static URL) or the localhost.run
    supervisor (rotating URL + automatic menu-button repointing)

## Operating

```bash
launchctl list | grep polytrade                  # status
tail -f ~/polytrade/logs/server.log              # app + engine logs
tail -f ~/polytrade/logs/tunnel.log              # tunnel logs
launchctl kickstart -k gui/$(id -u)/com.polytrade.server   # restart app
```

Updating the code: `cd ~/polytrade && git pull && ./deploy/macmini/setup.sh`
(re-runs builds and reloads the agents; the SQLite DB and .env are untouched).

Keep the mini awake: System Settings → Energy → "Prevent automatic sleeping".
The DB (`copybot.db`) holds users' encrypted keys — treat the machine and its
backups accordingly.
