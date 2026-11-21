"""Every problem slug raised in service or library code is documented (T-02-54, PLAT-06).

Scans the source of the five services and the shared libraries for the literal slug passed as the
second argument of ``ProblemError(...)`` (the call may span lines), plus the rejection slugs the
``dc_odcs`` library declares as ``slug="..."``, and asserts that each appears in
``docs/user/api-guide.md`` as ``urn:dc:problem:<slug>``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "user" / "api-guide.md"

SOURCE_ROOTS = (
    ROOT / "identity" / "src",
    ROOT / "contracts" / "src",
    ROOT / "validation" / "src",
    ROOT / "audit" / "src",
    ROOT / "mail" / "src",
    *sorted((ROOT / "shared").glob("*/src")),
)

# ProblemError(<status expression>, "<slug>", ...): the status may be a name or attribute.
_PROBLEM_ERROR = re.compile(r"""ProblemError\(\s*[\w.]+\s*,\s*["']([a-z0-9][a-z0-9-]*)["']""")
# dc_odcs rejections: ContractRejected(..., slug="...") and friends.
_ODCS_SLUG = re.compile(r"""\bslug\s*=\s*["']([a-z0-9][a-z0-9-]*)["']""")


def _raised_slugs(roots: tuple[Path, ...] = SOURCE_ROOTS, *, base: Path = ROOT) -> dict[str, str]:
    """Map each literal slug under ``roots`` to one file (relative to ``base``) that raises it."""
    found: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            matches = list(_PROBLEM_ERROR.finditer(text))
            if "dc_odcs" in path.parts:
                matches += list(_ODCS_SLUG.finditer(text))
            for match in matches:
                found.setdefault(match.group(1), str(path.relative_to(base)))
    return found


def _undocumented(raised: dict[str, str], doc_text: str) -> dict[str, str]:
    """The raised slugs that ``doc_text`` does not carry as ``urn:dc:problem:<slug>``."""
    documented = set(re.findall(r"urn:dc:problem:([a-z0-9][a-z0-9-]*)", doc_text))
    return {slug: where for slug, where in raised.items() if slug not in documented}


def test_scan_finds_the_known_slugs() -> None:
    """A vacuous scan (a broken regex or moved sources) must not pass silently."""
    raised = _raised_slugs()
    assert len(raised) >= 25, sorted(raised)
    assert {"not-found", "grant-ceiling-exceeded", "template-version-deprecated"} <= set(raised)
    assert "malformed-document" in raised  # dc_odcs rejection slug


def test_every_raised_problem_slug_is_documented() -> None:
    missing = _undocumented(_raised_slugs(), DOC.read_text("utf-8"))
    assert not missing, (
        "problem slugs raised in code but missing from docs/user/api-guide.md "
        f"(add a `urn:dc:problem:<slug>` row): {missing}"
    )


# --- the mail service (D-105) is scanned, and an undocumented slug there is reported -----------


def test_the_scan_covers_the_mail_service_sources() -> None:
    assert ROOT / "mail" / "src" in SOURCE_ROOTS


def test_a_planted_undocumented_mail_slug_is_found_and_reported(tmp_path: Path) -> None:
    source = tmp_path / "mail" / "src" / "dc_mail"
    source.mkdir(parents=True)
    (source / "planted.py").write_text(
        'raise ProblemError(503, "mail-made-up-slug", "Service Unavailable", "x")\n',
        encoding="utf-8",
    )
    raised = _raised_slugs((tmp_path / "mail" / "src",), base=tmp_path)
    assert raised == {"mail-made-up-slug": "mail/src/dc_mail/planted.py"}
    documented_text = "| `urn:dc:problem:not-found` | 404 |\n"
    assert _undocumented(raised, documented_text) == raised
    assert _undocumented(raised, documented_text + "urn:dc:problem:mail-made-up-slug") == {}
