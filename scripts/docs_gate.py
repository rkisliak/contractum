#!/usr/bin/env python3
"""The docs gate: docs are part of every change, and no reader doc claims that no release exists.

Python standard library only, so the git hooks run it with a plain ``python3`` and no ``uv sync``.
Three checks:

(a) **stale release claims**: once a ``vX.Y.Z`` tag exists (a release candidate counts), no reader
    doc may contain a phrase from the ``tokens:`` list of
    ``.vale/styles/Contractum/StaleRelease.yml``. That file is the one place the phrase list lives;
    Vale reads it too. Fenced code blocks, inline code and HTML comments are ignored, and each
    paragraph is joined into one line so a phrase broken by a hard wrap still matches.
(b) **docs impact**: a change to a documented surface (:data:`SURFACES`) must change a reader doc
    or ``mkdocs.yml`` too, or carry the trailer ``Docs-Impact: none — <reason>`` in the last
    paragraph of a commit message (or of the pull request description in CI).
(c) **Vale** on the changed reader docs: an ``error`` alert anywhere in a changed file blocks;
    ``warning`` alerts are printed only for the lines the change added or modified, never blocking.

Modes::

    python3 scripts/docs_gate.py --staged                 # pre-commit: (a) + (c) on staged docs
    python3 scripts/docs_gate.py --commit-msg FILE        # commit-msg: (b) on the index + message
    python3 scripts/docs_gate.py --range BASE HEAD [--pr-body-file F]  # CI: (a) all, (b), (c)
    python3 scripts/docs_gate.py --all                    # release pass: (a) + (c) on every doc
    python3 scripts/docs_gate.py --stale-only             # (a) only, on every doc
    python3 scripts/docs_gate.py --sync                   # download the pinned Vale packages

Exit 0 on pass, 1 on a finding, 2 on a setup problem (no git, no Vale, packages not synced). A setup
problem never passes silently: it prints how to fix it. How to pass honestly: section 10 of
``.claude/skills/keep-docs-current/SKILL.md`` and "The docs gate" in ``docs/dev/ci-and-github.md``.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_SETUP = 2

# Vale: the release binary on PATH at exactly this version, or the official image pinned by its
# index digest. Upgrade the version, VALE_SHA256 in .github/workflows/docs.yml and this image
# together (see "Upgrading the pins" in docs/dev/ci-and-github.md).
VALE_VERSION = "3.28.1"
VALE_IMAGE = (
    "jdkato/vale:v3.28.1@sha256:42b74f7f3ea7dd60554181037a8899a190516b9d48b67d157b09e5df791f9115"
)
VALE_INI = ".vale.ini"
STYLES_DIR = ".vale/styles"
STALE_RULE = ".vale/styles/Contractum/StaleRelease.yml"
# Written next to each synced package; a changed pin in .vale.ini then asks for a new --sync.
SYNC_STAMP = ".docs-gate-sha256"
PACKAGE_HOSTS = frozenset({"github.com"})
DOWNLOAD_TIMEOUT_S = 60

RELEASE_TAG_GLOB = "v[0-9]*.[0-9]*.[0-9]*"
RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

HOW_TO_PASS = {
    "stale-claim": (
        "Stale claim: rewrite the sentence to say what is true now (a release exists). Never "
        "remove a phrase from .vale/styles/Contractum/StaleRelease.yml to pass."
    ),
    "docs-impact": (
        "Docs impact: update the pages named above. Only if no reader can notice the change, end "
        "the commit message (in a pull request: its description) with a last paragraph\n"
        "      Docs-Impact: none — <a real reason, at least 10 characters>"
    ),
    "vale": (
        "Vale error: fix the text. A correct term that Vale flags goes into "
        ".vale/styles/config/vocabularies/Contractum/accept.txt; never switch a rule off."
    ),
}
DESCRIPTION = "The docs gate: stale release claims, docs impact and Vale on changed docs."
DETAILS = (
    "Details: 'The docs gate' in docs/dev/ci-and-github.md and section 10 of "
    ".claude/skills/keep-docs-current/SKILL.md. Never bypass the hooks with --no-verify."
)


class SetupError(Exception):
    """The gate cannot run (no git, no Vale, packages not synced); the message says the fix."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    check: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{where}: [{self.check}] {self.message}"


