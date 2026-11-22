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
# problem and the other one reaches the service. The credential routes under
# /api/v1/identity/auth/ sit behind the stricter rl-auth (5 per second, burst 10), so the API scan
# runs in two passes over a split copy of the merged OpenAPI document: every other route at
# ZAP_REQUESTS_PER_SECOND (40), then the credential routes at ZAP_AUTH_REQUESTS_PER_SECOND (4).
# Keep both below their limiter's average.
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
# ZAP starts with -silent: no update check and no add-on download from GitHub at start (a start-up
# download hung one run for the full 600 s connect wait), so every run uses exactly the add-ons
# below. Without -silent the scan scripts update the image's add-ons and install the beta passive
# rules at every start; these are the same releases that start-up installed in the last runs,
# pinned by SHA-256, downloaded and verified once before any scan and copied into ZAP's home plugin
# directory before ZAP starts (the same rule set as the start-up install, checked against the
# pinned image). To upgrade: pick the release in
# github.com/zaproxy/zap-extensions/releases, copy the .zap asset's digest, review the new alerts.
ZAP_ADDONS=(
  "pscanrules-v79/pscanrules-release-79.zap 824545a0c66bb0525f614153e6aee80ed10ae6a133d1624deddf85c219f6ba2d"
  "pscanrulesBeta-v50/pscanrulesBeta-beta-50.zap a979f7cd5d8be10e0338b417e006ec1e2f6ec79101010d570ddb5199892e921b"
  "retire-v0.69.0/retire-release-0.69.0.zap 11510b96a24fe2e18251c8166d26148c19c7853bf11353c684e02a996ca5129c"
)
NETWORK="${ZAP_NETWORK:-contractum_edge}"
TARGET="https://traefik"
LOGIN="https://localhost:8443/api/v1/identity/auth/login"
CA=".dev/ca.crt"
PW_FILE=".dev/admin_password"
# The active scan stops after this many minutes, inside the 15 minute (900 s) access token.
ACTIVE_SCAN_MINUTES="${ZAP_ACTIVE_SCAN_MINUTES:-12}"
# Requests per second ZAP sends to the gateway, below rl-default's average of 50 (see the header).
REQUESTS_PER_SECOND="${ZAP_REQUESTS_PER_SECOND:-40}"
# The credential routes have their own, stricter limiter (rl-auth: 5 per second, burst 10), so
# they are scanned in a separate pass at this rate.
AUTH_PREFIX="/api/v1/identity/auth/"
AUTH_REQUESTS_PER_SECOND="${ZAP_AUTH_REQUESTS_PER_SECOND:-4}"

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

# Next to the reports, so the path is one Docker can mount on every host (Docker Desktop does not
# share the system temp directory); removed again on exit, so the artifact never carries it.
ADDONS_DIR="$OUTDIR_ABS/.zap-addons"
rm -rf "$ADDONS_DIR"
mkdir -p "$ADDONS_DIR"
trap 'rm -rf "$ADDONS_DIR"' EXIT
for addon in "${ZAP_ADDONS[@]}"; do
  read -r release_path sha256 <<< "$addon"
  file="${release_path##*/}"
  curl -sS --fail --location -o "$ADDONS_DIR/$file" \
    "https://github.com/zaproxy/zap-extensions/releases/download/$release_path"
  python3 - "$ADDONS_DIR/$file" "$sha256" <<'PY'
import hashlib, sys
path, want = sys.argv[1:]
got = hashlib.sha256(open(path, "rb").read()).hexdigest()
sys.exit(0 if got == want else f"{path}: sha256 {got}, expected {want}")
PY
  chmod 644 "$ADDONS_DIR/$file"
done

password=$(cat "$PW_FILE")
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  echo "::add-mask::$password"
fi

# Logs in as the admin and exports the bearer header ZAP sends (masked in GitHub Actions). Each API
# pass mints its own token, so each pass has the full 15 minutes.
login_for_zap() {
  local password body token
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
}

