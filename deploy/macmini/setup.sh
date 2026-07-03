#!/bin/bash
# One-shot Mac mini setup: deps, frontend build, launchd agents for the app
# server and the HTTPS tunnel. Idempotent — re-run after every git pull.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${PORT:-8123}"
LOGS="$REPO/logs"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$LOGS" "$AGENTS"

echo "== repo: $REPO"
[ -f "$REPO/.env" ] || { echo "ERROR: create $REPO/.env first (cp .env.example .env)"; exit 1; }

# --- python -----------------------------------------------------------------
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  echo "== installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
echo "== python venv + deps"
cd "$REPO"
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --prerelease allow -q -r requirements.txt

# --- node + frontend build ---------------------------------------------------
if ! command -v node >/dev/null && [ ! -x "$HOME/.local/node/bin/node" ]; then
  echo "== installing node (user-space)"
  NODE_V="v22.14.0"
  ARCH=$(uname -m | sed 's/x86_64/x64/;s/arm64/arm64/')
  curl -fsSL "https://nodejs.org/dist/$NODE_V/node-$NODE_V-darwin-$ARCH.tar.gz" -o /tmp/node.tgz
  mkdir -p "$HOME/.local/node"
  tar -xzf /tmp/node.tgz -C "$HOME/.local/node" --strip-components=1
fi
export PATH="$HOME/.local/node/bin:$PATH"
echo "== frontend build"
cd "$REPO/frontend" && npm install --silent && npm run build

# --- tunnel choice -----------------------------------------------------------
TUNNEL_CMD=""
if [ -n "${NGROK_AUTHTOKEN:-}" ] && [ -n "${NGROK_DOMAIN:-}" ]; then
  if ! command -v ngrok >/dev/null && [ ! -x "$HOME/.local/bin/ngrok" ]; then
    echo "== installing ngrok"
    ARCH=$(uname -m | sed 's/x86_64/amd64/')
    curl -fsSL "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-darwin-$ARCH.zip" -o /tmp/ngrok.zip
    unzip -oq /tmp/ngrok.zip -d "$HOME/.local/bin"
  fi
  "$HOME/.local/bin/ngrok" config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null
  TUNNEL_CMD="$HOME/.local/bin/ngrok http --domain=$NGROK_DOMAIN $PORT --log=stdout"
  PUBLIC_URL="https://$NGROK_DOMAIN"
  echo "== tunnel: ngrok static domain -> $PUBLIC_URL"
else
  TUNNEL_CMD="$REPO/deploy/macmini/lhr_supervisor.sh $REPO $PORT"
  PUBLIC_URL=""
  echo "== tunnel: localhost.run supervisor (rotating URL, auto menu repoint)"
  echo "   TIP: for a permanent URL, re-run with NGROK_AUTHTOKEN + NGROK_DOMAIN set"
fi

# --- launchd agents ----------------------------------------------------------
write_plist() {  # $1 label, $2 program+args (space separated), $3 logfile
  local label="$1" args="$2" logfile="$3" xmlargs=""
  for a in $args; do xmlargs="$xmlargs<string>$a</string>"; done
  cat > "$AGENTS/$label.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$xmlargs</array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONPATH</key><string>$REPO</string>
    <key>PORT</key><string>$PORT</string>
    <key>PATH</key><string>$HOME/.local/bin:$HOME/.local/node/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$logfile</string>
  <key>StandardErrorPath</key><string>$logfile</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
}

echo "== installing launchd agents"
write_plist com.polytrade.server \
  "$REPO/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port $PORT --log-level warning" \
  "$LOGS/server.log"
write_plist com.polytrade.tunnel "$TUNNEL_CMD" "$LOGS/tunnel.log"

# --- verify + point the bot at the public URL --------------------------------
echo "== waiting for the app"
for i in $(seq 1 30); do
  curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/api/health" && break; sleep 2
done
curl -s "http://127.0.0.1:$PORT/api/health" && echo " <- app healthy"

if [ -n "$PUBLIC_URL" ]; then
  BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$REPO/.env" | cut -d= -f2-)
  if [ -n "$BOT_TOKEN" ]; then
    echo "== pointing bot menu button at $PUBLIC_URL"
    curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setChatMenuButton" \
      -H 'Content-Type: application/json' \
      -d "{\"menu_button\":{\"type\":\"web_app\",\"text\":\"Open app\",\"web_app\":{\"url\":\"$PUBLIC_URL\"}}}"
    echo
    echo "== also set this URL in @BotFather -> Configure Mini App (once): $PUBLIC_URL"
  fi
fi
echo "== done. logs: $LOGS/"