# --------------------------------------------------------------------------------------------
# git


def git(*args: str, stdin: str | None = None, check: bool = True) -> str:
    """Run git in the repository root and return its stdout."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv
            ["git", *args],  # noqa: S607 - git from PATH, like every hook
            cwd=ROOT,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise SetupError("git is not installed or not on PATH; install git and retry.") from exc
    if check and proc.returncode != 0:
        raise SetupError(f"`git {' '.join(args)}` failed: {proc.stderr.strip()}")
    return proc.stdout


def _split_z(output: str) -> list[str]:
    return [item for item in output.split("\0") if item]


def is_commit(ref: str) -> bool:
    if not ref or set(ref) == {"0"}:
        return False
    return bool(git("rev-parse", "--quiet", "--verify", f"{ref}^{{commit}}", check=False).strip())


# --------------------------------------------------------------------------------------------
# Reader docs

_NOT_READER_DIRS = frozenset({".claude", ".planning", "node_modules"})


def is_reader_doc(path: str) -> bool:
    """True for a page readers see: ``docs/**/*.md`` and every ``README.md``.

    Agent notes and skills (``.claude/``, any ``CLAUDE.md``) and ``.planning/`` are not reader docs:
    the skill quotes the banned phrases on purpose.
    """
    parts = path.split("/")
    if _NOT_READER_DIRS.intersection(parts[:-1]) or parts[-1] == "CLAUDE.md":
        return False
    if parts[0] == "docs" and len(parts) > 1 and parts[-1].endswith(".md"):
        return True
    return parts[-1] == "README.md"


def counts_as_docs(path: str) -> bool:
    """A changed path that satisfies the docs-impact check."""
    return is_reader_doc(path) or path == "mkdocs.yml"


def tracked_reader_docs() -> list[str]:
    return sorted(path for path in _split_z(git("ls-files", "-z")) if is_reader_doc(path))


# --------------------------------------------------------------------------------------------
# (a) Stale release claims


def release_tags() -> list[str]:
    return [tag for tag in git("tag", "--list", RELEASE_TAG_GLOB).split() if RELEASE_TAG.match(tag)]


def newest_tag(tags: Iterable[str]) -> str:
    """The highest version; a stable tag sorts above its own release candidates."""

    def key(tag: str) -> tuple[int, int, int, int, tuple[int | str, ...]]:
        match = RELEASE_TAG.match(tag)
        if match is None:
            raise ValueError(f"not a release tag: {tag!r}")
        major, minor, patch, pre = match.groups()
        pre_key: tuple[int | str, ...] = tuple(
            int(part) if part.isdigit() else part for part in (pre or "").split(".") if part
        )
        return int(major), int(minor), int(patch), 0 if pre else 1, pre_key

    return max(tags, key=key)


def _unquote_yaml_scalar(raw: str, source: str) -> str:
    value = raw.strip()
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise SetupError(f"{source}: unterminated single-quoted token {value!r}")
        return value[1:-1].replace("''", "'")
    if value.startswith('"'):
        raise SetupError(
            f"{source}: write tokens in single quotes (a double-quoted YAML string treats "
            f"backslashes as escapes): {value!r}"
        )
    return re.sub(r"\s+#.*$", "", value)


def load_stale_patterns(rule_file: Path | None = None) -> list[re.Pattern[str]]:
    """Compile the ``tokens:`` list of the StaleRelease Vale rule (a tiny line parser, no YAML lib).

    The tokens use only ``\\b``, ``\\w``, ``(?:...)``, classes and alternation, which mean the same
    in Python ``re`` and in Vale's Go regular expressions.
    """
    path = rule_file or ROOT / STALE_RULE
    source = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise SetupError(f"{source} is missing: it holds the stale-claim phrase list.") from exc
    tokens: list[str] = []
    in_tokens = False
    for raw in lines:
        if not in_tokens:
            in_tokens = raw.rstrip() == "tokens:"
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        item = re.match(r"^\s+-\s+(.*)$", raw)
        if item is None:
            break  # the next key ends the list
        tokens.append(_unquote_yaml_scalar(item.group(1), source))
    if not tokens:
        raise SetupError(f"{source} has no `tokens:` list; the stale-claim check cannot run.")
    patterns: list[re.Pattern[str]] = []
    for token in tokens:
        try:
            patterns.append(re.compile(token, re.IGNORECASE))
        except re.error as exc:
            raise SetupError(f"{source}: token {token!r} is not a valid regex: {exc}") from exc
    return patterns


_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_QUOTE_PREFIX = re.compile(r"^\s*(?:>\s?)+")
_BLOCK_START = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|\|)")
_CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)")
_HTML_COMMENT = re.compile(r"<!--.*?-->")


def prose_segments(text: str) -> list[tuple[str, list[int], list[int]]]:
    """Paragraphs as one line each: ``(joined_text, line_numbers, start_offsets)``.

    Fenced code blocks are dropped. A paragraph ends at a blank line, and a list item, heading or
    table row starts a new one. Leading blockquote markers are removed. Lines are stripped and
    joined with one space, so a phrase broken by a hard wrap matches as one line.
    """
    segments: list[tuple[str, list[int], list[int]]] = []
    lines: list[tuple[int, str]] = []

    def flush() -> None:
        if lines:
            numbers = [number for number, _ in lines]
            offsets: list[int] = []
            parts: list[str] = []
            position = 0
            for _, part in lines:
                offsets.append(position)
                parts.append(part)
                position += len(part) + 1
            segments.append((" ".join(parts), numbers, offsets))
            lines.clear()

    fence: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        opener = _FENCE.match(raw)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = None
            continue
        if opener:
            flush()
            fence = opener.group(1)
            continue
        content = _QUOTE_PREFIX.sub("", raw).strip()
        if not content:
            flush()
            continue
        if _BLOCK_START.match(content):
            flush()
        lines.append((number, content))
    flush()
    return segments


def find_phrases(text: str, patterns: Sequence[re.Pattern[str]]) -> list[tuple[int, str]]:
    """``(line, phrase)`` for every pattern hit in the prose of a Markdown text."""
    hits: list[tuple[int, str]] = []
    for joined, numbers, offsets in prose_segments(text):

        def blank(match: re.Match[str]) -> str:
            return " " * len(match.group(0))

        prose = _HTML_COMMENT.sub(blank, _CODE_SPAN.sub(blank, joined))
        for pattern in patterns:
            for match in pattern.finditer(prose):
                line = numbers[bisect.bisect_right(offsets, match.start()) - 1]
                hit = (line, match.group(0))
                if hit not in hits:
                    hits.append(hit)
    return sorted(hits)


def check_stale(paths: Iterable[str], read: Callable[[str], str]) -> list[Finding]:
    tags = release_tags()
    if not tags:
        return []  # no release yet: "no release has been published" is then true
    newest = newest_tag(tags)
    patterns = load_stale_patterns()
    findings: list[Finding] = []
    for path in paths:
        for line, phrase in find_phrases(read(path), patterns):
            findings.append(
                Finding(path, line, "stale-claim", f"{phrase!r} but tag {newest} exists")
            )
    return findings


def read_worktree(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8", errors="replace")


def read_index(path: str) -> str:
    return git("show", f":{path}")


# --------------------------------------------------------------------------------------------
# (b) Docs impact


@dataclass(frozen=True)
class Surface:
    """A part of the code that readers see; a change to it needs docs or a trailer."""

    name: str
    globs: tuple[str, ...]
    pages: tuple[str, ...]
    exclude: tuple[str, ...] = ()


SURFACES: tuple[Surface, ...] = (
    Surface(
        "Identity API",
        ("identity/src/dc_identity/api/**", "identity/src/dc_identity/internal_api.py"),
        ("HTTP reference", "docs/user/api-guide.md", "docs/admin/first-run.md"),
    ),
    Surface(
        "Contracts API",
        ("contracts/src/dc_contracts/api/**",),
        ("HTTP reference", "docs/user/api-guide.md", "docs/user/subscriptions.md"),
    ),
    Surface(
        "Validation API",
        ("validation/src/dc_validation/api/**",),
        ("HTTP reference", "docs/user/writing-rules.md"),
    ),
    Surface(
        "Audit API",
        ("audit/src/dc_audit/api.py", "audit/src/dc_audit/internal_api.py"),
        ("HTTP reference", "docs/user/api-guide.md"),
    ),
    Surface(
        "Mail service",
        ("mail/src/dc_mail/main.py", "mail/src/dc_mail/smtp.py"),
        ("docs/admin/settings.md", "docs/user/subscriptions.md"),
    ),
    Surface(
        "Settings",
        (
            "*/src/*/settings.py",
            "shared/dc-core/src/dc_core/settings.py",
            "shared/dc-core/src/dc_core/platform_settings.py",
            "identity/src/dc_identity/platform_settings.py",
            "contracts/src/dc_contracts/runtime_settings.py",
        ),
        ("docs/admin/settings.md",),
    ),
    Surface(
        "Roles and permissions",
        ("shared/dc-auth/src/dc_auth/roles.py",),
        ("docs/admin/first-run.md", "docs/user/api-guide.md"),
    ),
    Surface(
        "Problems",
        ("shared/dc-core/src/dc_core/problems.py",),
        ("docs/user/api-guide.md", "docs/admin/troubleshooting.md"),
    ),
    Surface(
        "SDK public API",
        (
            "clients/python/src/contractum/*.py",
            "clients/python/src/contractum/{auth,engine,report,sources}/**",
        ),
        ("SDK reference", "docs/user/sdk-cli.md", "clients/python/README.md"),
        # Private modules (_transport.py) are not public API; dunder modules (__init__.py) are.
        exclude=("clients/python/src/contractum/_[!_]*.py",),
    ),
    Surface(
        "CLI",
        (
            "clients/python/src/contractum/cli/**",
            "clients/python/src/contractum/__main__.py",
            "clients/python/src/contractum/pytest_plugin.py",
        ),
        ("CLI reference", "docs/user/sdk-cli.md"),
    ),
    Surface("UI", ("ui/src/features/**", "ui/src/routes/**"), ("docs/user/ui-guide.md",)),
    Surface(
        "Helm",
        (
            "deploy/helm/contractum/values.yaml",
            "deploy/helm/contractum/templates/**",
            "deploy/helm/contractum/Chart.yaml",
        ),
        (
            "docs/admin/deploy-kubernetes.md",
            "docs/admin/upgrade-backup.md",
            "deploy/helm/contractum/README.md",
        ),
    ),
    Surface(
        "Release",
        (".github/workflows/release.yml", "scripts/release/**"),
        ("docs/dev/ci-and-github.md", "the install pages"),
    ),
    Surface(
        "Local stack",
        ("compose.yaml", "run.sh", "run.ps1"),
        ("docs/dev/local-stack.md", "README.md"),
    ),
    Surface(
        "Gateway",
        ("gateway/traefik.yml", "gateway/dynamic/**"),
        ("docs/admin/deploy-kubernetes.md", "docs/dev/architecture/"),
    ),
    Surface(
        "The docs gate",
        ("scripts/docs_gate.py", ".githooks/**", ".vale.ini", ".vale/styles/Contractum/**"),
        (".claude/skills/keep-docs-current/", "docs/dev/ci-and-github.md"),
    ),
)

# Never a surface: tests and caches next to the code.
NOT_SURFACE: tuple[str, ...] = (
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/tests/**",
    "**/__pycache__/**",
    "**/conftest.py",
)

# A surface file whose changed lines are all version pins (or comments) is not reader-visible:
# Dependabot cannot write trailers. A major upgrade of a bundled image passes this check; the
# release docs pass (upgrade notes) is the control for that.
_COMMENT_OR_BLANK = re.compile(r"^\s*(?:#.*)?$")
_USES_PIN = re.compile(r"^\s*-?\s*uses:\s+\S+@[0-9a-f]{40}(\s+#.*)?$")
_IMAGE_PIN = re.compile(r"^\s*(digest|tag|image):\s")
PIN_ONLY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (".github/workflows/*.yml", _USES_PIN),
    ("deploy/helm/contractum/values.yaml", _IMAGE_PIN),
    ("compose.yaml", _IMAGE_PIN),
)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """A path glob as a regex: ``**`` spans directories, ``*`` and ``?`` stay inside one,
    ``{a,b}`` is alternation and ``[!x]`` a negated class."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "{":
            end = pattern.index("}", index)
            options = pattern[index + 1 : end].split(",")
            out.append("(?:" + "|".join(re.escape(option) for option in options) + ")")
            index = end
        elif char == "[":
            end = pattern.index("]", index)
            body = pattern[index + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            index = end
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("".join(out))


def path_matches(path: str, globs: Iterable[str]) -> bool:
    return any(glob_to_regex(pattern).fullmatch(path) for pattern in globs)


def surfaces_for(path: str) -> list[Surface]:
    """The surfaces a changed path belongs to (none for a test file)."""
    if path_matches(path, NOT_SURFACE):
        return []
    return [
        surface
        for surface in SURFACES
        if path_matches(path, surface.globs) and not path_matches(path, surface.exclude)
    ]


def changed_lines_of_diff(diff: str) -> list[str]:
    """The added and removed lines of a ``git diff`` (without the leading ``+``/``-``)."""
    return [
        line[1:]
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++ ", "--- "))
    ]


def is_pin_only(path: str, diff: str) -> bool:
    """True when every changed line of ``path`` is a version pin or a comment."""
    rule = next((rx for pattern, rx in PIN_ONLY_RULES if path_matches(path, (pattern,))), None)
    if rule is None:
        return False
    return all(
        _COMMENT_OR_BLANK.match(line) or rule.match(line) for line in changed_lines_of_diff(diff)
    )


TRAILER_KEY = "docs-impact"
_NONE_VALUE = re.compile(r"^none\s*(?:—|–|--|-)\s*(?P<reason>.*)$", re.IGNORECASE | re.DOTALL)
MIN_REASON_CHARS = 10
# "none" followed by one of these means the docs are due, which is not "no docs impact".
DEFERRED_REASONS = frozenset(
    {
        "n/a",
        "na",
        "none",
        "tbd",
        "todo",
        "wip",
        "later",
        "follow-up",
        "followup",
        "docs later",
        "will add docs",
    }
)
_DEFERRAL = re.compile(
    r"^(?:docs?\s+)?(?:(?:in|with|via)\s+(?:a\s+)?)?(?:later|follow[- ]?up|tbd|todo|wip)\b"
    r"|^will\s+(?:add|write|update|do)\b",
    re.IGNORECASE,
)
_SCISSORS = re.compile(r"^#\s*-+\s*>8\s*-+\s*$")


def clean_message(message: str, strip_comments: bool = True) -> str:
    """The message as git would store it: CRLF normalised; for a commit message being written, the
    scissors part and ``#`` comment lines dropped (a pull request body keeps its ``#`` headings)."""
    kept: list[str] = []
    for line in message.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if strip_comments and _SCISSORS.match(line):
            break
        if not (strip_comments and line.startswith("#")):
            kept.append(line)
    return "\n".join(kept).strip() + "\n"


def parse_trailers(message: str) -> list[tuple[str, str]]:
    """Trailers of the message's last paragraph, parsed by ``git interpret-trailers``."""
    output = git("interpret-trailers", "--parse", stdin=clean_message(message, False))
    pairs: list[tuple[str, str]] = []
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            pairs.append((key.strip(), value.strip()))
    return pairs


def judge_trailer_value(value: str) -> str | None:
    """None when ``value`` is an acceptable ``none — <reason>``, else why it is refused."""
    match = _NONE_VALUE.match(value.strip())
    if match is None:
        return f"'Docs-Impact: {value}' is not of the form 'Docs-Impact: none — <reason>'"
    reason = " ".join(match.group("reason").split())
    normalised = reason.lower().rstrip(".!")
    if normalised in DEFERRED_REASONS or _DEFERRAL.match(normalised):
        return f"'Docs-Impact: {value}' means the docs are due: put them in this change"
    if len(reason) < MIN_REASON_CHARS:
        return (
            f"'Docs-Impact: {value}' needs a real reason (at least {MIN_REASON_CHARS} characters)"
        )
    return None


def trailer_verdict(messages: Iterable[str]) -> tuple[bool, list[str]]:
    """``(accepted, refusals)``: accepted when any message carries a valid trailer."""
    refusals: list[str] = []
    for message in messages:
        for key, value in parse_trailers(message):
            if key.lower() != TRAILER_KEY:
                continue
            refusal = judge_trailer_value(value)
            if refusal is None:
                return True, []
            refusals.append(refusal)
    return False, refusals


def check_impact(
    paths: Sequence[str], messages: Iterable[str], diff_for: Callable[[str], str]
) -> list[Finding]:
    hits: dict[str, list[Surface]] = {}
    for path in paths:
        surfaces = surfaces_for(path)
        if surfaces and not is_pin_only(path, diff_for(path)):
            hits[path] = surfaces
    if not hits or any(counts_as_docs(path) for path in paths):
        return []
    accepted, refusals = trailer_verdict(messages)
    if accepted:
        return []
    findings = [
        Finding(
            path,
            None,
            "docs-impact",
            f"{' and '.join(s.name for s in surfaces)} changed without a docs change; update: "
            + ", ".join(dict.fromkeys(page for s in surfaces for page in s.pages)),
        )
        for path, surfaces in hits.items()
    ]
    findings.extend(Finding("trailer", None, "docs-impact", refusal) for refusal in refusals)
    return findings


# --------------------------------------------------------------------------------------------
# (c) Vale


def vale_packages(ini: Path | None = None) -> list[tuple[str, str, str]]:
    """``(name, url, sha256)`` for each entry of ``Packages =`` in ``.vale.ini``."""
    path = ini or ROOT / VALE_INI
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SetupError(f"{VALE_INI} is missing.") from exc
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append(buffer + line)
        buffer = ""
    packages: list[tuple[str, str, str]] = []
    for line in logical:
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "Packages":
            continue
        for entry in (item.strip() for item in value.split(",")):
            if not entry:
                continue
            url, _, fragment = entry.partition("#")
            digest = fragment.removeprefix("sha256:")
            if not fragment.startswith("sha256:") or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise SetupError(f"{VALE_INI}: package {url} is not pinned with #sha256:<digest>.")
            name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1].removesuffix(".zip")
            packages.append((name, url, digest))
    return packages


def _download(url: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in PACKAGE_HOSTS:
        raise SetupError(f"{VALE_INI}: refusing to download {url} (https from GitHub only).")
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response:  # noqa: S310 - https GitHub URL checked above
            data: bytes = response.read()
    except OSError as exc:
        raise SetupError(f"could not download {url}: {exc}") from exc
    return data


def sync_packages() -> list[str]:
    """Download each pinned package, check its SHA-256 and unpack it into ``.vale/styles``.

    Vale 3.28 ignores the ``#sha256:`` pin, so the gate checks it itself. Returns the names synced.
    """
    styles = ROOT / STYLES_DIR
    synced: list[str] = []
    for name, url, digest in vale_packages():
        data = _download(url)
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise SetupError(f"{url}: SHA-256 {actual} does not match the pin {digest}.")
        with tempfile.TemporaryDirectory(dir=styles) as scratch:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    parts = Path(info.filename).parts
                    if (
                        info.filename.startswith(("/", "\\"))
                        or ".." in parts
                        or parts[:1] != (name,)
                    ):
                        raise SetupError(
                            f"{url}: unexpected path {info.filename!r} in the archive."
                        )
                    destination = Path(scratch).joinpath(*parts)
                    if info.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(info))
            target = styles / name
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(Path(scratch) / name), target)
        (target / SYNC_STAMP).write_text(digest + "\n", encoding="utf-8")
        synced.append(name)
    return synced


