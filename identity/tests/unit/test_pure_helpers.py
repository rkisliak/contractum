"""Pure helpers that answer without the database: the member cursor, member names of an id no
parser accepts, the ltree path inverse and the empty registered-ids query."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from dc_core.problems import ProblemError
from dc_identity.api import groups as groups_api
from dc_identity.entra import registered_object_ids
from dc_identity.folders import ltree_to_slug_path
from dc_identity.models import GroupMember

pytestmark = pytest.mark.unit


class _Untouchable:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the database must not be used (accessed {name!r})")


def test_a_member_cursor_round_trips() -> None:
    member = GroupMember(group_id=uuid.uuid4(), member_type="user", member_id="local:abc")
    cursor = groups_api._encode_member_cursor(member)
    assert groups_api._decode_member_cursor(cursor) == ("user", "local:abc")


@pytest.mark.parametrize("cursor", ["%%%", "bm8tc2VwYXJhdG9y", "_w"])
def test_a_member_cursor_that_does_not_decode_is_a_422_on_the_cursor(cursor: str) -> None:
    with pytest.raises(ProblemError) as caught:
        groups_api._decode_member_cursor(cursor)
    assert (caught.value.status, caught.value.slug) == (422, "validation-error")
    assert caught.value.extensions["errors"] == [
        {"pointer": "/query/cursor", "detail": "cursor is not valid"}
    ]


async def test_a_member_id_no_parser_accepts_has_no_names() -> None:
    member = GroupMember(group_id=uuid.uuid4(), member_type="user", member_id="not-an-id")
    assert await groups_api._member_names(_Untouchable(), member) == (None, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "expected"),
    [("root", ""), ("root.acme.data", "acme/data"), ("acme.data", "acme/data")],
)
def test_ltree_to_slug_path_drops_only_a_leading_root_label(path: str, expected: str) -> None:
    assert ltree_to_slug_path(path) == expected


async def test_no_object_ids_means_no_query_and_no_registrations() -> None:
    found = await registered_object_ids(_Untouchable(), "group", "tenant", [], None)  # type: ignore[arg-type]
    assert found == frozenset()
