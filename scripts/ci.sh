#!/usr/bin/env bash
# Single CI entrypoint (PLAT-04): `bash scripts/ci.sh`. Any CI system runs exactly this.
#
# Steps, in order, each fatal:
#   1. uv lock --check           the lock matches every pyproject.toml (never re-resolves, T-01-SC)
#   2. uv sync --frozen          install exactly what the lock says
#   3. ruff check / ruff format  lint and formatting (planning notes are not code: excluded)
#   4. mypy                      strict typing of every library, service and the gateway's Python
#                                (problem responder, api-docs merge and server)
#   5. lint-imports              services import only dc_* libraries; dc_* never import services
#   6. pytest -m unit            includes every service's route-policy walker, which fails the
#                                build when a route is declared with neither require() nor public()
#   7. pytest -m integration     when Docker is available and CI_INTEGRATION is not 0
#   8. ui                        generated-client drift, format, lint, types, unit tests and build
#                                (pnpm ci:static): natively when node matches ui/.nvmrc and pnpm is
#                                12.17.0, otherwise the Docker `ci` stage of
#                                infra/docker/ui.Dockerfile; without Node and Docker it warns, or
#                                fails when CI is set. The Python half of the drift check
#                                (tests/repo/test_ui_openapi_snapshot.py) already ran in step 6
#   9. cold-start e2e            only when CI_E2E=1: tear the stack down with its volumes, rebuild,
#                                bring it up, run the UI e2e (Playwright in the ui-e2e runner, so the
#                                UI journeys see the cold stack and share .dev/admin_password), then
#                                run the whole tests/e2e suite, which includes the Schemathesis
#                                conformance run of the merged OpenAPI document
#                                (tests/e2e/test_phase7_schemathesis.py, about 3 minutes)
#
# Environment toggles:
#   CI_INTEGRATION=0   skip step 7 even when Docker is available
#   CI_E2E=1           run step 9 (needs Docker and free port 8443)
#   CI_UI=0            skip the ui step (8) and the UI e2e inside step 9
#
# THE FULL GATE: `CI_FULL=1 bash scripts/ci.sh` or `bash scripts/ci.sh --full`. The most
# comprehensive run, about an hour on a cold stack; run it once per phase or before a release, not
# per edit (the default run above stays fast). It needs Docker and port 8443, refuses CI_E2E=0,
# CI_INTEGRATION=0 and CI_UI=0, and changes the steps as follows:
#   6. (HYPOTHESIS_PROFILE=ci for the whole run) unit tests under line+branch coverage (pytest-cov) with the backend operation tracer on
#      (DC_OP_TRACE), then the `timing` tests (CPU or wall-clock budgets) again without tracing
#   7. integration tests, always, appended to the same coverage data and trace; the `timing` ones
#      run afterwards without tracing, like the unit ones
#   7b. scripts/coverage_gate.py: every workspace member at or above its floor in
#      [tool.dc-coverage.thresholds] (pyproject.toml)
#   8. ui: `pnpm ci:full` (ci:static with Vitest V8 coverage and the thresholds of
#      ui/vitest.config.ts), natively or in the `ci-full` stage of infra/docker/ui.Dockerfile
#   9. the cold-start e2e, always: every Playwright spec and project (all personas, the layout
#      audit, dark), then the whole tests/e2e suite (Schemathesis of every routed service through
#      the gateway, the API-04 principal matrix, the e2e operation-coverage gate)
#   10. scripts/operation_coverage.py --gate: every OpenAPI operation has a backend test that
#      reaches it (2xx/3xx), a backend refusal (401/403/404) and a row in the generated
#      authorization matrix (tests/repo/authz_matrix.golden.json); the per-layer report, gateway
#      e2e and Playwright included, lands in .coverage-data/operation-coverage.json
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

for arg in "$@"; do
  case "$arg" in
    --full) CI_FULL=1 ;;
    *) echo "unknown argument: $arg (only --full is accepted)" >&2; exit 2 ;;
  esac
done
docker_available() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }
CI_FULL="${CI_FULL:-0}"
if [ "$CI_FULL" = "1" ]; then
  for toggle in CI_INTEGRATION CI_E2E CI_UI; do
    if [ "${!toggle:-1}" = "0" ]; then
      echo "CI_FULL=1 runs everything: unset $toggle=0" >&2
      exit 2
    fi
  done
  docker_available || { echo "CI_FULL=1 needs a running Docker daemon" >&2; exit 1; }
  CI_E2E=1
  rm -rf .coverage-data
  mkdir -p .coverage-data
  export DC_OP_TRACE="$PWD/.coverage-data/optrace.jsonl"
  # More Hypothesis examples where a suite reads it (the SQL-vs-oracle RBAC proof: 150, not 40).
  export HYPOTHESIS_PROFILE=ci
