#!/usr/bin/env bash
# Warm-stack e2e runner: `bash scripts/e2e.sh <pytest paths/args>`.
#
# Brings the stack up incrementally (rebuilds only changed images, never removes containers or
# volumes), then runs the e2e container on the compose networks and propagates pytest's exit code.
# Cold starts (teardown with volume removal) are reserved for plan-level verification and
# `CI_E2E=1 scripts/ci.sh`. Tests marked `performance` (load profiles) are left out unless
# DC_PERFORMANCE=1: Docker on development hardware cannot hold them.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

marker="e2e and not performance"
if [ "${DC_PERFORMANCE:-0}" = "1" ]; then marker="e2e"; fi

# Created by the caller, so the binds belong to the user the runner runs as (not to root).
mkdir -p .dev ui/test-results
docker compose up -d --build --wait --wait-timeout "${E2E_WAIT_TIMEOUT:-300}"
# The runner's non-root user and groups (the ./.dev bind, the e2e secrets, the Docker socket).
source scripts/e2e-runner-env.sh
exec docker compose -f compose.yaml -f compose.test.yaml --profile test \
  run --rm --build e2e pytest -m "$marker" -q -o asyncio_default_test_loop_scope=session "$@"
