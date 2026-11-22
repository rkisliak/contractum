"""`.github/CODEOWNERS` gives every file in the repository a code owner.

GitHub applies the last matching rule, so the catch-all ``*`` must be the first rule and the more
specific lines below it stay valid. A rule without an owner removes ownership for its paths, and a
path that no longer exists silently stops matching anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
CODEOWNERS = REPO / ".github" / "CODEOWNERS"
MAINTAINER = "@" + "rkisliak"
GLOB_CHARS = set("*?[")


def rules() -> list[tuple[str, list[str]]]:
    """The (pattern, owners) pairs, in file order, without comments and blank lines."""
    parsed: list[tuple[str, list[str]]] = []
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        pattern, *owners = stripped.split()
        parsed.append((pattern, owners))
    return parsed


def test_first_rule_is_the_catch_all_owned_by_the_maintainer() -> None:
    parsed = rules()
    assert parsed, "CODEOWNERS has no rules"
    assert parsed[0] == ("*", [MAINTAINER])


def test_every_rule_has_an_owner() -> None:
    ownerless = [pattern for pattern, owners in rules() if not owners]
    assert ownerless == []


def test_every_literal_path_exists() -> None:
    missing = [
        pattern
        for pattern, _ in rules()
        if not GLOB_CHARS & set(pattern) and not (REPO / pattern.strip("/")).exists()
    ]
    assert missing == []
