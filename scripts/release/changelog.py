#!/usr/bin/env python3
"""Release notes of a platform release, from the Conventional Commits since the last stable tag.

    python3 scripts/release/changelog.py --version 1.2.0 --to <sha> [--since v1.1.0] \\
        --repo owner/contractum [--server-url https://github.com] [--output notes.md]

The notes of a release candidate and of its promotion both list every commit since the previous
stable tag (``--since``; without it, the whole history: the first release). Groups, in order:
Breaking changes, Features, Fixes, Security (``security`` commits and ``fix(security)``),
Performance and Other changes (``refactor``, ``revert``, dependency updates and subjects that are
not Conventional Commits). ``docs``, ``test``, ``ci``, ``style``, ``build`` and ``chore`` commits
are left out. Inside a group the lines are sorted by scope; each links its pull request (the
``(#NN)`` of a squash-merge subject) or, without one, its commit. The notes end with the image
and chart references of the version and how to verify them. Standard library only.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # next_version.py sits next to this file

from next_version import Commit, git, is_dependency_update, read_commits  # noqa: E402

GROUPS = ("Breaking changes", "Features", "Fixes", "Security", "Performance", "Other changes")
OTHER_TYPES = frozenset({"refactor", "revert"})
COMPONENTS = ("identity", "contracts", "validation", "audit", "mail", "api-docs", "ui")


def group_of(commit: Commit) -> str | None:
    """The notes section of ``commit``, or None when it is left out of the notes."""
    if commit.breaking:
        return "Breaking changes"
    if commit.type is None:
        return "Other changes"
    if commit.type == "feat":
        return "Features"
    if commit.type == "security" or (commit.type == "fix" and commit.scope == "security"):
        return "Security"
    if commit.type == "fix":
        return "Fixes"
    if commit.type == "perf":
        return "Performance"
    if commit.type in OTHER_TYPES or is_dependency_update(commit):
        return "Other changes"
    return None


def _line(commit: Commit, repo_url: str, group: str) -> str:
    description = commit.description[:1].upper() + commit.description[1:]
    # `fix(security)` already sits under Security: its scope would only repeat the heading.
    shown = (
        commit.scope if commit.scope and (group, commit.scope) != ("Security", "security") else None
    )
    scope = f"**{shown}:** " if shown else ""
    if commit.pull_request is not None:
        link = f"[#{commit.pull_request}]({repo_url}/pull/{commit.pull_request})"
    else:
        link = f"[{commit.sha[:12]}]({repo_url}/commit/{commit.sha})"
    return f"- {scope}{description} ({link})"


def changes(commits: Sequence[Commit], repo_url: str, since: str | None) -> str:
    """The grouped change list (``commits`` newest first, as ``git log`` lists them)."""
    grouped: dict[str, list[tuple[int, Commit]]] = {name: [] for name in GROUPS}
    for index, commit in enumerate(reversed(commits)):  # oldest first inside a scope
        group = group_of(commit)
        if group is not None:
            grouped[group].append((index, commit))
    lines = ["## Changes", ""]
    lines.append(f"Since {since}." if since else "Everything in the first release.")
    lines.append("")
    if not any(grouped.values()):
        lines += ["No user-facing changes.", ""]
    for name in GROUPS:
        entries = grouped[name]
        if not entries:
            continue
        entries.sort(key=lambda item: (item[1].scope is None, item[1].scope or "", item[0]))
        lines += [f"### {name}", ""]
        lines += [_line(commit, repo_url, name) for _, commit in entries]
        lines.append("")
    return "\n".join(lines)


def artifacts(version: str, repo: str) -> str:
    owner = repo.split("/", 1)[0].lower()
    registry = f"ghcr.io/{owner}"
    lines = ["## Images and Helm chart", ""]
    lines.append(f"Every image and the chart carry the version `{version}`:")
    lines.append("")
    lines += [f"- `{registry}/contractum/{name}:{version}`" for name in COMPONENTS]
    lines.append(f"- Helm chart `oci://{registry}/charts/contractum`, version `{version}`")
    lines += [
        "",
        "Install or upgrade with:",
        "",
        "```bash",
        f"helm upgrade --install dc oci://{registry}/charts/contractum --version {version} \\",
        "  --namespace contractum --create-namespace -f values-prod.yaml",
        "```",
        "",
        "Verify an image and the chart archive attached to this release:",
        "",
        "```bash",
        f"gh attestation verify oci://{registry}/contractum/identity:{version} --repo {repo}",
        f"gh attestation verify contractum-{version}.tgz --repo {repo}",
        "```",
        "",
    ]
    return "\n".join(lines)


def notes(
    version: str, commits: Sequence[Commit], repo: str, server_url: str, since: str | None
) -> str:
    repo_url = f"{server_url.rstrip('/')}/{repo}"
    return changes(commits, repo_url, since) + "\n" + artifacts(version, repo)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--version", required=True, help="the release version, without the v")
    parser.add_argument("--to", required=True, help="the released commit")
    parser.add_argument("--since", default="", help="the previous stable tag (empty: first)")
    parser.add_argument("--repo", required=True, help="owner/name of the GitHub repository")
    parser.add_argument("--server-url", default="https://github.com")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    since = args.since or None
    head = git("rev-parse", "--verify", f"{args.to}^{{commit}}").strip()
    text = notes(args.version, read_commits(since, head), args.repo, args.server_url, since)
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
