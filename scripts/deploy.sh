#!/usr/bin/env bash
#
# Deploy an EXPLICIT commit, prove what landed, and roll back if it did not.
#
# Why this exists: on 2026-08-27 the screener commit 026678c reached GitHub at
# 11:25 UTC while the running container had started at 10:02. Production was a
# commit behind, /api/health returned 200 throughout, and nothing in either
# service could report which build was actually answering.
#
# Run ON the VPS, from /opt/polytrade.
#
#   ./scripts/deploy.sh <git-ref>          deploy that ref
#   ./scripts/deploy.sh --check-only       verify what is running now
#
# Every check is a gate. A failure past the point of no return triggers an
# automatic rollback to the image that was running when this started.
set -Eeuo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/polytrade}"
BASE_URL="${BASE_URL:-http://127.0.0.1}"
READY_TIMEOUT="${READY_TIMEOUT:-120}"
COMPOSE=(docker compose)

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
fail() { printf '    \033[31mFAIL\033[0m %s\n' "$*" >&2; }
die()  { fail "$*"; exit 1; }

cd "$DEPLOY_DIR"

# --- what is running right now, so rollback has a target ------------------
PREVIOUS_IMAGE="$("${COMPOSE[@]}" images -q app 2>/dev/null | head -1 || true)"

verify() {
  # $1 = expected revision, or "" to just report
  local expected="${1:-}" failed=0

  log "Verifying the deployment"

  # 1. containers are up and healthy
  local unhealthy
  unhealthy="$(docker ps --filter 'name=polytrade-' \
    --format '{{.Names}} {{.Status}}' | grep -v ' Up ' || true)"
  if [[ -n "$unhealthy" ]]; then
    fail "containers not up: $unhealthy"; failed=1
  else
    ok "containers up"
  fi

  # 2. the API reports the revision we asked for
  local version_json revision data_mode
  version_json="$(curl -fsS --max-time 10 "$BASE_URL/api/version" || echo '{}')"
  revision="$(printf '%s' "$version_json" | sed -n 's/.*"revision":"\([^"]*\)".*/\1/p')"
  if [[ -z "$revision" || "$revision" == "unknown" ]]; then
    fail "/api/version reports no revision — image was not built by this script"
    failed=1
  elif [[ -n "$expected" && "$revision" != "$expected" ]]; then
    fail "/api/version reports $revision, expected $expected"
    failed=1
  else
    ok "api revision $revision"
  fi

  # 3. readiness, not liveness. A 200 from /api/health proves nothing.
  local ready_json ready_status ready_code
  ready_code="$(curl -s -o /tmp/ready.json -w '%{http_code}' --max-time 15 \
    "$BASE_URL/api/ready" || echo 000)"
  ready_json="$(cat /tmp/ready.json 2>/dev/null || echo '{}')"
  ready_status="$(printf '%s' "$ready_json" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')"
  case "$ready_status" in
    healthy)  ok "readiness healthy" ;;
    degraded) ok "readiness degraded (a background loop is stale — check /api/ready)" ;;
    *)        fail "readiness '$ready_status' (http $ready_code)"; failed=1 ;;
  esac

  # 4. the screener answers, with a revision and a live data mode
  local scr_json scr_rev scr_mode
  scr_json="$(curl -fsS --max-time 10 "$BASE_URL/screener/api/health" || echo '{}')"
  scr_rev="$(printf '%s' "$scr_json" | sed -n 's/.*"revision":"\([^"]*\)".*/\1/p')"
  data_mode="$(printf '%s' "$scr_json" | sed -n 's/.*"dataMode":"\([^"]*\)".*/\1/p')"
  if [[ -z "$scr_rev" || "$scr_rev" == "unknown" ]]; then
    fail "screener reports no revision"; failed=1
  elif [[ -n "$expected" && "$scr_rev" != "$expected" ]]; then
    fail "screener reports $scr_rev, expected $expected"; failed=1
  else
    ok "screener revision $scr_rev"
  fi
  if [[ "$data_mode" != "live" ]]; then
    fail "screener data mode is '$data_mode', expected live"; failed=1
  else
    ok "screener serving live data"
  fi

  return "$failed"
}

if [[ "${1:-}" == "--check-only" ]]; then
  verify "" && { log "All checks passed"; exit 0; } || die "checks failed"
fi

REF="${1:?usage: deploy.sh <git-ref> | --check-only}"

# --- fetch and pin --------------------------------------------------------
log "Resolving $REF"
git fetch --all --tags --prune
REVISION="$(git rev-parse --verify "${REF}^{commit}")"
SHORT="${REVISION:0:7}"
ok "$REF -> $REVISION"

log "Checking out $SHORT"
git checkout --detach "$REVISION"

# --- pre-flight: everything that can fail BEFORE anything changes ---------
log "Pre-flight"

# The Caddyfile is validated against the real Caddy image, in a throwaway
# container, before it can take the site down. A bad matcher here is a 100%
# outage, and `caddy reload` reports success on a stale inode (see
# docs/deployment.md), so this must happen before the container is recreated.
docker run --rm --network none \
  -v "$DEPLOY_DIR/Caddyfile:/tmp/Caddyfile:ro" \
  caddy:2.10.2-alpine caddy validate --config /tmp/Caddyfile --adapter caddyfile \
  >/dev/null 2>&1 && ok "Caddyfile valid" || die "Caddyfile is invalid — nothing changed"

"${COMPOSE[@]}" config >/dev/null && ok "compose config valid" || die "compose config invalid"

# The engine flag lives only in the VPS .env. A redeploy that loses it leaves
# /api/health green while nothing trades — exactly the failure this whole
# script exists to catch.
if ! grep -q '^COPY_ENGINE_AUTOSTART=1' .env 2>/dev/null; then
  fail "COPY_ENGINE_AUTOSTART=1 is not set in .env — the copy engine will NOT start"
  die "refusing to deploy a silently disabled engine"
fi
ok "engine autostart configured"

# --- build ----------------------------------------------------------------
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "Building $SHORT"
"${COMPOSE[@]}" build \
  --build-arg "GIT_REVISION=$REVISION" \
  --build-arg "BUILD_TIME=$BUILD_TIME"
ok "built"

log "Running tests inside the image"
"${COMPOSE[@]}" run --rm --no-deps app python -m pytest -q \
  && ok "tests passed" || die "tests failed — nothing deployed"

# --- point of no return ---------------------------------------------------
log "Recreating containers"
"${COMPOSE[@]}" up -d --force-recreate

log "Waiting for readiness (${READY_TIMEOUT}s)"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
until curl -fsS --max-time 5 "$BASE_URL/api/health" >/dev/null 2>&1; do
  if (( $(date +%s) > deadline )); then break; fi
  sleep 3
done

if verify "$REVISION"; then
  log "Deployed $SHORT"
  printf '    revision   %s\n    built      %s\n' "$REVISION" "$BUILD_TIME"
  exit 0
fi

# --- rollback -------------------------------------------------------------
log "Verification failed — rolling back"
if [[ -z "$PREVIOUS_IMAGE" ]]; then
  die "no previous image recorded; NOT rolling back automatically. Investigate now."
fi
git checkout --detach - 2>/dev/null || true
docker tag "$PREVIOUS_IMAGE" polytrade:local
"${COMPOSE[@]}" up -d --force-recreate app
sleep 10
if verify ""; then
  die "rolled back to the previous image; the new build did not pass verification"
fi
die "ROLLBACK ALSO FAILED VERIFICATION — manual intervention required now"
