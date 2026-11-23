#!/usr/bin/env python3
"""The next platform version, from Conventional Commits and the release tags (release.yml).

    python3 scripts/release/next_version.py --mode pre-release [--head REV] [--github-output FILE]
    python3 scripts/release/next_version.py --mode promote [--github-output FILE]

One version covers the whole platform (every image, the Helm chart and the Python SDK and CLI). It
is never written into a file: the Release workflow computes it here and injects it at build and
package time. The SDK package takes its PEP 440 form (``python-version``: ``1.2.0rc1`` for
``1.2.0-rc.1``).

pre-release
    Reads the commits since the last stable tag ``vX.Y.Z`` and bumps it: a breaking change (``!``
    after the type or scope, or a ``BREAKING CHANGE:`` footer) is major, ``feat`` is minor, and
    ``fix``, ``perf``, ``security``, ``refactor``, ``revert`` and dependency updates
    (``build(deps)``, ``chore(deps)``) are patch. Other types (``docs``, ``test``, ``ci``,
    ``chore``, ``style``, ``build``) and subjects that are not Conventional Commits release
    nothing. With no stable tag yet, the first release is 1.0.0. The result is the release
    candidate ``vX.Y.Z-rc.N``, N one above the highest existing candidate of that version.
promote
    Takes the newest release candidate ``vX.Y.Z-rc.N`` and releases the same commit as
    ``vX.Y.Z``. It must be newer than the last stable tag.

It prints ``key=value`` lines (version, python-version, tag, sha, previous, prerelease, candidate,
image-version), also appended to ``--github-output``. ``candidate`` is the release candidate tag a
promote releases (empty for a pre-release) and ``image-version`` is the version the images were
built under: that candidate's version when promoting, the version itself otherwise. Promote adds
its tags to those tested images instead of building new ones. When there is nothing to do it
explains why and exits 1. Standard library only, so the workflow runs it with the runner's own
Python.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?$")
SUBJECT = re.compile(
    r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()\r\n]+)\))?(?P<bang>!)?:[ \t]+(?P<description>\S.*)$"
)
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:[ \t]", re.MULTILINE)
PULL_REQUEST = re.compile(r"\s*\(#(?P<number>\d+)\)$")

PATCH_TYPES = frozenset({"fix", "perf", "security", "refactor", "revert"})
DEPENDENCY_TYPES = frozenset({"build", "chore"})
DEPENDENCY_SCOPE = "deps"


class ReleaseStopError(Exception):
    """There is nothing to release; the message says why and what to do."""


class Bump(IntEnum):
    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    rc: int | None = None

    @property
    def text(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core if self.rc is None else f"{core}-rc.{self.rc}"

    @property
    def tag(self) -> str:
        return f"v{self.text}"

    @property
    def python(self) -> str:
        """The PEP 440 form of the SDK package version: ``1.2.3rc4`` for ``1.2.3-rc.4``."""
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core if self.rc is None else f"{core}rc{self.rc}"

    @property
    def base(self) -> Version:
        return Version(self.major, self.minor, self.patch)

    @property
    def stable(self) -> bool:
        return self.rc is None

    @property
    def key(self) -> tuple[int, int, int, int, int]:
        """Semver precedence: a release candidate sorts below its stable version."""
        return (self.major, self.minor, self.patch, int(self.rc is None), self.rc or 0)

    def bumped(self, bump: Bump) -> Version:
        if bump is Bump.MAJOR:
            return Version(self.major + 1, 0, 0)
        if bump is Bump.MINOR:
            return Version(self.major, self.minor + 1, 0)
        if bump is Bump.PATCH:
            return Version(self.major, self.minor, self.patch + 1)
        return self.base


FIRST_RELEASE = Version(1, 0, 0)


def parse_tag(name: str) -> Version | None:
    """``v1.2.3`` or ``v1.2.3-rc.4``; anything else (``contractum-v0.1.0``, ``v1.2``) is None."""
    match = TAG.fullmatch(name)
    if match is None:
        return None
    major, minor, patch, rc = match.groups()
    return Version(int(major), int(minor), int(patch), int(rc) if rc else None)


@dataclass(frozen=True)
class Commit:
    """One commit, read as a Conventional Commit (``type`` is None when the subject is not one)."""

    sha: str
    subject: str
    type: str | None
    scope: str | None
    description: str
    breaking: bool
    pull_request: int | None


def parse_commit(sha: str, subject: str, body: str = "") -> Commit:
    subject = subject.strip()
    pull_request: int | None = None
    pr_match = PULL_REQUEST.search(subject)
    if pr_match is not None:
        pull_request = int(pr_match.group("number"))
    footer_breaking = BREAKING_FOOTER.search(body) is not None
    match = SUBJECT.fullmatch(subject)
    if match is None:
        description = PULL_REQUEST.sub("", subject)
        return Commit(sha, subject, None, None, description, footer_breaking, pull_request)
    scope = match.group("scope")
    return Commit(
        sha=sha,
        subject=subject,
        type=match.group("type").lower(),
        scope=scope.strip().lower() if scope else None,
        description=PULL_REQUEST.sub("", match.group("description")),
        breaking=footer_breaking or match.group("bang") is not None,
        pull_request=pull_request,
    )


def is_dependency_update(commit: Commit) -> bool:
    return commit.type in DEPENDENCY_TYPES and commit.scope == DEPENDENCY_SCOPE


def bump_of(commit: Commit) -> Bump:
    if commit.breaking:
        return Bump.MAJOR
    if commit.type == "feat":
        return Bump.MINOR
    if commit.type in PATCH_TYPES or is_dependency_update(commit):
        return Bump.PATCH
    return Bump.NONE


def latest_stable(tags: Iterable[Version]) -> Version | None:
    stable = [tag for tag in tags if tag.stable]
    return max(stable, key=lambda v: v.key) if stable else None


def next_release(previous: Version | None, commits: Sequence[Commit]) -> Version | None:
    """The next stable version, or None when no commit since ``previous`` releases anything."""
    if previous is None:
        return FIRST_RELEASE
    bump = max((bump_of(commit) for commit in commits), default=Bump.NONE)
    return None if bump is Bump.NONE else previous.bumped(bump)


def next_candidate(base: Version, tags: Iterable[Version]) -> Version:
    highest = max((tag.rc or 0 for tag in tags if not tag.stable and tag.base == base), default=0)
    return Version(base.major, base.minor, base.patch, highest + 1)


@dataclass(frozen=True)
class Plan:
    version: Version
    sha: str
    previous: Version | None
    candidate: Version | None = None

    def outputs(self) -> dict[str, str]:
        return {
            "version": self.version.text,
            "python-version": self.version.python,
            "tag": self.version.tag,
            "sha": self.sha,
            "previous": self.previous.tag if self.previous else "",
            "prerelease": "false" if self.version.stable else "true",
            "candidate": self.candidate.tag if self.candidate else "",
            "image-version": (self.candidate or self.version).text,
        }


def plan_pre_release(tags: Mapping[Version, str], head: str, commits: Sequence[Commit]) -> Plan:
    """``tags`` maps every release tag to its commit; ``commits`` are the ones since the stable."""
    previous = latest_stable(tags)
    release = next_release(previous, commits)
    if release is None:
        since = previous.tag if previous else "the first commit"
        raise ReleaseStopError(
            f"nothing to release: no feat, fix, perf, security, refactor, revert, dependency or "
            f"breaking commit since {since}."
        )
    for tag, sha in tags.items():
        if not tag.stable and tag.base == release and sha == head:
            raise ReleaseStopError(
                f"{head[:12]} is already the release candidate {tag.tag}: merge a change first, "
                "or promote it."
            )
    return Plan(next_candidate(release, tags), head, previous)


def plan_promote(tags: Mapping[Version, str]) -> Plan:
    previous = latest_stable(tags)
    candidates = [tag for tag in tags if not tag.stable]
    if not candidates:
        raise ReleaseStopError("nothing to promote: there is no release candidate vX.Y.Z-rc.N yet.")
    newest = max(candidates, key=lambda v: v.key)
    if previous is not None and newest.base.key <= previous.key:
        raise ReleaseStopError(
            f"nothing to promote: the newest release candidate {newest.tag} is not newer than "
            f"the stable release {previous.tag} (already promoted). Run a pre-release first."
        )
    return Plan(newest.base, tags[newest], previous, newest)


# --- git ----------------------------------------------------------------------------------------


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed program, arguments are refs and formats
        ["git", *args],  # noqa: S607 - git from PATH, as every developer and runner has it
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def read_tags() -> dict[Version, str]:
    """Every release tag and the commit it points at (annotated tags are dereferenced)."""
    out = git(
        "for-each-ref",
        "--format=%(refname:strip=2)%09%(objectname)%09%(*objectname)",
        "refs/tags/v*",
    )
    tags: dict[Version, str] = {}
    for line in out.splitlines():
        name, obj, deref = (line.split("\t") + ["", ""])[:3]
        version = parse_tag(name)
        if version is not None:
            tags[version] = deref or obj
    return tags


def read_commits(since: str | None, head: str) -> list[Commit]:
    """Commits reachable from ``head`` and not from ``since`` (all of them without one)."""
    revision = f"{since}..{head}" if since else head
    out = git("log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", revision)
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, subject, body = (record.split("\x1f") + ["", ""])[:3]
        commits.append(parse_commit(sha, subject, body))
    return commits


def resolve(rev: str) -> str:
    return git("rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def write_outputs(outputs: Mapping[str, str], github_output: Path | None) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in outputs.items())
    sys.stdout.write(lines)
    if github_output is not None:
        with github_output.open("a", encoding="utf-8") as handle:
            handle.write(lines)


def report_stop(message: str) -> None:
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "error: "
    print(prefix + message, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--mode", required=True, choices=("pre-release", "promote"))
    parser.add_argument("--head", default="HEAD", help="commit to release (pre-release only)")
    parser.add_argument("--github-output", type=Path, default=None)
    args = parser.parse_args(argv)
    tags = read_tags()
    try:
        if args.mode == "promote":
            plan = plan_promote(tags)
        else:
            head = resolve(args.head)
            previous = latest_stable(tags)
            commits = read_commits(previous.tag if previous else None, head)
            plan = plan_pre_release(tags, head, commits)
    except ReleaseStopError as stop:
        report_stop(str(stop))
        return 1
    write_outputs(plan.outputs(), args.github_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
