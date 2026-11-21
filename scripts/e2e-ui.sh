#!/usr/bin/env bash
# Warm-stack UI e2e runner: `bash scripts/e2e-ui.sh <spec paths/args>`, for example
# `bash scripts/e2e-ui.sh e2e/smoke.spec.ts -g axe`.
#
# Brings the stack up incrementally (rebuilds only changed images, never removes containers or
# volumes), then runs Playwright in the pinned `ui-e2e` container on the compose edge network and
# propagates its exit code. A leading `ui/` on a path is accepted (the specs live in ui/e2e).
# Traces and failure artifacts land in ui/test-results (bind-mounted, gitignored). Cold starts
# are reserved for `CI_E2E=1 scripts/ci.sh`.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

args=()
for arg in "$@"; do
  args+=("${arg#ui/}")
done

# The images carry the commit they were built from (GET /api/v1/<module>/version, the UI's
# "Versions" dialog); BUILD_TIME stays "dev" unless the caller sets it, so containers are not
# recreated on every run.
export GIT_SHA="${GIT_SHA:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"

docker compose up -d --build --wait --wait-timeout "${E2E_WAIT_TIMEOUT:-300}"
exec docker compose -f compose.yaml -f compose.test.yaml --profile test \
  run --rm --build ui-e2e pnpm exec playwright test ${args[@]+"${args[@]}"}
