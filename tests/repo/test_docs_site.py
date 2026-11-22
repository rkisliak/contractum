"""Docs site and docs rules guards (docs/ and mkdocs.yml).

``zensical build --strict`` builds pages that are missing from the nav without a word, and it does
not know the audience rules. These checks are small and fast so they run in the unit step:

- every ``docs/**/*.md`` page is in the mkdocs.yml nav and every nav entry exists;
- user and admin pages (and the home page) carry no internal ids;
- no docs page links into the planning folder, ``.claude/`` or a CLAUDE.md file;
- no relative ``.md`` link under docs/ leaves docs/;
- README.md opens with the centred logo, has at most two badges and no GitHub repository URL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"
README = ROOT / "README.md"

REQUIREMENT_PREFIXES = (
    "UI RULE RBAC VAL AUTH VER SUB PLAT AUD GOV DOCS DATA CONT SDK FETCH API REPORT REF CLIAUTH "
    "TMPL SITE SDKQA DOCCI CLEAN SRV PUB E2E"
).split()
INTERNAL_ID = re.compile(
    r"\bD-(?:[A-Z]+-)*\d+\b"  # decision ids: D-12, D-UI-08-30
    r"|\bT-\d+(?:-[A-Z0-9]+)+\b"  # threat ids: T-15-24, T-8-SC
    r"|\b\d{6}-[a-z0-9]{3}\b"  # quick task ids: 261121-psn
    r"|\bPhase \d+"  # planning phases
    rf"|\b(?:{'|'.join(REQUIREMENT_PREFIXES)})-\d+\b"  # requirement ids: VAL-03, SITE-01
)
# Inline links and images, and HTML href/src attributes.
LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)|(?:href|src)=\"([^\"]+)\"")
FENCE = re.compile(r"^\s*(```|~~~)")


class _NavLoader(yaml.SafeLoader):
    """SafeLoader that reads mkdocs.yml: unknown tags (!ENV, !!python/...) load as None."""


def _ignore_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> None:
    return None


_NavLoader.add_multi_constructor("!", _ignore_tag)
_NavLoader.add_multi_constructor("tag:yaml.org,2002:python/", _ignore_tag)


def _nav_pages(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _nav_pages(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _nav_pages(value)


def _pages(*folders: str) -> list[Path]:
    return sorted(p for folder in folders for p in (DOCS / folder).rglob("*.md"))


def _prose_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Lines outside fenced code blocks."""
    fenced = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


def _links(path: Path) -> Iterator[tuple[int, str]]:
    for number, line in _prose_lines(path):
        for match in LINK.finditer(line):
            yield number, match.group(1) or match.group(2)


# Pages that are built and linked from other pages but deliberately left out of the menu.
OFF_NAV_PAGES = frozenset({"admin/security/asvs-l2.md"})


def test_nav_lists_every_page_and_only_existing_pages() -> None:
    config = yaml.load(MKDOCS.read_text(encoding="utf-8"), Loader=_NavLoader)  # noqa: S506
    nav = list(_nav_pages(config["nav"]))
    assert len(nav) == len(set(nav)), "a page is listed twice in the nav"
    missing = [page for page in nav if not (DOCS / page).is_file()]
    assert missing == [], f"nav entries without a page: {missing}"
    on_disk = {p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*.md")}
    assert OFF_NAV_PAGES <= on_disk, "an off-menu page no longer exists"
    assert OFF_NAV_PAGES.isdisjoint(nav), "an off-menu page is listed in the nav"
    assert sorted(on_disk - set(nav) - OFF_NAV_PAGES) == [], "pages missing from the mkdocs.yml nav"


def test_user_and_admin_pages_carry_no_internal_ids() -> None:
    hits = [
        f"{page.relative_to(ROOT)}:{number}: {match.group(0)}"
        for page in [*_pages("user", "admin"), DOCS / "index.md"]
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1)
        for match in INTERNAL_ID.finditer(line)
    ]
    assert hits == []


# Short assumption and gap ids (A4, G3) only matter in the API text, where no such token is real.
OPENAPI_INTERNAL_ID = re.compile(rf"{INTERNAL_ID.pattern}|\b[A-Z]\d+\b")


def _api_texts(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("description", "summary") and isinstance(value, str):
                yield f"{path}/{key}", value
            else:
                yield from _api_texts(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _api_texts(value, f"{path}/{index}")


def test_openapi_texts_carry_no_internal_ids() -> None:
    spec = json.loads((ROOT / "ui" / "openapi" / "openapi.json").read_text(encoding="utf-8"))
    hits = [
        f"{where}: {match.group(0)}"
        for where, text in _api_texts(spec)
        for match in OPENAPI_INTERNAL_ID.finditer(text)
    ]
    assert hits == []


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("see D-12 and D-UI-08-30", True),
        ("threat T-15-24", True),
        ("quick 261121-psn", True),
        ("added in Phase 9", True),
        ("covers VAL-03 and SITE-01", True),
        ("UTF-8, SHA-256, ISO-8601, RFC-9457, 2026-11-21", False),
    ],
)
def test_internal_id_pattern(text: str, flagged: bool) -> None:
    assert bool(INTERNAL_ID.search(text)) is flagged


def test_no_docs_link_into_planning_agent_files_or_claude_md() -> None:
    planning = "." + "planning"
    hits = [
        f"{page.relative_to(ROOT)}:{number}: {target}"
        for page in _pages(".")
        for number, target in _links(page)
        if planning in target or ".claude/" in target or "CLAUDE.md" in target
    ]
    assert hits == []


def test_relative_md_links_stay_inside_docs() -> None:
    docs = DOCS.resolve()
    hits: list[str] = []
    for page in _pages("."):
        for number, target in _links(page):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith(("#", "/")):
                continue
            path = target.split("#", 1)[0].split("?", 1)[0]
            if not path.endswith(".md"):
                continue
            resolved = (page.parent / path).resolve()
            if not resolved.is_relative_to(docs):
                hits.append(f"{page.relative_to(ROOT)}:{number}: {target}")
    assert hits == []


WORKFLOW_BADGE = re.compile(
    r'<a href="https://github\.com/rkisliak/contractum/actions/workflows/([a-z0-9-]+\.yml)'
    r'\?query=branch%3Amain"><img src="https://github\.com/rkisliak/contractum/actions/workflows/'
    r'\1/badge\.svg\?branch=main" alt="[^"]+"></a>'
)


def test_readme_logo_badges_and_no_repository_url() -> None:
    text = README.read_text(encoding="utf-8")
    first = text.splitlines()[0]
    assert first.startswith('<p align="center"><picture>')
    assert "docs/images/logo-dark.svg" in first and "docs/images/logo-light.svg" in first
    assert len(re.findall(r"https://img\.shields\.io/", text)) <= 2
    # The only repository links are the workflow status badges of this repository, each for a
    # workflow file that exists, and every scheduled or push workflow on main has one.
    badges = WORKFLOW_BADGE.findall(text)
    assert badges, "README must show the workflow status badges"
    for workflow in badges:
        assert (ROOT / ".github" / "workflows" / workflow).is_file(), workflow
    expected = {
        "ci.yml",
        "security.yml",
        "sdk.yml",
        "docs.yml",
        "e2e-nightly.yml",
        "dast-nightly.yml",
        "docs-links-weekly.yml",
    }
    assert expected <= set(badges)
    # Both badge rows are centred.
    assert text.count('<p align="center">') >= 3
    rest = WORKFLOW_BADGE.sub("", text)
    assert not re.search(r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", rest)
