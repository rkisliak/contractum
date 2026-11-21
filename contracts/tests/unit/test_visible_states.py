"""Which row states a version read route may return (A3, D-48)."""

from __future__ import annotations

import pytest
from dc_contracts.versions import visible_states

pytestmark = pytest.mark.unit


def test_a_published_only_reader_sees_published_rows_and_never_drafts() -> None:
    assert visible_states(frozenset({"contract:read-published"})) == {"published"}
    assert visible_states(frozenset({"contract:read-published", "contract:read"})) == {"published"}
    assert visible_states(frozenset()) == {"published"}


def test_an_owner_who_can_publish_also_finds_unpublished_rows() -> None:
    states = visible_states(frozenset({"contract:read", "contract:publish"}))
    assert states == {"published", "unpublished"}
    assert "draft" not in states
