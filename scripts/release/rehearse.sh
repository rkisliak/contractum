#!/usr/bin/env bash
# Rehearsal of build once, test by digest, promote by digest, on a real image and a throwaway
# registry: `bash scripts/release/rehearse.sh`.
#
# It builds the real api-docs image once (with SBOM and provenance), pushes it to a local registry
# as the release candidate 9.9.9-rc.1 under all 7 component repositories, then runs the same
# digest operations the Release workflow runs (scripts/release/release_images.py and
# scripts/release/tested_images.sh): the manifest, the tested record, the retag to 9.9.9 and latest
# without a rebuild, a moved candidate tag that must be refused, the digest-pinned chart, and the
# version the image reports at startup. GitHub attestations cannot be rehearsed offline; the
# unit tests cover that step with a stub.
#
# It never touches GHCR, the compose stack or any local contractum/* image tag. Everything it
# creates (registry container, builder, run container, pulled image, temp files) is removed on exit.
# REHEARSE_PORT (default 5057) is the host port of the registry on 127.0.0.1.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

# Pinned by digest (registry:3).
REGISTRY_IMAGE="registry:3@sha256:82f6ff4a7f982960c8aa51617fea3aabc485df6e55cce80d22deb1de36f1bc3c"
PORT="${REHEARSE_PORT:-5057}"
REGISTRY="127.0.0.1:${PORT}"
NAMESPACE="${REGISTRY}/rehearsal"
SUFFIX="$$"
REGISTRY_NAME="contractum-rehearse-registry-${SUFFIX}"
BUILDER="contractum-rehearse-${SUFFIX}"
RUN_NAME="contractum-rehearse-run-${SUFFIX}"
RC_VERSION="9.9.9-rc.1"
VERSION="9.9.9"
IMAGES=(identity contracts validation audit mail api-docs ui)

WORK="$(mktemp -d)"
RUN_IMAGE=""

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'rehearsal failed: %s\n' "$*" >&2; exit 1; }

cleanup() {
  status=$?
  docker rm -f "$RUN_NAME" >/dev/null 2>&1 || true
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  docker buildx rm --force "$BUILDER" >/dev/null 2>&1 || true
  if [ -n "$RUN_IMAGE" ]; then docker image rm "$RUN_IMAGE" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
  if [ "$status" -eq 0 ]; then echo "rehearsal passed; everything it created is removed"; fi
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v helm >/dev/null 2>&1 || fail "helm is required"

chart_sum() { find deploy/helm/contractum -type f -print0 | sort -z | xargs -0 shasum | shasum; }
chart_before="$(chart_sum)"

tool="scripts/release/release_images.py"
export DC_RELEASE_IMAGES="$WORK/tested-images.json"
export BUILDX_BUILDER="$BUILDER"

commit="$(git rev-parse HEAD)"
git_sha="$(git rev-parse --short=12 HEAD)"
build_time="$(git log -1 --format=%cI HEAD)"

step "start the throwaway registry on ${REGISTRY} and a host-network builder"
docker run -d --name "$REGISTRY_NAME" -p "127.0.0.1:${PORT}:5000" "$REGISTRY_IMAGE" >/dev/null
docker buildx create --name "$BUILDER" --driver docker-container \
  --driver-opt network=host >/dev/null
docker buildx inspect "$BUILDER" --bootstrap >/dev/null
for _ in $(seq 1 30); do
  if curl -fsS "http://${REGISTRY}/v2/" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://${REGISTRY}/v2/" >/dev/null || fail "the registry does not answer on ${REGISTRY}"

step "build the real api-docs image once, push it as ${RC_VERSION} under all 7 repositories"
tags=()
for name in "${IMAGES[@]}"; do
  tags+=(--tag "${NAMESPACE}/contractum/${name}:${RC_VERSION}")
done
docker buildx build -f infra/docker/api-docs.Dockerfile \
  --sbom=true --provenance=mode=max \
  --build-arg "GIT_SHA=${git_sha}" --build-arg "BUILD_TIME=${build_time}" \
  "${tags[@]}" --push .
digest="$(docker buildx imagetools inspect "${NAMESPACE}/contractum/identity:${RC_VERSION}" \
  --format '{{json .Manifest.Digest}}' | tr -d '"[:space:]')"
case "$digest" in sha256:*) ;; *) fail "no digest read back: ${digest}" ;; esac
echo "digest ${digest}"

