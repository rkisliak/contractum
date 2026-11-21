#!/usr/bin/env bash
# Walking-skeleton smoke test against a running compose stack (warm or cold).
#
# Logs in as the bootstrap admin through the gateway (https://localhost:8443). If a previous
# rotated password in .dev/admin_password no longer works (e.g. the stack was torn down together
# with its volumes) the bootstrap flow runs again: secret login -> rotate -> write the new password
# -> login. Then it waits until the audited rotation event shows up exactly once in the audit
# database. Prints only "skeleton OK" and the count, never a credential.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

BASE="https://localhost:8443"
CA=".dev/ca.crt"
PW_FILE=".dev/admin_password"
SECRET_FILE=".dev/bootstrap_admin_secret"
LOGIN="$BASE/api/v1/identity/auth/login"
ROTATE="$BASE/api/v1/identity/auth/password"

[ -s "$CA" ] || { echo "missing $CA: run 'docker compose up' first" >&2; exit 1; }

json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

post_json() { # url bearer(optional) body
  local url=$1 bearer=$2 body=$3
  if [ -n "$bearer" ]; then
    curl -sS --fail-with-body --cacert "$CA" -X POST "$url" \
      -H 'Content-Type: application/json' -H "Authorization: Bearer $bearer" --data "$body"
  else
    curl -sS --fail-with-body --cacert "$CA" -X POST "$url" \
      -H 'Content-Type: application/json' --data "$body"
  fi
}

body_for() { # builds a JSON body without putting the value on a command line
  python3 -c 'import json,sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))' "$@"
}

login_status() { # password -> http status of a login attempt
  local body
  body=$(body_for username admin password "$1")
  curl -sS -o /dev/null -w '%{http_code}' --cacert "$CA" -X POST "$LOGIN" \
    -H 'Content-Type: application/json' --data "$body"
}

need_bootstrap=1
if [ -s "$PW_FILE" ]; then
  if [ "$(login_status "$(cat "$PW_FILE")")" = "200" ]; then
    need_bootstrap=0
  else
    rm -f "$PW_FILE"
  fi
fi

if [ "$need_bootstrap" = "1" ]; then
  [ -s "$SECRET_FILE" ] || { echo "missing $SECRET_FILE" >&2; exit 1; }
  restricted=$(post_json "$LOGIN" "" "$(body_for username admin password "$(cat "$SECRET_FILE")")" | json_field access_token)
  new_password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  post_json "$ROTATE" "$restricted" "$(body_for new_password "$new_password")" > /dev/null
  umask 077
  printf '%s' "$new_password" > "$PW_FILE"
  chmod 600 "$PW_FILE"
fi

token=$(post_json "$LOGIN" "" "$(body_for username admin password "$(cat "$PW_FILE")")" | json_field access_token)
[ -n "$token" ] || { echo "login returned no token" >&2; exit 1; }

count=0
for _ in $(seq 1 30); do
  count=$(docker compose exec -T postgres psql -U postgres -d audit -tAc \
    "select count(*) from audit_event where action = 'user.password_rotated'" | tr -d '[:space:]')
  [ "$count" = "1" ] && break
  sleep 1
done
if [ "$count" != "1" ]; then
  echo "audit database holds $count user.password_rotated events, expected 1" >&2
  exit 1
fi

# D-18: a path no router serves (internal routes are never routed) is answered by the catch-all
# with a 404 problem+json from the problem responder, not by Traefik's plain-text 404.
probe=$(curl -sS -o /dev/null -w '%{http_code} %{content_type}' --cacert "$CA" "$BASE/internal/v1/events")
if [ "$probe" != "404 application/problem+json" ]; then
  echo "GET /internal/v1/events through the gateway returned '$probe', expected '404 application/problem+json'" >&2
  exit 1
fi

echo "skeleton OK (user.password_rotated events: $count)"