fi

step() { printf '\n==> %s\n' "$*"; }


step "uv lock --check"
uv lock --check

step "uv sync --frozen"
uv sync --frozen

step "ruff check"
uv run ruff check .

step "ruff format --check"
uv run ruff format --check --extend-exclude .planning .

step "mypy"
uv run mypy \
  shared/dc-core/src shared/dc-auth/src shared/dc-odcs/src shared/dc-rules/src \
  identity/src contracts/src validation/src audit/src mail/src \
  gateway/problem-responder/responder.py gateway/api-docs/merge.py gateway/api-docs/server.py \
  scripts/export_openapi.py scripts/coverage_gate.py scripts/operation_coverage.py \
  scripts/gen_http_reference.py scripts/publish/leak_check.py

step "lint-imports"
uv run lint-imports

# Tests marked `performance` (throughput and scale budgets) are left out unless DC_PERFORMANCE=1:
# Docker on development hardware cannot hold them. scripts/e2e.sh does the same.
perf=" and not performance"
if [ "${DC_PERFORMANCE:-0}" = "1" ]; then perf=""; fi

if [ "$CI_FULL" = "1" ]; then
  step "unit tests with line+branch coverage and the operation tracer (full gate)"
  uv run pytest -m "unit and not timing$perf" -q --cov --cov-report=
  step "timing-sensitive unit tests, without coverage tracing (full gate)"
  DC_OP_TRACE= uv run pytest -m "unit and timing$perf" -q
  step "integration tests with coverage (full gate)"
  uv run pytest -m "integration and not timing$perf" -q --cov --cov-append --cov-report=
  step "timing-sensitive integration tests, without coverage tracing (full gate)"
  DC_OP_TRACE= uv run pytest -m "integration and timing$perf" -q
  step "python coverage floors (scripts/coverage_gate.py)"
  uv run python scripts/coverage_gate.py
else
  step "unit tests (pytest -m unit)"
  uv run pytest -m "unit$perf" -q

  if [ "${CI_INTEGRATION:-1}" != "0" ]; then
    if docker_available; then
      step "integration tests (pytest -m integration)"
      uv run pytest -m "integration$perf" -q
    else
      echo "==> integration tests skipped: Docker is not available"
    fi
  else
    echo "==> integration tests skipped: CI_INTEGRATION=0"
  fi
fi

if [ "${CI_UI:-1}" = "0" ]; then
  echo "==> ui (generated client drift, lint, types, unit tests, build) skipped: CI_UI=0"
else
  step "ui (generated client drift, lint, types, unit tests, build)"
  ui_node_want="v$(tr -d '[:space:]' <ui/.nvmrc)"
  ui_script=ci:static
  ui_stage=ci
  if [ "$CI_FULL" = "1" ]; then ui_script=ci:full; ui_stage=ci-full; fi
  if command -v node >/dev/null 2>&1 && command -v pnpm >/dev/null 2>&1 \
    && [ "$(node --version)" = "$ui_node_want" ] && [ "$(pnpm --version)" = "12.17.0" ]; then
    pnpm --dir ui install --frozen-lockfile
    pnpm --dir ui "$ui_script"
  elif docker_available; then
    # Node and pnpm are pinned by the image, so no host tooling is needed or trusted.
    docker build --target "$ui_stage" -f infra/docker/ui.Dockerfile ui
  elif [ -n "${CI:-}" ]; then
    echo "ui step needs Node $ui_node_want with pnpm 12.17.0, or Docker; neither is available" >&2
    exit 1
  else
    echo "==> ui step skipped: neither Node $ui_node_want with pnpm 12.17.0 nor Docker is available (fails when CI is set)"
  fi
fi

if [ "${CI_E2E:-0}" = "1" ]; then
  docker_available || { echo "CI_E2E=1 needs a running Docker daemon" >&2; exit 1; }
  step "cold start (compose down -v, rebuild, up)"
  docker compose down -v --remove-orphans
  docker compose up -d --build --wait --wait-timeout 300
  if [ "${CI_UI:-1}" != "0" ]; then
    step "ui e2e tests (cold stack, before the Python e2e)"
    bash scripts/e2e-ui.sh
  else
    echo "==> ui e2e tests skipped: CI_UI=0"
  fi
  step "e2e tests (cold stack)"
  bash scripts/e2e.sh tests/e2e
fi

if [ "$CI_FULL" = "1" ]; then
  step "operation coverage gate (scripts/operation_coverage.py --gate)"
  uv run python scripts/operation_coverage.py --gate
fi

printf '\nci: all steps passed\n'