def check_packages_synced() -> None:
    for name, _, digest in vale_packages():
        stamp = ROOT / STYLES_DIR / name / SYNC_STAMP
        try:
            current = stamp.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            current = ""
        if current != digest:
            raise SetupError(
                f"the Vale package {name} is not synced at its pinned digest. "
                "Run: python3 scripts/docs_gate.py --sync"
            )


def vale_command() -> list[str]:
    """The Vale binary at :data:`VALE_VERSION` on PATH, else the pinned Docker image."""
    found = ""
    exe = shutil.which("vale")
    if exe:
        proc = subprocess.run(  # noqa: S603 - fixed argv
            [exe, "--version"], capture_output=True, text=True, check=False
        )
        found = proc.stdout.strip()
        if re.search(rf"\b{re.escape(VALE_VERSION)}\b", found):
            return [exe]
    docker = shutil.which("docker")
    if docker:
        info = subprocess.run(  # noqa: S603 - fixed argv
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if info.returncode == 0:
            return [docker, "run", "--rm", "-v", f"{ROOT}:/repo:ro", "-w", "/repo", VALE_IMAGE]
    wrong = f" (found `{found}` on PATH)" if found else ""
    raise SetupError(
        f"Vale {VALE_VERSION} is needed{wrong}. Either install the release binary "
        f"(https://github.com/vale-cli/vale/releases/tag/v{VALE_VERSION}, check it against "
        f"vale_{VALE_VERSION}_checksums.txt) as `vale` on PATH, or start Docker so the gate can "
        f"run {VALE_IMAGE}. Then run: python3 scripts/docs_gate.py --sync"
    )


def run_vale(paths: Sequence[str]) -> dict[str, list[dict[str, object]]]:
    """Vale's JSON alerts for ``paths`` (repository-relative), keyed by path."""
    if not paths:
        return {}
    check_packages_synced()
    command = [
        *vale_command(),
        "--no-global",
        f"--config={VALE_INI}",
        "--output=JSON",
        "--minAlertLevel=warning",
        *paths,
    ]
    proc = subprocess.run(  # noqa: S603 - fixed argv, paths come from git
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )
    try:
        alerts = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        alerts = None
    if proc.returncode not in (0, 1) or not isinstance(alerts, dict):
        raise SetupError(
            f"Vale failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return {Path(path).as_posix().removeprefix("/repo/"): value for path, value in alerts.items()}


def added_lines(diff: str) -> set[int]:
    """New-file line numbers that a ``git diff -U0`` adds or modifies."""
    lines: set[int] = set()
    for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE):
        start, count = int(match.group(1)), int(match.group(2) or "1")
        lines.update(range(start, start + count))
    return lines


def check_vale(
    paths: Sequence[str], touched: Callable[[str], set[int]] | None
) -> tuple[list[Finding], list[Finding], int]:
    """``(errors, warnings on touched lines, all warnings)``; ``touched=None`` shows none."""
    errors: list[Finding] = []
    warnings: list[Finding] = []
    total_warnings = 0
    for path, alerts in sorted(run_vale(paths).items()):
        lines = touched(path) if touched is not None else set()
        for alert in alerts:
            line = alert.get("Line")
            finding = Finding(
                path,
                line if isinstance(line, int) else None,
                f"vale {alert.get('Check')}",
                str(alert.get("Message")),
            )
            if alert.get("Severity") == "error":
                errors.append(finding)
            else:
                total_warnings += 1
                if finding.line in lines:
                    warnings.append(finding)
    return errors, warnings, total_warnings


# --------------------------------------------------------------------------------------------
# Modes


def mode_staged() -> list[Finding]:
    staged = _split_z(
        git("diff", "--cached", "--name-only", "--no-renames", "--diff-filter=ACMR", "-z")
    )
    docs = [path for path in staged if is_reader_doc(path)]
    if not docs:
        return []
    findings = check_stale(docs, read_index)
    errors, warnings, _ = check_vale(
        docs, lambda path: added_lines(git("diff", "--cached", "-U0", "--", path))
    )
    _print_warnings(warnings)
    return findings + errors


def _skip_commit_msg(message: str) -> bool:
    """Merge, fixup and squash commits are checked by the pull request run instead."""
    if git("rev-parse", "--quiet", "--verify", "MERGE_HEAD", check=False).strip():
        return True
    subject = next((line for line in message.splitlines() if line.strip()), "")
    return subject.startswith(("fixup!", "squash!", "amend!"))


def mode_commit_msg(message_file: str) -> list[Finding]:
    message = clean_message(Path(message_file).read_text(encoding="utf-8", errors="replace"))
    if _skip_commit_msg(message):
        return []
    paths = _split_z(git("diff", "--cached", "--name-only", "--no-renames", "-z"))
    return check_impact(paths, [message], lambda path: git("diff", "--cached", "-U0", "--", path))


def resolve_range(base: str, head: str) -> tuple[str, str]:
    """``(base, head)`` as usable refs. A missing or unknown base (the null SHA of a first push, a
    force push, a manual run) falls back to the parent of ``head``, or the empty tree."""
    if not is_commit(head):
        raise SetupError(f"head {head!r} is not a commit in this clone (fetch-depth: 0?).")
    if is_commit(base):
        return base, head
    parent = f"{head}~1"
    fallback = parent if is_commit(parent) else EMPTY_TREE
    print(f"docs gate: base {base!r} is unknown here; checking {fallback}..{head}.")
    return fallback, head


def _range_spec(base: str, head: str) -> list[str]:
    if base != EMPTY_TREE and git("merge-base", base, head, check=False).strip():
        return [f"{base}...{head}"]
    return [base, head]


def mode_range(base: str, head: str, pr_body_file: str | None) -> list[Finding]:
    base, head = resolve_range(base, head)
    spec = _range_spec(base, head)
    paths = _split_z(git("diff", "--name-only", "--no-renames", "-z", *spec))
    if base == EMPTY_TREE:
        messages = [git("log", "-1", "--format=%B", head)]
    else:
        messages = _split_z(git("log", "--format=%B%x00", f"{base}..{head}"))
    if pr_body_file:
        # A title line first: git never reads the first paragraph of a message as trailers.
        body = Path(pr_body_file).read_text(encoding="utf-8", errors="replace")
        messages.append(f"Pull request description\n\n{body}")

    def diff_for(path: str) -> str:
        return git("diff", "-U0", *spec, "--", path)

    findings = check_stale(tracked_reader_docs(), read_worktree)
    findings += check_impact(paths, messages, diff_for)
    changed = _split_z(git("diff", "--name-only", "--no-renames", "--diff-filter=AMR", "-z", *spec))
    docs = [path for path in changed if is_reader_doc(path) and (ROOT / path).is_file()]
    errors, warnings, _ = check_vale(docs, lambda path: added_lines(diff_for(path)))
    _print_warnings(warnings)
    return findings + errors


def mode_all() -> list[Finding]:
    docs = tracked_reader_docs()
    findings = check_stale(docs, read_worktree)
    errors, _, total = check_vale(docs, None)
    if total:
        print(
            f"docs gate: {total} Vale warnings in the docs (not blocking; fix them in the lines "
            "you touch). See them with: vale --no-global <file>"
        )
    return findings + errors


def mode_stale_only() -> list[Finding]:
    return check_stale(tracked_reader_docs(), read_worktree)


def _print_warnings(warnings: Sequence[Finding]) -> None:
    if warnings:
        print("Vale warnings on the lines this change touches (not blocking, please fix):")
        for warning in warnings:
            print(f"  {warning}")


def report(findings: Sequence[Finding]) -> int:
    if not findings:
        return EXIT_OK
    print("Docs gate failed:")
    for finding in findings:
        print(f"  {finding}")
    print("How to pass:")
    checks = {finding.check.split(" ", 1)[0] for finding in findings}
    for check, advice in HOW_TO_PASS.items():
        if check in checks:
            print(f"  - {advice}")
    print(f"  {DETAILS}")
    return EXIT_FINDING


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--staged", action="store_true", help="pre-commit: the staged docs")
    modes.add_argument("--commit-msg", metavar="FILE", help="commit-msg: docs impact")
    modes.add_argument("--range", nargs=2, metavar=("BASE", "HEAD"), help="CI: a change range")
    modes.add_argument("--all", action="store_true", help="release pass: every reader doc")
    modes.add_argument("--stale-only", action="store_true", help="stale claims only")
    modes.add_argument("--sync", action="store_true", help="download the pinned Vale packages")
    parser.add_argument("--pr-body-file", metavar="FILE", help="with --range: the PR description")
    args = parser.parse_args(argv)
    if args.pr_body_file and not args.range:
        parser.error("--pr-body-file needs --range")
    try:
        if args.sync:
            print(f"docs gate: synced {', '.join(sync_packages())} into {STYLES_DIR}")
            return EXIT_OK
        if args.staged:
            findings = mode_staged()
        elif args.commit_msg:
            findings = mode_commit_msg(args.commit_msg)
        elif args.range:
            findings = mode_range(args.range[0], args.range[1], args.pr_body_file)
        elif args.all:
            findings = mode_all()
        else:
            findings = mode_stale_only()
    except SetupError as exc:
        print(f"docs gate cannot run: {exc}", file=sys.stderr)
        return EXIT_SETUP
    return report(findings)


if __name__ == "__main__":
    sys.exit(main())
