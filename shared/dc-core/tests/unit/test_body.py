"""The capped request-body reader (T-01-43): refuse early on Content-Length, cap the stream."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from dc_core.body import read_body_capped
from dc_core.problems import ProblemError
from starlette.requests import Request

pytestmark = pytest.mark.unit


class _Spy:
    """Records whether the body was read, and feeds the chunks."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = list(chunks)
        self.reads = 0

    async def receive(self) -> dict[str, object]:
        self.reads += 1
        if self._chunks:
            chunk = self._chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(self._chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}


def _request(spy: _Spy, content_length: str | None) -> Request:
    headers = [(b"content-length", content_length.encode())] if content_length is not None else []
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    return Request(scope, spy.receive)  # type: ignore[arg-type]


async def test_a_declared_length_above_the_cap_is_413_before_reading() -> None:
    spy = _Spy([b"x" * 11])
    with pytest.raises(ProblemError) as caught:
        await read_body_capped(_request(spy, "11"), 10)
    assert (caught.value.status, caught.value.slug) == (413, "payload-too-large")
    assert spy.reads == 0


async def test_a_streamed_body_one_byte_over_the_cap_is_413() -> None:
    spy = _Spy([b"x" * 6, b"x" * 5])
    with pytest.raises(ProblemError) as caught:
        await read_body_capped(_request(spy, None), 10)
    assert caught.value.status == 413


async def test_a_body_exactly_at_the_cap_is_returned_whole() -> None:
    spy = _Spy([b"x" * 6, b"y" * 4])
    assert await read_body_capped(_request(spy, "10"), 10) == b"xxxxxxyyyy"


async def test_a_non_numeric_content_length_falls_through_to_the_streaming_cap() -> None:
    spy = _Spy([b"x" * 11])
    with pytest.raises(ProblemError) as caught:
        await read_body_capped(_request(spy, "abc"), 10)
    assert caught.value.status == 413
    assert spy.reads >= 1


async def test_the_default_detail_names_the_limit() -> None:
    with pytest.raises(ProblemError) as caught:
        await read_body_capped(_request(_Spy([]), "99"), 10)
    assert caught.value.detail == "The document exceeds the 10-byte limit."


async def test_a_custom_detail_replaces_the_default() -> None:
    with pytest.raises(ProblemError) as caught:
        await read_body_capped(_request(_Spy([]), "99"), 10, detail="Rule set too large.")
    assert caught.value.detail == "Rule set too large."
