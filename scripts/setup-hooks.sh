#!/bin/sh
# Turn on the repository's git hooks (.githooks: the docs gate) for this clone, and download the
# pinned Vale packages the pre-commit hook needs. Run once per clone, from anywhere in it:
#   sh scripts/setup-hooks.sh
# The pre-commit hook also needs Vale 3.28.1 as `vale` on PATH, or a running Docker (see
# "The docs gate" in docs/dev/ci-and-github.md).
set -eu
root=$(git rev-parse --show-toplevel)
cd "$root"
current=$(git config --get core.hooksPath || true)
if [ -n "$current" ] && [ "$current" != ".githooks" ]; then
  echo "core.hooksPath is already '$current'. Call .githooks/pre-commit and .githooks/commit-msg" >&2
  echo "from your hooks there, or run: git config core.hooksPath .githooks" >&2
  exit 1
fi
git config core.hooksPath .githooks
echo "git hooks: core.hooksPath = .githooks"
python3 scripts/docs_gate.py --sync
