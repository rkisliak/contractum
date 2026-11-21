"""The UI's permission registry snapshot matches dc_auth's role matrix.

``ui/src/features/access/permissionRegistry.ts`` lists every permission id (with its evaluation
target) and every role id so the UI can give each one a readable label
(``permissionLabels.test.ts`` asserts the labels). A permission or role added to
``dc_auth.roles`` without updating the snapshot fails here, in the unit step, with no Node needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from dc_auth.roles import PERMISSIONS, Role

pytestmark = pytest.mark.unit

SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "ui"
    / "src"
    / "features"
    / "access"
    / "permissionRegistry.ts"
)


def _block(text: str, name: str) -> str:
    """The body of ``export const <name>`` up to its closing bracket."""
    match = re.search(rf"export const {name}\b.*?(?:\] as const;|\];|\n}};)", text, re.S)
    assert match is not None, f"{name} not found in {SNAPSHOT.name}"
    return match.group(0)


def test_permission_ids_match_the_registry() -> None:
    text = SNAPSHOT.read_text(encoding="utf-8")
    ids = re.findall(r'"([a-z-]+:[a-z-]+)"', _block(text, "PERMISSION_IDS"))
    assert ids == list(PERMISSIONS)


def test_permission_targets_match_the_registry() -> None:
    text = SNAPSHOT.read_text(encoding="utf-8")
    targets = dict(
        re.findall(r'"([a-z-]+:[a-z-]+)": "([a-z]+)"', _block(text, "PERMISSION_TARGETS"))
    )
    assert targets == {name: spec.target for name, spec in PERMISSIONS.items()}


def test_role_ids_match_the_registry() -> None:
    text = SNAPSHOT.read_text(encoding="utf-8")
    roles = re.findall(r'"([a-z_]+)"', _block(text, "ROLE_IDS"))
    assert roles == [str(role) for role in Role]
