#!/bin/sh
# Renders the runtime files nginx serves and includes (D-UI-08-05, D-UI-08-07). Runs from
# /docker-entrypoint.d as the unprivileged user; output goes to ${DC_UI_RUNTIME_DIR:-/tmp/ui}:
#   config.json         public identifiers only (there is no secret in this container)
#   headers.conf        security headers for every page and asset (frame-ancestors 'none', COOP)
#   headers-bridge.conf the same set for /redirect.html: frame-ancestors 'self', no COOP (A-4, A-7)
#
# Fail closed (T-8-12): every value is checked against an allow-list pattern before anything is
# written. On a violation one line names the variable (never its value) on stderr and the script
# exits 1, which aborts the nginx entrypoint and so the container start. A weakened CSP is never
# served. The patterns keep quotes, semicolons, spaces, backslashes, dollar signs and newlines out
# of everything that lands in the CSP or in nginx configuration text.
set -eu

DIR="${DC_UI_RUNTIME_DIR:-/tmp/ui}"
ENTRA_ENABLED="${DC_UI_ENTRA_ENABLED:-false}"
LOCAL_LOGIN="${DC_UI_LOCAL_LOGIN_ENABLED:-true}"
API_BASE="${DC_UI_API_BASE:-/api/v1}"
PROTOCOL="${DC_UI_ENTRA_PROTOCOL:-aad}"
CLIENT_ID="${DC_UI_ENTRA_CLIENT_ID:-}"
AUTHORITY="${DC_UI_ENTRA_AUTHORITY:-}"
SCOPES="${DC_UI_ENTRA_SCOPES:-openid profile offline_access}"

NL='
'

fail() {
  printf 'ui-config: invalid %s\n' "$1" >&2
  exit 1
}

# matches VALUE EXTENDED_REGEX: true when VALUE is one line and matches the whole pattern.
matches() {
  case $1 in *"$NL"*) return 1 ;; esac
  printf '%s\n' "$1" | grep -Eq "$2"
}

case "$ENTRA_ENABLED" in true | false) ;; *) fail DC_UI_ENTRA_ENABLED ;; esac
case "$LOCAL_LOGIN" in true | false) ;; *) fail DC_UI_LOCAL_LOGIN_ENABLED ;; esac
case "$PROTOCOL" in aad | oidc) ;; *) fail DC_UI_ENTRA_PROTOCOL ;; esac
matches "$API_BASE" '^/([A-Za-z0-9_-][A-Za-z0-9/_-]*)?$' || fail DC_UI_API_BASE
matches "$SCOPES" '^[A-Za-z0-9 ._:/-]*$' || fail DC_UI_ENTRA_SCOPES
[ -z "$CLIENT_ID" ] || matches "$CLIENT_ID" '^[A-Za-z0-9._-]{1,128}$' || fail DC_UI_ENTRA_CLIENT_ID
[ -z "$AUTHORITY" ] || matches "$AUTHORITY" \
  '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?$' || fail DC_UI_ENTRA_AUTHORITY

ORIGIN=""
if [ "$ENTRA_ENABLED" = "true" ]; then
  [ -n "$CLIENT_ID" ] || fail DC_UI_ENTRA_CLIENT_ID
  [ -n "$AUTHORITY" ] || fail DC_UI_ENTRA_AUTHORITY
  ORIGIN=$(printf '%s' "$AUTHORITY" | sed -E 's#^(https://[^/]+).*#\1#')
else
  # Entra off: the identifiers are not needed and are not published.
  CLIENT_ID=""
  AUTHORITY=""
fi
EXTRA=""
[ -z "$ORIGIN" ] || EXTRA=" $ORIGIN"

# write_headers FILE FRAME_ANCESTORS WITH_COOP. Every line carries `always` so the headers also
# land on 404 and 405 responses.
write_headers() {
  csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'$EXTRA; frame-src 'self'$EXTRA; worker-src 'self' blob:; base-uri 'none'; form-action 'self'$EXTRA; object-src 'none'; frame-ancestors $2"
  {
    printf 'add_header Content-Security-Policy "%s" always;\n' "$csp"
    printf 'add_header X-Content-Type-Options "nosniff" always;\n'
    printf 'add_header Referrer-Policy "no-referrer" always;\n'
    printf 'add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;\n'
    if [ "$3" = "yes" ]; then
      printf 'add_header Cross-Origin-Opener-Policy "same-origin" always;\n'
    fi
  } > "$1"
}

mkdir -p "$DIR"
printf '{"entraEnabled":%s,"entraClientId":"%s","entraAuthority":"%s","entraScopes":"%s","entraProtocol":"%s","localLoginEnabled":%s,"apiBase":"%s"}\n' \
  "$ENTRA_ENABLED" "$CLIENT_ID" "$AUTHORITY" "$SCOPES" "$PROTOCOL" "$LOCAL_LOGIN" "$API_BASE" \
  > "$DIR/config.json"
write_headers "$DIR/headers.conf" "'none'" yes
write_headers "$DIR/headers-bridge.conf" "'self'" no
