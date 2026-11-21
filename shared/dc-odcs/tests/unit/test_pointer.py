"""``string_at``: the one JSON-pointer string lookup shared by contracts and validation."""

from __future__ import annotations

from typing import Any

import pytest
from dc_odcs import string_at

pytestmark = pytest.mark.unit

DOC: dict[str, Any] = {
    "id": "orders",
    "schema": [{"name": "t", "properties": [{"name": "a"}, {"name": "b", "count": 3}]}],
    "a/b": "slash",
    "m~n": "tilde",
    "~1": "literal tilde-one",
    "empty": "",
}


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("/id", "orders"),
        ("/schema/0/name", "t"),
        ("/schema/0/properties/1/name", "b"),
        ("/a~1b", "slash"),
        ("/m~0n", "tilde"),
        # ~01 is the escaped text "~1", not a slash: ~1 is unescaped first, then ~0.
        ("/~01", "literal tilde-one"),
        ("/empty", ""),
    ],
)
def test_resolves_strings(pointer: str, expected: str) -> None:
    assert string_at(DOC, pointer) == expected


@pytest.mark.parametrize(
    "pointer",
    [
        "/missing",
        "/schema/1",  # index out of range
        "/schema/0/properties/9/name",
        "/schema/x",  # not an index
        "/schema/-1",
        "/schema/²",  # superscript two: isdigit() but not an index
        "/schema/0/properties/1/count",  # a non-string leaf
        "/schema",  # a list is not a string
        "/schema/0",  # a dict is not a string
        "/id/deeper",  # descending into a string
        "",  # the document itself is not a string
    ],
)
def test_missing_or_non_string_is_none(pointer: str) -> None:
    assert string_at(DOC, pointer) is None


def test_a_string_document_at_the_root_pointer_is_returned() -> None:
    assert string_at("text", "") == "text"
