"""Docs-only detection and the ci-ok verdict for .github/workflows/ci.yml (stdlib only).

``classify`` reads the paths a pull request or push changed (the workflow writes
``git diff --name-only`` to a file) and writes ``docs_only=true`` to ``$GITHUB_OUTPUT`` only when
every changed path is a documentation file (:data:`DOCS_PATTERNS`). Anything unknown fails safe to
``docs_only=false``, so the full CI runs: a missing base commit (a new branch, a force push), a
failing ``git diff`` (the workflow then removes the file) or an empty diff.

``verdict`` is the logic of the required ``ci-ok`` job. It reads the ``needs`` context as JSON and
passes only when every job that should have run succeeded. On a docs-only change the code jobs
(:data:`CODE_JOBS`) may be skipped and the docs-only repo tests (:data:`DOCS_ONLY_JOBS`) must
pass; on any other change the code jobs must pass and the docs-only jobs may be skipped. A failed,
cancelled or unexpectedly skipped job always fails the verdict.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from collections.abc import Iterable, Mapping

# A change touching only these paths skips the code jobs. The Documentation and Security
# workflows always run, and the docs-only repo tests (tests/repo) still check the docs.
DOCS_PATTERNS: tuple[str, ...] = (
    "docs/**",  # pages, diagrams and their images
    "*.md",  # any Markdown file, at any depth
    "mkdocs.yml",
    ".markdownlint-cli2.jsonc",
    "_typos.toml",
    "lychee.toml",
)

# Skipped on a docs-only change, required on every other change.
CODE_JOBS: frozenset[str] = frozenset(
    {"python-lint", "python-unit", "python-integration", "ui", "docker-build"}
)
# Required on a docs-only change, skipped on every other change.
DOCS_ONLY_JOBS: frozenset[str] = frozenset({"docs-repo-tests"})
# Required on every change.
ALWAYS_JOBS: frozenset[str] = frozenset({"changes"})


def is_docs_path(path: str) -> bool:
    """True when ``path`` (repo-relative, forward slashes) is a documentation file."""
    for pattern in DOCS_PATTERNS:
        if pattern.endswith("/**"):
            if path.startswith(pattern[: -len("**")]):
                return True
        elif "/" not in pattern and pattern.startswith("*"):
            if fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern):
                return True
        elif path == pattern:
            return True
    return False


def docs_only(paths: Iterable[str]) -> bool:
    """True only for a non-empty change set whose every path is a documentation file."""
    changed = [path for path in paths if path]
    return bool(changed) and all(is_docs_path(path) for path in changed)


def read_paths(paths_file: str) -> list[str] | None:
    """The changed paths listed one per line in ``paths_file``, or None when it is missing.

    The workflow removes the file when ``git diff`` fails (an unknown base commit, for example
    the null SHA of a new branch), so a missing file means "unknown" and runs the full CI.
    """
    try:
        with open(paths_file, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except FileNotFoundError:
        return None


def verdict(needs: Mapping[str, Mapping[str, object]], docs_only_change: bool) -> list[str]:
    """The reasons ci-ok fails (an empty list means it passes)."""
    problems: list[str] = []
    expected = ALWAYS_JOBS | CODE_JOBS | DOCS_ONLY_JOBS
    missing = sorted(expected - needs.keys())
    unknown = sorted(needs.keys() - expected)
    if missing:
        problems.append(f"ci-ok does not wait for: {', '.join(missing)}")
    if unknown:
        problems.append(f"ci-ok does not know the rule for: {', '.join(unknown)}")
    for job, need in sorted(needs.items()):
        result = need.get("result")
        if result == "success":
            continue
        may_skip = (job in CODE_JOBS and docs_only_change) or (
            job in DOCS_ONLY_JOBS and not docs_only_change
        )
        if result == "skipped" and may_skip:
            continue
        problems.append(f"{job}: {result}")
    return problems


def _write_output(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    print(f"{name}={value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    classify = sub.add_parser("classify", help="decide whether a change is docs-only")
    classify.add_argument(
        "--paths-file", required=True, help="`git diff --name-only` output, one path per line"
    )
    check = sub.add_parser("verdict", help="the ci-ok decision")
    check.add_argument("--needs", required=True, help="the needs context as JSON")
    check.add_argument("--docs-only", required=True, help="'true' for a docs-only change")
    args = parser.parse_args(argv)

    if args.command == "classify":
        paths = read_paths(args.paths_file)
        if paths is None:
            print("changed paths unknown: running the full CI")
            _write_output("docs_only", "false")
            return 0
        print("changed paths:", *paths, sep="\n  ")
        _write_output("docs_only", "true" if docs_only(paths) else "false")
        return 0

    needs = json.loads(args.needs)
    problems = verdict(needs, args.docs_only == "true")
    for job, need in sorted(needs.items()):
        print(f"{job}: {need.get('result')}")
    if problems:
        print("ci failed:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print("ci ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
