"""`.github/dependabot.yml` commit messages follow the Conventional Commits rule in CONTRIBUTING.md.

Every package ecosystem opens `build(deps): ...` pull requests (a dependency update is a build
change), and GitHub Actions updates open `ci(deps): ...` (they change only the CI workflows).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
DEPENDABOT = REPO / ".github" / "dependabot.yml"


def _updates() -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))[
        "updates"
    ]
    return updates


def test_every_update_entry_sets_a_commit_message_prefix() -> None:
    updates = _updates()
    assert updates
    for update in updates:
        assert update["commit-message"]["prefix"], update["package-ecosystem"]


@pytest.mark.parametrize(
    "update",
    _updates(),
    ids=lambda update: ":".join(
        [update["package-ecosystem"], update.get("directory") or ",".join(update["directories"])]
    ),
)
def test_the_prefix_matches_the_conventional_commits_rule(update: dict[str, Any]) -> None:
    expected = "ci(deps)" if update["package-ecosystem"] == "github-actions" else "build(deps)"
    assert update["commit-message"]["prefix"] == expected


def test_contributing_names_the_dependency_prefix() -> None:
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "Dependency updates use `build(deps):`" in contributing
