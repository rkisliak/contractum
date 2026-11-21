#!/usr/bin/env bash
# Build a public copy of this repository as ONE fresh commit with no history.
#
# Manual only: never run this in CI. It pushes nothing and adds no remote; at the end it prints
# the next steps for you to run by hand. See docs/dev/publishing.md.
#
#   scripts/publish/export-public.sh [--out DIR]
#
# Steps: take a fresh `git clone --no-local` of the last commit (tracked content only), extract it
# into DIR (default: a new temporary folder; a given DIR must be empty or absent), remove the
# private folders (planning notes, GSD state, development secrets, scanner and coverage output,
# local and memory content under every .claude folder), strip the GSD workflow and profile blocks
# from .claude/CLAUDE.md, commit everything once as "Initial public release", then run the leak
# check (scripts/publish/leak_check.py: marker scan plus gitleaks with .gitleaks.toml). The
# commit author is your git user.name and user.email; the address is never written to a file.
set -euo pipefail

# gitleaks release used by the leak check. To upgrade: take every value from
# gitleaks_<version>_checksums.txt of the release and keep GITLEAKS_VERSION and
# GITLEAKS_SHA256_LINUX_X64 equal to GITLEAKS_VERSION and GITLEAKS_SHA256 in
# .github/workflows/security.yml (tests/repo/test_publish_leak_check.py checks this).
GITLEAKS_VERSION="8.30.1"
GITLEAKS_SHA256_LINUX_X64="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
GITLEAKS_SHA256_LINUX_ARM64="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
GITLEAKS_SHA256_DARWIN_X64="dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"
GITLEAKS_SHA256_DARWIN_ARM64="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"

die() {
  echo "export-public: $*" >&2
  exit 1
}

info() {
  echo "export-public: $*" >&2
}

usage() {
  echo "usage: scripts/publish/export-public.sh [--out DIR]"
}

OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out)
      [ $# -ge 2 ] || die "--out needs a directory"
      OUT="$2"
      shift 2
      ;;
    --out=*)
      OUT="${1#--out=}"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is required"
command -v uv >/dev/null 2>&1 || die "uv is required (it runs the leak check with Python 3.13)"

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
LEAK_CHECK="$REPO_ROOT/scripts/publish/leak_check.py"
[ -f "$LEAK_CHECK" ] || die "missing $LEAK_CHECK"

if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  info "warning: uncommitted changes are NOT exported; the export uses the last commit" \
    "($(git -C "$REPO_ROOT" rev-parse --short HEAD))"
fi

AUTHOR_NAME="$(git -C "$REPO_ROOT" config user.name || true)"
AUTHOR_EMAIL="$(git -C "$REPO_ROOT" config user.email || true)"
[ -n "$AUTHOR_NAME" ] && [ -n "$AUTHOR_EMAIL" ] ||
  die "set git config user.name and user.email first (used only as the export commit author)"

if [ -z "$OUT" ]; then
  OUT="$(mktemp -d "${TMPDIR:-/tmp}/contractum-public.XXXXXX")"
else
  if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT")" ]; then
    die "--out $OUT is not empty"
  fi
  mkdir -p "$OUT"
fi
OUT="$(cd "$OUT" && pwd -P)"
case "$OUT/" in
  "$(cd "$REPO_ROOT" && pwd -P)/"*) die "--out must be outside the repository" ;;
esac

