#!/bin/bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$REPO/deploy/macmini/ngrok.env"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE. Copy ngrok.env.example and fill NGROK_AUTHTOKEN + NGROK_DOMAIN."; exit 2; }
set -a
source "$ENV_FILE"
set +a
: "${NGROK_AUTHTOKEN:?NGROK_AUTHTOKEN required}"
: "${NGROK_DOMAIN:?NGROK_DOMAIN required}"
PORT="${PORT:-8123}"
ngrok config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null
exec ngrok http --domain="$NGROK_DOMAIN" "$PORT" --log=stdout
