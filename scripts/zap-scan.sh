#!/usr/bin/env bash
# OWASP ZAP scans of the compose TEST stack, shared by CI (dast-nightly.yml) and local runs.
#
#   bash scripts/zap-scan.sh OUTDIR
#
# Needs the compose stack up (docker compose up -d --wait, then scripts/skeleton-smoke.sh so that
# .dev/admin_password is valid). It logs in as the bootstrap admin through the gateway on the
# host (https://localhost:8443, verified with the dev CA), then runs the official ZAP image on the
# compose `edge` network, where https://traefik/ serves both the UI and the API:
#
#   (a) zap-baseline.py: passive scan of the UI (spider only, no attack traffic)
#   (b) zap-api-scan.py: active scan of every operation of the merged OpenAPI document, sent with
#       the admin bearer token
#
# Never run this against the k8s deployment: the network below only exists for the compose stack.
# The admin password and the access token are never echoed and never appear on a command line:
# the token reaches ZAP through the environment (`-e NAME`, value not on the docker argv), and
# ZAP_AUTH_HEADER_SITE keeps the header off every host except the gateway. In GitHub Actions both
# values are masked in the log. The access token lives 900 s, so the active scan is capped below
# that.
#
# Both scans are paced below the gateway's API rate limit (rl-default in
# gateway/dynamic/middlewares.yml: 50 requests per second per client, burst 100) with ZAP's own
# rate limit rule for the `traefik` host. An unpaced active scan gets 429 for a large part of its
# probes: those never reach a service, so the scan does not test them, and the boolean-based SQL
# injection rule (40018) reports a finding whenever one probe of a pair gets the constant 429
# problem and the other one reaches the service. Keep ZAP_REQUESTS_PER_SECOND below the gateway's
# average.
#
# Reports (HTML for people, JSON for scripts/zap_gate.py) land in OUTDIR. ZAP exits 0 (clean) or 2
# (warnings, because of -I) when it completed; anything else is a failure of the scan itself. The
# gate then fails on any High alert that .zap/rules.tsv does not ignore (with a reason).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# The single pin: the multi-arch index digest of ghcr.io/zaproxy/zaproxy:stable (ZAP 2.17.0).
# To upgrade: `docker buildx imagetools inspect ghcr.io/zaproxy/zaproxy:stable`, copy the top-level
# Digest, re-run this script locally and review the new alerts.
ZAP_IMAGE="ghcr.io/zaproxy/zaproxy:stable@sha256:9624669db72f54d86c4b46a7064518bd9bf79d8c61a4c31befa9923489f328af"
NETWORK="${ZAP_NETWORK:-contractum_edge}"
TARGET="https://traefik"
LOGIN="https://localhost:8443/api/v1/identity/auth/login"
CA=".dev/ca.crt"
PW_FILE=".dev/admin_password"
# The active scan stops after this many minutes, inside the 15 minute (900 s) access token.
ACTIVE_SCAN_MINUTES="${ZAP_ACTIVE_SCAN_MINUTES:-12}"
# Requests per second ZAP sends to the gateway, below rl-default's average of 50 (see the header).
REQUESTS_PER_SECOND="${ZAP_REQUESTS_PER_SECOND:-40}"

OUTDIR="${1:?usage: scripts/zap-scan.sh OUTDIR}"

[ -s "$CA" ] || { echo "missing $CA: start the compose stack first" >&2; exit 1; }
[ -s "$PW_FILE" ] || { echo "missing $PW_FILE: run scripts/skeleton-smoke.sh first" >&2; exit 1; }
[ -s .zap/rules.tsv ] || { echo "missing .zap/rules.tsv" >&2; exit 1; }
docker network inspect "$NETWORK" > /dev/null 2>&1 || {
  echo "docker network $NETWORK not found: the compose TEST stack is not up" >&2
  exit 1
}

mkdir -p "$OUTDIR"
# The ZAP container runs as uid 1000 and must write its reports here.
chmod 777 "$OUTDIR"
cp .zap/rules.tsv "$OUTDIR/rules.tsv"
chmod 644 "$OUTDIR/rules.tsv"
OUTDIR_ABS=$(cd "$OUTDIR" && pwd)

password=$(cat "$PW_FILE")
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  echo "::add-mask::$password"
fi

# Same body shape as scripts/skeleton-smoke.sh; the password never touches a command line.
# The here-string appends a newline that is not part of the password: strip it.
body=$(python3 -c 'import json,sys; print(json.dumps({"username": "admin", "password": sys.stdin.read().rstrip("\n")}))' <<< "$password")
unset password
token=$(curl -sS --fail-with-body --cacert "$CA" -X POST "$LOGIN" \
  -H 'Content-Type: application/json' --data "$body" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
unset body
[ -n "$token" ] || { echo "login returned no token" >&2; exit 1; }
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  echo "::add-mask::$token"
  echo "::add-mask::Bearer $token"
fi

export ZAP_AUTH_HEADER="Authorization"
export ZAP_AUTH_HEADER_VALUE="Bearer $token"
export ZAP_AUTH_HEADER_SITE="traefik"
unset token

# ZAP's network rate limit rule (network add-on), passed to both scans with -z.
RATE_LIMIT="-config network.ratelimit.rules.rule(0).description=gateway"
RATE_LIMIT+=" -config network.ratelimit.rules.rule(0).enabled=true"
RATE_LIMIT+=" -config network.ratelimit.rules.rule(0).matchStr=traefik"
RATE_LIMIT+=" -config network.ratelimit.rules.rule(0).regex=false"
RATE_LIMIT+=" -config network.ratelimit.rules.rule(0).reqsPerSec=${REQUESTS_PER_SECOND}"

run_zap() { # script args... ; returns 0 on a completed scan, 1 otherwise
  local rc=0
  docker run --rm --network "$NETWORK" -v "$OUTDIR_ABS:/zap/wrk:rw" \
    -e ZAP_AUTH_HEADER -e ZAP_AUTH_HEADER_VALUE -e ZAP_AUTH_HEADER_SITE \
    "$ZAP_IMAGE" "$@" || rc=$?
  case "$rc" in
    0 | 2) return 0 ;;
    *) echo "ZAP ($1) failed with exit code $rc" >&2; return 1 ;;
  esac
}

echo "== ZAP baseline (passive) of the UI =="
run_zap zap-baseline.py -t "$TARGET/" -j -c rules.tsv \
  -J zap-baseline.json -r zap-baseline.html -I \
  -z "$RATE_LIMIT"

echo "== ZAP API scan of the merged OpenAPI document (authenticated) =="
run_zap zap-api-scan.py -t "$TARGET/api/v1/openapi.json" -f openapi -c rules.tsv \
  -J zap-api.json -r zap-api.html -I \
  -z "$RATE_LIMIT -config scanner.maxScanDurationInMins=${ACTIVE_SCAN_MINUTES} -config scanner.threadPerHost=2"

echo "== Gate: no unignored High alert =="
python3 scripts/zap_gate.py "$OUTDIR/zap-baseline.json" "$OUTDIR/zap-api.json" --rules .zap/rules.tsv