WORK="$(mktemp -d "${TMPDIR:-/tmp}/contractum-export-work.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# Prints the path of a gitleaks binary of exactly GITLEAKS_VERSION: one on PATH, else the official
# release, downloaded once into the user cache and verified against the SHA-256 table above.
resolve_gitleaks() {
  local found os arch var expected cache bin tarball actual
  found="$(command -v gitleaks 2>/dev/null || true)"
  if [ -n "$found" ] && [ "$("$found" version 2>/dev/null)" = "$GITLEAKS_VERSION" ]; then
    echo "$found"
    return 0
  fi
  case "$(uname -s)" in
    Darwin) os=darwin ;;
    Linux) os=linux ;;
    *) die "no pinned gitleaks build for $(uname -s); put gitleaks $GITLEAKS_VERSION on PATH" ;;
  esac
  case "$(uname -m)" in
    x86_64 | amd64) arch=x64 ;;
    arm64 | aarch64) arch=arm64 ;;
    *) die "no pinned gitleaks build for $(uname -m); put gitleaks $GITLEAKS_VERSION on PATH" ;;
  esac
  var="GITLEAKS_SHA256_$(printf '%s_%s' "$os" "$arch" | tr '[:lower:]' '[:upper:]')"
  expected="${!var}"
  cache="${XDG_CACHE_HOME:-$HOME/.cache}/contractum/gitleaks/$GITLEAKS_VERSION"
  bin="$cache/gitleaks"
  if [ ! -x "$bin" ]; then
    command -v curl >/dev/null 2>&1 || die "curl is required to download gitleaks"
    info "downloading gitleaks $GITLEAKS_VERSION ($os/$arch)"
    tarball="$WORK/gitleaks.tar.gz"
    curl -fsSL -o "$tarball" \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_${os}_${arch}.tar.gz"
    actual="$(sha256_of "$tarball")"
    [ "$actual" = "$expected" ] || die "gitleaks checksum mismatch: expected $expected, got $actual"
    mkdir -p "$WORK/gitleaks" "$cache"
    tar -xzf "$tarball" -C "$WORK/gitleaks" gitleaks
    mv "$WORK/gitleaks/gitleaks" "$bin"
    chmod 0755 "$bin"
  fi
  [ "$("$bin" version)" = "$GITLEAKS_VERSION" ] || die "$bin is not gitleaks $GITLEAKS_VERSION"
  echo "$bin"
}

GITLEAKS="$(resolve_gitleaks)"

info "cloning the last commit of $REPO_ROOT"
git clone --no-local --quiet "$REPO_ROOT" "$WORK/clone"
SOURCE_COMMIT="$(git -C "$WORK/clone" rev-parse --short HEAD)"
git -C "$WORK/clone" archive --format=tar HEAD | tar -x -C "$OUT"
[ -n "$(ls -A "$OUT")" ] || die "the export tree is empty"

info "removing private folders and files"
# Planning notes, GSD state, development secrets, scanner output, coverage data and site output.
# Most are untracked and so already absent from the clone; remove them defensively anyway.
for path in .planning .gsd .dev .security-reports .coverage-data htmlcov site \
  .coverage coverage.xml clients/python/coverage.xml ui/coverage; do
  rm -rf -- "${OUT:?}/$path"
done
# Local and memory content under every .claude folder (the root one and each module's).
while IFS= read -r -d '' claude_dir; do
  rm -rf -- "$claude_dir/worktrees" "$claude_dir/projects" "$claude_dir/memory" \
    "$claude_dir/settings.local.json"
  find "$claude_dir" -type f -iname 'memory.md' -delete
done < <(find "$OUT" -type d -name .claude -print0)

if [ -f "$OUT/.claude/CLAUDE.md" ]; then
  uv run --quiet --no-project --python 3.13 python -I "$LEAK_CHECK" strip-claude-md \
    "$OUT/.claude/CLAUDE.md"
fi

info "creating the single export commit"
git -C "$OUT" init -q -b main
git -C "$OUT" add --all
GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
  GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
  git -C "$OUT" -c core.hooksPath=/dev/null -c commit.gpgsign=false \
  commit -q -m "Initial public release"
# A tracked file that matches .gitignore would be left out silently: fail instead.
if [ -n "$(git -C "$OUT" status --porcelain --ignored)" ]; then
  git -C "$OUT" status --short --ignored >&2
  die "some exported files were not committed (see above)"
fi

info "running the leak check"
if ! uv run --quiet --no-project --python 3.13 python -I "$LEAK_CHECK" scan "$OUT" \
  --owner-email "$AUTHOR_EMAIL" --gitleaks "$GITLEAKS"; then
  die "leak check FAILED: do not publish $OUT. Fix each finding at the source (or add a" \
    "reasoned allowlist entry in scripts/publish/leak_check.py), commit, and run the export again."
fi

FILES="$(git -C "$OUT" ls-files | wc -l | tr -d ' ')"
COMMITS="$(git -C "$OUT" rev-list --count HEAD)"
cat <<NEXT

Public export ready (leak check passed).
  Path:    $OUT
  Source:  commit $SOURCE_COMMIT
  Commits: $COMMITS (no history)
  Files:   $FILES

Nothing was pushed and no remote was added. Next steps, by hand:
  1. Review the export once more (README, licence, docs) as a reader would.
  2. Create a NEW, empty repository (no README, licence or .gitignore).
  3. git -C "$OUT" remote add origin <new-repo-url>
  4. git -C "$OUT" push -u origin main
NEXT