step "assemble, record, check"
mkdir "$WORK/digests"
for name in "${IMAGES[@]}"; do printf '%s\n' "$digest" >"$WORK/digests/$name"; done
python3 "$tool" assemble --digests "$WORK/digests" --version "$RC_VERSION" --commit "$commit" \
  --git-sha "$git_sha" --registry "$NAMESPACE" --output "$WORK/release-images.json"
if python3 "$tool" record --manifest "$WORK/release-images.json" --run-id 1 \
  --e2e failure --dast success --output "$WORK/refused.json" 2>/dev/null; then
  fail "record accepted a failed E2E result"
fi
[ ! -e "$WORK/refused.json" ] || fail "record wrote a file for a failed E2E result"
python3 "$tool" record --manifest "$WORK/release-images.json" --run-id 1 \
  --e2e success --dast success --output "$DC_RELEASE_IMAGES"
python3 "$tool" check --manifest "$DC_RELEASE_IMAGES" --version "$RC_VERSION" --commit "$commit" \
  --registry "$NAMESPACE" --tested
bash scripts/release/tested_images.sh verify-tags "$RC_VERSION"

step "tag ${VERSION} and latest on the tested digests, without a rebuild"
bash scripts/release/tested_images.sh tag "$VERSION"
bash scripts/release/tested_images.sh tag latest

step "a candidate tag moved to another build is refused"
docker buildx build -f infra/docker/api-docs.Dockerfile --provenance=false \
  --build-arg "GIT_SHA=${git_sha}" --build-arg "BUILD_TIME=moved" \
  --tag "${NAMESPACE}/contractum/identity:${RC_VERSION}" --push . >/dev/null
if bash scripts/release/tested_images.sh verify-tags "$RC_VERSION" 2>"$WORK/moved.log"; then
  fail "verify-tags accepted a moved candidate tag"
fi
cat "$WORK/moved.log"
bash scripts/release/tested_images.sh verify-tags "$VERSION"

step "the chart: pinned digests on a copy, packaged as ${VERSION}, rendered"
cp -R deploy/helm/contractum "$WORK/chart"
python3 "$tool" pin-chart --manifest "$DC_RELEASE_IMAGES" --values "$WORK/chart/values.yaml"
helm package "$WORK/chart" --version "$VERSION" --app-version "$VERSION" \
  --destination "$WORK/dist" >/dev/null
helm template dc "$WORK/dist/contractum-${VERSION}.tgz" \
  --set "global.imageRegistry=${NAMESPACE}" >"$WORK/rendered.yaml"
python3 "$tool" check-rendered --manifest "$DC_RELEASE_IMAGES" --version "$VERSION" \
  --rendered "$WORK/rendered.yaml"
helm template dc deploy/helm/contractum --set "global.imageRegistry=${NAMESPACE}" \
  --set "global.imageTag=${VERSION}" >"$WORK/unpinned.yaml"
if python3 "$tool" check-rendered --manifest "$DC_RELEASE_IMAGES" --version "$VERSION" \
  --rendered "$WORK/unpinned.yaml" 2>/dev/null; then
  fail "check-rendered accepted an image without its digest"
fi
[ "$(chart_sum)" = "$chart_before" ] || fail "the rehearsal modified the chart in the working tree"

step "run the image by digest with the version given at startup"
RUN_IMAGE="${NAMESPACE}/contractum/api-docs@${digest}"
docker run -d --name "$RUN_NAME" --network none -e "DC_VERSION=${VERSION}" "$RUN_IMAGE" >/dev/null
reported=""
for _ in $(seq 1 30); do
  if reported="$(docker exec "$RUN_NAME" python -c \
    "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/api/v1/docs/version').read().decode())" \
    2>/dev/null)"; then break; fi
  sleep 1
done
[ -n "$reported" ] || fail "the version route did not answer"
echo "$reported"
got="$(printf '%s' "$reported" | python3 -I -c 'import json, sys; d = json.load(sys.stdin); print(d["version"], d["commit"])')"
[ "$got" = "${VERSION} ${git_sha}" ] || fail "the image reports '${got}', expected '${VERSION} ${git_sha}'"
config="$(docker image inspect "$RUN_IMAGE" --format '{{json .Config.Env}} {{json .Config.Labels}}')"
case "$config" in
  *DC_VERSION*) fail "the image config holds DC_VERSION" ;;
  *org.opencontainers.image.version*) fail "the image config holds the version label" ;;
esac
