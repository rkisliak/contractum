"""The diff changelog cursor and the default refusal of ``resolve_ref`` (VAL-09).

The cursor is opaque base64url of ``o<offset>``; anything that is not the canonical encoding of a
non-negative offset is a 422 at ``/query/cursor``, so a tampered cursor never selects a page by
accident. Without an injected refusal, a ref the caller may not see is the uniform 404.
"""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import pytest
from dc_contracts.diffs import decode_diff_cursor, encode_diff_cursor, resolve_ref
from dc_core.problems import ProblemError

pytestmark = pytest.mark.unit


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.mark.parametrize("offset", [0, 1, 25, 10_000])
def test_a_cursor_round_trips_its_offset(offset: int) -> None:
    assert decode_diff_cursor(encode_diff_cursor(offset)) == offset


@pytest.mark.parametrize(
    "raw",
    [
        "A" * 33,  # longer than any real cursor
        b64("x25"),  # not the diff prefix
        b64("o-1"),  # not a decimal offset
        b64("o007"),  # decodes, but is not the canonical cursor for 7
        encode_diff_cursor(7) + "==",  # padding is never part of a cursor
        "@@@@",  # not base64url
    ],
)
def test_anything_but_a_canonical_cursor_is_422_at_the_cursor(raw: str) -> None:
    with pytest.raises(ProblemError) as exc:
        decode_diff_cursor(raw)

    assert (exc.value.status, exc.value.slug) == (422, "validation-error")
    assert exc.value.extensions["errors"] == [
        {"pointer": "/query/cursor", "detail": "cursor is not valid"}
    ]


async def test_without_an_injected_refusal_a_published_only_caller_gets_the_plain_404() -> None:
    contract = SimpleNamespace(id=uuid.uuid4(), latest_version_id=None)

    with pytest.raises(ProblemError) as exc:
        await resolve_ref(
            None,  # type: ignore[arg-type]
            contract,  # type: ignore[arg-type]
            "latest",
            frozenset({"contract:read-published"}),
        )

    assert (exc.value.status, exc.value.slug) == (404, "not-found")