# ZAP's start options: -silent, and the network rate limit rule (network add-on) for the traefik
# host: rate_limit REQS_PER_SECOND.
rate_limit() {
  local rule="-config network.ratelimit.rules.rule(0)"
  echo "-silent $rule.description=gateway $rule.enabled=true $rule.matchStr=traefik" \
    "$rule.regex=false $rule.reqsPerSec=$1"
}

run_zap() { # script args... ; returns 0 on a completed scan, 1 otherwise
  local rc=0
  # The add-ons go where ZAP's own start-up install puts them (its home plugin directory), then
  # the scan script runs with the arguments unchanged.
  docker run --rm --network "$NETWORK" -v "$OUTDIR_ABS:/zap/wrk:rw" -v "$ADDONS_DIR:/addons:ro" \
    -e ZAP_AUTH_HEADER -e ZAP_AUTH_HEADER_VALUE -e ZAP_AUTH_HEADER_SITE \
    --entrypoint sh "$ZAP_IMAGE" \
    -c 'mkdir -p "$HOME/.ZAP/plugin" && cp /addons/*.zap "$HOME/.ZAP/plugin/" && exec "$@"' \
    zap "$@" || rc=$?
  case "$rc" in
    0 | 2) return 0 ;;
    *) echo "ZAP ($1) failed with exit code $rc" >&2; return 1 ;;
  esac
}

login_for_zap

echo "== ZAP baseline (passive) of the UI =="
run_zap zap-baseline.py -t "$TARGET/" -j -c rules.tsv \
  -J zap-baseline.json -r zap-baseline.html -I \
  -z "$(rate_limit "$REQUESTS_PER_SECOND")"

# The merged OpenAPI document, split by gateway limit: the credential routes under
# /api/v1/identity/auth/ (rl-auth) and everything else (rl-default). Both copies name the gateway
# as their server, so ZAP sends every request to https://traefik.
curl -sS --fail-with-body --cacert "$CA" "https://localhost:8443/api/v1/openapi.json" \
  -o "$OUTDIR/openapi.json"
python3 - "$OUTDIR/openapi.json" "$OUTDIR" "$AUTH_PREFIX" "$TARGET" <<'PY'
import json, sys
src, outdir, prefix, target = sys.argv[1:]
doc = json.load(open(src))
for name, keep in (("openapi-api.json", False), ("openapi-auth.json", True)):
    part = dict(doc, servers=[{"url": target}])
    part["paths"] = {p: v for p, v in doc["paths"].items() if p.startswith(prefix) == keep}
    if not part["paths"]:
        sys.exit(f"{name}: no paths (prefix {prefix})")
    json.dump(part, open(f"{outdir}/{name}", "w"))
    print(f"{name}: {len(part['paths'])} paths")
PY
chmod 644 "$OUTDIR/openapi.json" "$OUTDIR/openapi-api.json" "$OUTDIR/openapi-auth.json"

echo "== ZAP API scan of every route outside $AUTH_PREFIX (authenticated) =="
run_zap zap-api-scan.py -t /zap/wrk/openapi-api.json -f openapi -c rules.tsv \
  -J zap-api.json -r zap-api.html -I \
  -z "$(rate_limit "$REQUESTS_PER_SECOND") -config scanner.maxScanDurationInMins=${ACTIVE_SCAN_MINUTES} -config scanner.threadPerHost=2"

login_for_zap
echo "== ZAP API scan of the credential routes under $AUTH_PREFIX (authenticated) =="
run_zap zap-api-scan.py -t /zap/wrk/openapi-auth.json -f openapi -c rules.tsv \
  -J zap-api-auth.json -r zap-api-auth.html -I \
  -z "$(rate_limit "$AUTH_REQUESTS_PER_SECOND") -config scanner.maxScanDurationInMins=${ACTIVE_SCAN_MINUTES} -config scanner.threadPerHost=2"
unset ZAP_AUTH_HEADER_VALUE

echo "== Gate: no unignored High alert =="
python3 scripts/zap_gate.py "$OUTDIR/zap-baseline.json" "$OUTDIR/zap-api.json" \
  "$OUTDIR/zap-api-auth.json" --rules .zap/rules.tsv
