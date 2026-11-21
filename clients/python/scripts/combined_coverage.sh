#!/usr/bin/env bash
# Combined SDK coverage, the single definition used locally and by the sonarqube job (D-48).
#
# The SDK coverage figure is the unit and the integration runs measured together and combined:
# code that only the integration tests reach (the HTTP transport against the real server, the
# auth round trips) would otherwise read as uncovered. A unit-only figure is never the SDK figure,
# so this script labels every run and refuses to fall back silently.
#
# Usage (from anywhere):
#   bash clients/python/scripts/combined_coverage.sh                        # unit only, labelled so
#   bash clients/python/scripts/combined_coverage.sh --require-integration  # CI: exit 2 without a stack
#
# Environment for the integration run (all read by the integration tests, never printed here):
#   CONTRACTUM_IT_URL                    base URL of the running stack, for example https://localhost:8443
#   CONTRACTUM_IT_CA                     CA certificate file the stack's TLS certificate chains to
#   CONTRACTUM_IT_ADMIN_PASSWORD_FILE    file holding the bootstrap admin password
#
# Output: clients/python/coverage.xml with repository-root-relative file paths (for Sonar) and a
# final "coverage measured from: <label>" line. Exit codes: 0 ok, 1 a test failed, coverage is
# below 98 percent or an XML path does not resolve, 2 integration was required but not available.
set -euo pipefail

require_integration=false
for arg in "$@"; do
  case "$arg" in
    --require-integration) require_integration=true ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [ -n "${CONTRACTUM_IT_URL:-}" ]; then
  label="unit+integration"
elif [ "$require_integration" = true ]; then
  echo "--require-integration was passed but CONTRACTUM_IT_URL is not set: refusing to measure unit only" >&2
  exit 2
else
  label="unit only"
fi

cd "$(git rev-parse --show-toplevel)/clients/python"

uv run coverage erase
uv run coverage run -m pytest -m unit -q
if [ "$label" = "unit+integration" ]; then
  uv run coverage run -m pytest -m integration -q
fi

# [tool.coverage.paths] folds subprocess and integration data recorded under other working
# directories onto src/contractum.
uv run coverage combine -q
uv run coverage report --fail-under=98
uv run coverage xml -o coverage.xml
uv run python scripts/coverage_xml_for_sonar.py coverage.xml

echo "coverage measured from: ${label}"
