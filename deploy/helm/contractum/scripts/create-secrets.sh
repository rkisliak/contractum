#!/usr/bin/env bash
# Create the Kubernetes Secrets the contractum chart expects, with fresh random values.
#
#   NAMESPACE=contractum KUBE_CONTEXT=my-cluster bash scripts/create-secrets.sh
#
# Values come from the same generator docker compose uses (infra/dev-init/generate.py), run in the
# identity image, so the files and their names match what each service reads from /run/secrets.
# A Secret that already exists is left untouched: regenerating database or NATS passwords would
# lock the services out of a database that already holds the old ones. The generated files live
# only in a temporary Docker volume that is removed at the end.
#
# Optional environment:
#   IMAGE        identity image to run the generator (default contractum/identity:dev)
#   TLS_CERT, TLS_KEY   your certificate and key for the gateway (PEM files). Without them the
#                       gateway gets a self-signed development certificate for localhost only.
#   SMTP_PASSWORD       creates the optional mail Secret (key smtp_password)
#   SKIP_BUNDLED=1      do not create the bundled Postgres and NATS Secrets (external servers)
set -euo pipefail

NAMESPACE="${NAMESPACE:-contractum}"
IMAGE="${IMAGE:-contractum/identity:dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
KUBECTL=(kubectl)
[ -n "${KUBE_CONTEXT:-}" ] && KUBECTL+=(--context "$KUBE_CONTEXT")
KUBECTL+=(--namespace "$NAMESPACE")

echo "Target: context=$(kubectl config current-context 2>/dev/null)${KUBE_CONTEXT:+ (override: $KUBE_CONTEXT)} namespace=$NAMESPACE"

"${KUBECTL[@]}" get namespace "$NAMESPACE" >/dev/null 2>&1 \
  || kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} create namespace "$NAMESPACE"

VOLUME="dc-secrets-$$"
WORK="$(mktemp -d)"
cleanup() {
  docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

docker volume create "$VOLUME" >/dev/null
docker run --rm --user 0:0 \
  -v "$VOLUME":/out \
  -v "$REPO_ROOT/infra":/opt/infra:ro \
  -e DEV_INIT_OUT_HOST=/nonexistent \
  "$IMAGE" python /opt/infra/dev-init/generate.py >/dev/null
docker run --rm --user 0:0 -v "$VOLUME":/out "$IMAGE" tar -C /out -cf - . | tar -xf - -C "$WORK"
chmod -R u+rwX "$WORK"

exists() { "${KUBECTL[@]}" get secret "$1" >/dev/null 2>&1; }

create_from_dir() {
  local name="$1" dir="$2"
  if exists "$name"; then echo "kept    secret/$name (already exists)"; return; fi
  "${KUBECTL[@]}" create secret generic "$name" --from-file="$WORK/$dir" >/dev/null
  echo "created secret/$name"
}

create_from_dir dc-identity identity
create_from_dir dc-contracts contracts
create_from_dir dc-validation validation
create_from_dir dc-audit audit
if [ "${SKIP_BUNDLED:-0}" != "1" ]; then
  create_from_dir dc-postgres postgres
  create_from_dir dc-nats nats
fi

if exists dc-gateway-tls; then
  echo "kept    secret/dc-gateway-tls (already exists)"
elif [ -n "${TLS_CERT:-}" ] && [ -n "${TLS_KEY:-}" ]; then
  "${KUBECTL[@]}" create secret tls dc-gateway-tls --cert="$TLS_CERT" --key="$TLS_KEY" >/dev/null
  echo "created secret/dc-gateway-tls (your certificate)"
else
  "${KUBECTL[@]}" create secret tls dc-gateway-tls \
    --cert="$WORK/traefik/tls.crt" --key="$WORK/traefik/tls.key" >/dev/null
  echo "created secret/dc-gateway-tls (self-signed development certificate for localhost)"
fi

if [ -n "${SMTP_PASSWORD:-}" ] && ! exists dc-mail; then
  "${KUBECTL[@]}" create secret generic dc-mail --from-literal=smtp_password="$SMTP_PASSWORD" >/dev/null
  echo "created secret/dc-mail"
fi

echo "Done. The admin's one-time bootstrap secret:"
echo "  kubectl ${KUBE_CONTEXT:+--context $KUBE_CONTEXT }-n $NAMESPACE get secret dc-identity -o jsonpath='{.data.bootstrap_admin_secret}' | base64 -d"
