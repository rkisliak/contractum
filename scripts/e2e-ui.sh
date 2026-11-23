#!/usr/bin/env bash
# Warm-stack UI e2e runner: `bash scripts/e2e-ui.sh <spec paths/args>`, for example
# `bash scripts/e2e-ui.sh e2e/smoke.spec.ts -g axe`.
#
# Brings the stack up incrementally (rebuilds only changed images, never removes containers or
# volumes), then runs Playwright in the pinned `ui-e2e` container on the compose edge network and
# propagates its exit code. A leading `ui/` on a path is accepted (the specs live in ui/e2e).
# Traces and failure artifacts land in ui/test-results (bind-mounted, gitignored). Cold starts
# are reserved for `CI_E2E=1 scripts/ci.sh`.
#
# With DC_RELEASE_IMAGES set (a release image manifest, the Release workflow's call of E2E
# Nightly) nothing is built except the runner: the stack starts with `--no-build` on the images
# scripts/release/tested_images.sh load put there, and the runner is built on its own, because
# `run --build` could also rebuild its dependencies and replace the tested images.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

args=()
for arg in "$@"; do
  args+=("${arg#ui/}")
done

# The images carry the commit they were built from (GET /api/v1/<module>/version, the UI's
# "Versions" dialog) in the release format, 12 characters; BUILD_TIME stays "dev" unless the caller
# sets it, so containers are not recreated on every run. The platform version is not built in: it
# is DC_VERSION when the caller sets it, "dev" otherwise.
export GIT_SHA="${GIT_SHA:-$(git rev-parse --short=12 HEAD 2>/dev/null || echo dev)}"

# Created by the caller, so the binds belong to the user the runner runs as (not to root).
mkdir -p .dev ui/test-results
# The runner's non-root user and groups (the ./.dev and ui/test-results binds, the e2e secrets).
source scripts/e2e-runner-env.sh
if [ -n "${DC_RELEASE_IMAGES:-}" ]; then
  docker compose up -d --no-build --wait --wait-timeout "${E2E_WAIT_TIMEOUT:-300}"
  docker compose -f compose.yaml -f compose.test.yaml --profile test build ui-e2e
  exec docker compose -f compose.yaml -f compose.test.yaml --profile test \
    run --rm ui-e2e pnpm exec playwright test ${args[@]+"${args[@]}"}
fi
docker compose up -d --build --wait --wait-timeout "${E2E_WAIT_TIMEOUT:-300}"
exec docker compose -f compose.yaml -f compose.test.yaml --profile test \
  run --rm --build ui-e2e pnpm exec playwright test ${args[@]+"${args[@]}"}
