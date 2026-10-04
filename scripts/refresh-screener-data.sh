#!/usr/bin/env bash
# Refresh the screener cohort snapshot in place, without a rebuild.
#
# Upstream regenerates the cohort daily. Before this existed the snapshot was
# baked into the image, so the board's staleness banner reappeared ~2 days
# after every deploy and the only cure was another deploy. It sat 10 days
# stale on 2026-09-05 telling users to run an ingest nobody was running.
#
# Writes into the mounted ./screener-data directory. The server re-reads on
# mtime change (SNAPSHOT_RELOAD_SECONDS), so no restart is needed.
#
# Install (on the VPS):
#   sudo cp deploy/polytrade-screener-refresh.{service,timer} /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now polytrade-screener-refresh.timer
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/polytrade}"
cd "$DEPLOY_DIR"

COMPOSE=(docker compose)
if ! docker compose version >/dev/null 2>&1; then COMPOSE=(docker-compose); fi

echo "[$(date -u +%FT%TZ)] refreshing screener snapshot"

# Ingest runs in a throwaway container that mounts the same data volume, so
# the host needs no node and the running service is never interrupted.
"${COMPOSE[@]}" run --rm --no-deps trader-screener node scripts/ingest.mjs

# Report what the live service now believes, so a silent no-op is visible.
# Via Caddy's plain-HTTP IP block, NOT 127.0.0.1: no site block matches that
# Host, so the request falls through to the HTTPS redirect and the readback
# silently prints "unknown" (observed on the first real run, 2026-09-05).
BASE_URL="${BASE_URL:-http://52.51.200.58}"
sleep 2
generated="$(curl -fsS --max-time 10 "$BASE_URL/screener/api/health" 2>/dev/null \
  | sed -n 's/.*"generatedAt":"\([^"]*\)".*/\1/p' || true)"
echo "[$(date -u +%FT%TZ)] service reports generatedAt=${generated:-unknown}"
