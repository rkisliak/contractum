#!/usr/bin/env bash
# Build and check the release files of the contractum SDK and CLI, the single definition used by the
# platform release (.github/workflows/release.yml, job sdk) and its pull-request dry run
# (.github/workflows/sdk.yml, job sdk-package).
#
# Usage (from anywhere, on Linux with git, uv, unzip, tar and sha256sum):
#   bash clients/python/scripts/build_release.sh 1.1.0rc1
#   bash clients/python/scripts/build_release.sh --set-only 1.1.0rc1   # set the version, no build
#
# The argument is the release version in PEP 440 form (1.1.0 or 1.1.0rc1 for the platform version
# 1.1.0-rc.1). The committed version in pyproject.toml is the placeholder 0.0.0.dev0: this script
# sets the release version in the checkout only (`uv version`, never committed) and checks that
# the lock moved nothing but the project's own version line, so the released files carry exactly
# the tested dependency pins. It then builds the wheel and the sdist, installs each on the oldest
# and newest supported Python (3.11, 3.14) and checks `contractum --version`, checks the Apache-2.0
# LICENSE and NOTICE in both files and writes dist/SHA256SUMS. With --set-only it stops after the
# version and lock checks (the CLI image job, which builds from the same checkout).
set -euo pipefail

set_only=false
if [ "${1:-}" = "--set-only" ]; then
  set_only=true
  shift
fi
if [ "$#" -ne 1 ]; then
  echo "usage: build_release.sh [--set-only] <pep440-version>" >&2
  exit 2
fi
version="$1"
if ! printf '%s' "$version" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?'; then
  echo "not a release version (X.Y.Z or X.Y.ZrcN): $version" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

uv version "$version" --no-sync
actual=$(uv version --short)
if [ "$actual" != "$version" ]; then
  echo "pyproject.toml reads $actual, expected $version" >&2
  exit 1
fi
# Relocking must change the contractum entry's version line and nothing else.
lock_change=$(git diff --numstat -- uv.lock | cut -f1,2)
if [ "$lock_change" != "$(printf '1\t1')" ]; then
  echo "setting the version changed more of uv.lock than the project version:" >&2
  git diff -- uv.lock >&2
  exit 1
fi
if [ "$set_only" = true ]; then
  echo "contractum version set to $version"
  exit 0
fi

uv build --clear

wheel="dist/contractum-${version}-py3-none-any.whl"
sdist="dist/contractum-${version}.tar.gz"
for artefact in "$wheel" "$sdist"; do
  for py in 3.11 3.14; do
    out=$(uv run --isolated --no-project --python "$py" --with "$artefact" contractum --version)
    echo "$artefact on python $py: $out"
    if [ "$out" != "contractum ${version}" ]; then
      echo "expected: contractum ${version}" >&2
      exit 1
    fi
  done
done

# The SDK is Apache-2.0: both files must carry clients/python/LICENSE and NOTICE, never the
# repository's noncommercial server licence. The files are unpacked first, so no early-exiting grep
# in a pipeline can hide a match. The pattern is written as a regular expression that does not
# match itself, because this script ships in the sdist.
unpacked=$(mktemp -d)
trap 'rm -rf "$unpacked"' EXIT
unzip -q "$wheel" -d "$unpacked/wheel"
mkdir "$unpacked/sdist"
tar -xzf "$sdist" -C "$unpacked/sdist"
info="$unpacked/wheel/contractum-${version}.dist-info"
grep -qx "License-Expression: Apache-2.0" "$info/METADATA"
cmp "$info/licenses/LICENSE" LICENSE
cmp "$info/licenses/NOTICE" NOTICE
cmp "$unpacked/sdist/contractum-${version}/LICENSE" LICENSE
cmp "$unpacked/sdist/contractum-${version}/NOTICE" NOTICE
for kind in wheel sdist; do
  if grep -rq "Noncommercial[ ]License" "$unpacked/$kind"; then
    echo "the $kind carries the noncommercial server licence" >&2
    exit 1
  fi
done

(
  cd dist
  sha256sum "contractum-${version}-py3-none-any.whl" "contractum-${version}.tar.gz" > SHA256SUMS
  sha256sum -c SHA256SUMS
  cat SHA256SUMS
)
