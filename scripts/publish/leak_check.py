"""Leak check and CLAUDE.md strip for the manual public export (``export-public.sh`` next to it).

Standard library only, so the export can run it with any Python 3.13 (``uv run --no-project``).

    leak_check.py scan <tree> [--owner-email ADDR ...] [--gitleaks PATH]
    leak_check.py strip-claude-md <file>

``scan`` walks every regular file of ``<tree>`` except ``.git/``, skips binary files, and reports
one line per finding as ``relative/path:line: <marker name>``. It never prints the matched text.
Markers: absolute home paths, the agent project folder under ``.claude``, the agent memory index
file name, the planning folder used as a path, and the owner's email addresses (read from the
tree's LICENSE plus every ``--owner-email``). The owner's address may appear only in LICENSE and
NOTICE files; planning-folder mentions only in the files listed in ``ALLOWLIST``, each with a
reason. Private folders and files that the export removes are findings when still present. With
``--gitleaks PATH`` it also runs gitleaks over the tree with the tree's own ``.gitleaks.toml``.
Exit 1 on any finding, 0 when clean.

``strip-claude-md`` removes the GSD workflow and developer-profile blocks from a CLAUDE.md file in
place and exits non-zero, leaving the file untouched, when a block marker is missing.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

HOME_PATH = "home-path"
CLAUDE_PROJECTS = "claude-projects"
MEMORY_FILE = "memory-file"
PLANNING_PATH = "planning-path"
OWNER_EMAIL = "owner-email"
EXCLUDED_PATH = "excluded-path"

# Written so that none of them matches its own source text.
CONTENT_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # An absolute path: the leading slash must not follow a name (src/features/home/ is fine).
    (HOME_PATH, re.compile(r"(?<![\w.-])/(?:Users|home)/[A-Za-z0-9._-]+")),
    (CLAUDE_PROJECTS, re.compile(r"\.claude[/\\]projects\b")),
    (MEMORY_FILE, re.compile(r"\bMEMORY\.md\b")),
    # The planning folder as a path component, never inside a longer word.
    (PLANNING_PATH, re.compile(r"(?<![\w.-])\.planning(?![\w-])")),
)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Files where the owner's address belongs (licence notices); matched on the file name.
EMAIL_ALLOWED_NAMES = frozenset({"LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "NOTICE.md"})

# (path, marker, reason): functional mentions of the planning folder. Never add an email, a home
# path or a memory marker here; fix those at the source.
ALLOWLIST: tuple[tuple[str, str, str], ...] = (
    (".dockerignore", PLANNING_PATH, "keeps the planning folder out of image build contexts"),
    (".gitignore", PLANNING_PATH, "ignores the planning tool's runtime state files"),
    (".github/workflows/ci.yml", PLANNING_PATH, "ruff format --extend-exclude of the folder"),
    ("scripts/ci.sh", PLANNING_PATH, "ruff format --extend-exclude of the folder"),
    (".gitleaks.toml", PLANNING_PATH, "history-only allowlist entries for planning files"),
    (
        "tests/repo/test_no_default_secrets.py",
        PLANNING_PATH,
        "the secret scan skips planning notes",
    ),
    ("_typos.toml", PLANNING_PATH, "the spell check excludes the planning folder"),
    ("lychee.toml", PLANNING_PATH, "the link check excludes the planning folder"),
    (".markdownlint-cli2.jsonc", PLANNING_PATH, "markdown lint ignores the planning folder"),
    ("scripts/publish/leak_check.py", PLANNING_PATH, "this checker names the folder it rejects"),
    ("scripts/publish/export-public.sh", PLANNING_PATH, "the export removes the folder by name"),
    ("tests/repo/test_publish_leak_check.py", PLANNING_PATH, "tests plant the marker on purpose"),
    ("tests/repo/test_docs_site.py", PLANNING_PATH, "guards docs against links to the folder"),
    ("docs/dev/publishing.md", PLANNING_PATH, "explains which folder the export removes"),
)

# Folder names that never ship, at any depth.
EXCLUDED_DIRS = frozenset({".planning", ".gsd", ".dev", ".security-reports", ".coverage-data"})
# Entries directly inside any .claude folder that never ship (local and memory content).
EXCLUDED_UNDER_CLAUDE = frozenset({"worktrees", "projects", "memory", "settings.local.json"})
# File names that never ship, at any depth (coverage data and the memory index).
EXCLUDED_FILES = frozenset({".coverage", "coverage.xml", "MEMORY" + ".md"})

BINARY_PROBE = 8192


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    marker: str

    def __str__(self) -> str:
        if self.line:
            return f"{self.path}:{self.line}: {self.marker}"
        return f"{self.path}: {self.marker}"


class StripError(ValueError):
    """A CLAUDE.md block marker is missing or repeated."""


def _allowed(rel: str, marker: str) -> bool:
    if marker == OWNER_EMAIL:
        return Path(rel).name in EMAIL_ALLOWED_NAMES
    return any(path == rel and m == marker for path, m, _ in ALLOWLIST)


def _excluded(parts: Sequence[str]) -> bool:
    name = parts[-1]
    if name in EXCLUDED_DIRS or name in EXCLUDED_FILES:
        return True
    return len(parts) >= 2 and parts[-2] == ".claude" and name in EXCLUDED_UNDER_CLAUDE


def _walk(tree: Path) -> Iterator[tuple[str, bool]]:
    """Yield (relative path, is_excluded) for regular files, and for excluded folders once."""
    for dirpath, dirnames, filenames in os.walk(tree):
        base = Path(dirpath).relative_to(tree)
        keep: list[str] = []
        for name in sorted(dirnames):
            rel = base / name
            if rel.parts == (".git",):
                continue
            if _excluded(rel.parts):
                yield rel.as_posix(), True
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = base / name
            full = tree / rel
            if full.is_symlink() or not full.is_file():
                continue
            yield rel.as_posix(), _excluded(rel.parts)


def _read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\x00" in data[:BINARY_PROBE]:
        return None
    return data.decode("utf-8", errors="replace")


def owner_emails_from_license(tree: Path) -> set[str]:
    license_file = tree / "LICENSE"
    if not license_file.is_file():
        return set()
    return {m.lower() for m in EMAIL.findall(license_file.read_text(encoding="utf-8"))}


def _scan_text(rel: str, text: str, emails: Iterable[str]) -> Iterator[Finding]:
    # A whole-file search first, so line numbers are only worked out for files with a hit.
    markers = [
        (marker, pattern)
        for marker, pattern in CONTENT_MARKERS
        if not _allowed(rel, marker) and pattern.search(text)
    ]
    lowered_emails = [] if _allowed(rel, OWNER_EMAIL) else sorted({e.lower() for e in emails if e})
    lowered_text = text.lower()
    lowered_emails = [email for email in lowered_emails if email in lowered_text]
    if not markers and not lowered_emails:
        return
    for number, line in enumerate(text.splitlines(), start=1):
        for marker, pattern in markers:
            if pattern.search(line):
                yield Finding(rel, number, marker)
        if lowered_emails:
            lowered = line.lower()
            if any(email in lowered for email in lowered_emails):
                yield Finding(rel, number, OWNER_EMAIL)


def scan_tree(
    tree: Path, owner_emails: Iterable[str] = (), paths: Iterable[str] | None = None
) -> list[Finding]:
    """Scan ``tree`` (or only the listed relative ``paths``) and return the sorted findings."""
    tree = Path(tree)
    emails = set(owner_emails) | owner_emails_from_license(tree)
    if paths is None:
        entries: Iterable[tuple[str, bool]] = _walk(tree)
    else:
        entries = ((p, _excluded(Path(p).parts)) for p in paths)
    found: list[Finding] = []
    for rel, excluded in entries:
        if excluded:
            found.append(Finding(rel, 0, EXCLUDED_PATH))
            continue
        full = tree / rel
        if full.is_symlink() or not full.is_file():
            continue
        text = _read_text(full)
        if text is not None:
            found.extend(_scan_text(rel, text, emails))
    return sorted(found)


def run_gitleaks(gitleaks: str, tree: Path) -> int:
    config = tree / ".gitleaks.toml"
    if not config.is_file():
        print(f"gitleaks: {config.name} is missing from the export tree", file=sys.stderr)
        return 1
    command = [gitleaks, "git", "--config", str(config), "--redact", "--no-banner"]
    command += ["--exit-code", "1", str(tree)]
    # The binary path comes from the operator's own export script, never from the scanned tree.
    return subprocess.run(command, check=False).returncode  # noqa: S603


def _remove_block(lines: list[str], name: str) -> None:
    starts = [i for i, line in enumerate(lines) if line.startswith(f"<!-- GSD:{name}-start")]
    ends = [i for i, line in enumerate(lines) if line.strip() == f"<!-- GSD:{name}-end -->"]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise StripError(f"expected exactly one GSD:{name}-start and GSD:{name}-end marker")
    start = starts[0]
    del lines[start : ends[0] + 1]
    if start < len(lines):
        if not lines[start].strip():
            del lines[start]
    elif start > 0 and not lines[start - 1].strip():
        del lines[start - 1]


def strip_claude_md(text: str) -> str:
    """Return ``text`` without the GSD workflow and profile blocks; everything else is kept."""
    lines = text.splitlines(keepends=True)
    for name in ("workflow", "profile"):
        _remove_block(lines, name)
    return "".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="scan an export tree for private markers")
    scan.add_argument("tree", type=Path)
    scan.add_argument("--owner-email", action="append", default=[], metavar="ADDR")
    scan.add_argument("--gitleaks", metavar="PATH", help="also run this gitleaks binary")
    strip = commands.add_parser("strip-claude-md", help="remove the GSD blocks from CLAUDE.md")
    strip.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    if args.command == "strip-claude-md":
        target: Path = args.file
        try:
            stripped = strip_claude_md(target.read_text(encoding="utf-8"))
        except (OSError, StripError) as exc:
            print(f"strip-claude-md: {target}: {exc}", file=sys.stderr)
            return 1
        target.write_text(stripped, encoding="utf-8")
        return 0

    tree: Path = args.tree
    if not tree.is_dir():
        print(f"scan: {tree} is not a directory", file=sys.stderr)
        return 1
    found = scan_tree(tree, args.owner_email)
    for finding in found:
        print(finding)
    status = 1 if found else 0
    if args.gitleaks and run_gitleaks(args.gitleaks, tree) != 0:
        print("gitleaks reported findings (or failed to run)", file=sys.stderr)
        status = 1
    print(f"leak check: {len(found)} marker finding(s)", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
