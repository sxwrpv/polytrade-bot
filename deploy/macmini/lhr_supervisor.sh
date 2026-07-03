#!/bin/bash
# Fallback tunnel: keeps a localhost.run HTTPS tunnel alive forever and
# repoints the Telegram bot's menu button whenever the (rotating) URL changes.
# Prefer ngrok with a static domain (see setup.sh) — this exists so the bot
# still self-heals with zero accounts configured.
REPO="${1:?usage: lhr_supervisor.sh <repo> <port>}"
PORT="${2:?usage: lhr_supervisor.sh <repo> <port>}"
BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$REPO/.env" | cut -d= -f2-)

while true; do
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes -o ConnectTimeout=15 \
      -R "80:127.0.0.1:$PORT" nokey@localhost.run 2>&1 | while read -r line; do
    u=$(echo "$line" | grep -o "https://[a-z0-9]*\.lhr\.life" | head -1)
    if [ -n "$u" ] && [ "$u" != "${last:-}" ]; then
      last="$u"
      echo "$(date '+%F %H:%M:%S') NEW URL: $u"
      if [ -n "$BOT_TOKEN" ]; then
        curl -s -m 15 -X POST "https://api.telegram.org/bot$BOT_TOKEN/setChatMenuButton" \
          -H 'Content-Type: application/json' \
          -d "{\"menu_button\":{\"type\":\"web_app\",\"text\":\"Open app\",\"web_app\":{\"url\":\"$u\"}}}" >/dev/null \
          && echo "$(date '+%F %H:%M:%S') menu button repointed"
      fi
    fi
  done
  echo "$(date '+%F %H:%M:%S') tunnel dropped — reconnecting in 5s"
  sleep 5
done
