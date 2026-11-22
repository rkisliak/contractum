#!/usr/bin/env bash
# Starts the local trial / dev Contractum stack (macOS / Linux / WSL / Git Bash); production uses Helm.
#
#   ./run.sh            check prerequisites, then start everything and wait until healthy
#   ./run.sh check      only check prerequisites
#   ./run.sh smoke      log in as admin through the gateway (first run rotates the bootstrap password)
#   ./run.sh e2e [args] run the end-to-end tests in a container (default: tests/e2e)
#   ./run.sh status     show containers
#   ./run.sh logs [svc] follow logs (all services or one)
#   ./run.sh down       stop and remove containers (data is kept)
#   ./run.sh reset      stop and delete ALL data and generated secrets (asks first)
#
# No environment variables are required: the dev-init container generates every secret and the
# TLS certificate on first start. See docs/dev/local-stack.md (production: docs/admin/deploy-kubernetes.md).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PORT=8443
BASE="https://localhost:${PORT}"
MIN_COMPOSE="2.21"
MIN_MEM_GIB=6
WAIT_TIMEOUT="${WAIT_TIMEOUT:-300}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

FAIL=0
fail() { red "  ✗ $*"; FAIL=1; }
ok() { grn "  ✓ $*"; }
warn() { ylw "  ! $*"; }

version_ge() { # a >= b for dotted versions
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

stack_running() {
  [ -n "$(docker compose ps -q --status running traefik 2>/dev/null)" ]
}

port_in_use() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null
  fi
}

check() {
  echo "Checking prerequisites…"
  if ! command -v docker >/dev/null 2>&1; then
    fail "docker not found — install Docker Desktop (or Docker Engine) first"
    return 1
  fi
  ok "docker installed"

  if ! docker info >/dev/null 2>&1; then
    fail "Docker daemon not reachable — start Docker Desktop / the docker service"
    return 1
  fi
  ok "Docker daemon running"

  local cv
  if ! cv=$(docker compose version --short 2>/dev/null); then
    fail "'docker compose' (v2 plugin) not available — update Docker"
  else
    cv=${cv#v}
    if version_ge "$cv" "$MIN_COMPOSE"; then ok "docker compose $cv"; else fail "docker compose $cv is too old (need ≥ $MIN_COMPOSE)"; fi
  fi

  local mem
  mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
  if [ "$mem" -gt 0 ]; then
    local gib=$((mem / 1024 / 1024 / 1024))
    if [ "$gib" -lt "$MIN_MEM_GIB" ]; then
      warn "Docker has ${gib} GiB memory; ${MIN_MEM_GIB}+ GiB recommended (Docker Desktop → Settings → Resources)"
    else
      ok "Docker memory ${gib} GiB"
    fi
  fi

  if stack_running; then
    ok "port ${PORT} held by this stack (already running)"
  elif port_in_use; then
    fail "port ${PORT} is in use by another program — free it before starting"
  else
    ok "port ${PORT} free"
  fi

  command -v curl >/dev/null 2>&1 && ok "curl installed" || warn "curl not found — needed only for '$0 smoke'"
  command -v python3 >/dev/null 2>&1 && ok "python3 installed" || warn "python3 not found — needed only for '$0 smoke'"

  [ "$FAIL" -eq 0 ] && grn "All required prerequisites OK." || { red "Fix the items marked ✗ and run again."; return 1; }
}

up() {
  check
  echo
  echo "Building and starting the stack (first run builds images and can take several minutes)…"
  docker compose up -d --build --wait --wait-timeout "$WAIT_TIMEOUT"
  echo
  if command -v curl >/dev/null 2>&1 && [ -s .dev/ca.crt ]; then
    if curl -sS -o /dev/null -w '%{http_code}' --cacert .dev/ca.crt "$BASE/api/v1/openapi.json" | grep -q '^200$'; then
      ok "gateway answers at $BASE"
    else
      warn "stack started but the gateway did not answer 200 yet — check './run.sh status' / './run.sh logs api-docs'"
    fi
  fi
  # The Web UI is https://localhost:8443/ (the ui container behind the gateway).
  cat <<EOF

Stack is up.
  Web UI:                 $BASE/
  API docs (Swagger UI):  $BASE/api/v1/docs/
  Merged OpenAPI:         $BASE/api/v1/openapi.json
  TLS CA to trust:        .dev/ca.crt          (e.g. curl --cacert .dev/ca.crt …)
  Admin login:            user 'admin'; first-time secret in .dev/bootstrap_admin_secret
                          (must be rotated on first login — './run.sh smoke' does it and
                           saves the new password to .dev/admin_password)
EOF
}

e2e() {
  check
  [ "$#" -eq 0 ] && set -- tests/e2e
  # Created by the caller, so the binds belong to the user the runner runs as (not to root).
  mkdir -p .dev ui/test-results
  docker compose up -d --build --wait --wait-timeout "$WAIT_TIMEOUT"
  # The runner's non-root user and groups (the ./.dev bind, the e2e secrets, the Docker socket).
  source scripts/e2e-runner-env.sh
  exec docker compose -f compose.yaml -f compose.test.yaml --profile test \
    run --rm --build e2e pytest -m e2e -q -o asyncio_default_test_loop_scope=session "$@"
}

cmd="${1:-up}"
[ "$#" -gt 0 ] && shift
case "$cmd" in
  up|start) up ;;
  check) check ;;
  smoke) exec bash scripts/skeleton-smoke.sh ;;
  e2e|test) e2e "$@" ;;
  status|ps) docker compose ps ;;
  logs) docker compose logs -f "$@" ;;
  down|stop) docker compose down --remove-orphans ;;
  reset)
    read -r -p "Delete ALL data, generated secrets and .dev/? [y/N] " a
    if [ "$a" = "y" ] || [ "$a" = "Y" ]; then
      docker compose -f compose.yaml -f compose.test.yaml --profile test down -v --remove-orphans
      rm -rf .dev
      grn "Reset done. Next './run.sh' starts from scratch."
    else
      echo "Cancelled."
    fi
    ;;
  -h|--help|help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) red "unknown command: $cmd"; sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
