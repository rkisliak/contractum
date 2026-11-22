"""NUL (U+0000) or a lone surrogate in request input is a 422, never a 500 (dc_core.nul_guard)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from dc_core.app import create_app
from dc_core.nul_guard import (
    SURROGATE_DETAIL,
    NoNulStr,
    NulGuardRoute,
    find_nul,
    find_unstorable,
    nul_problem,
    reject_nul,
)
from dc_core.policy import PolicyRouter, public
from dc_core.settings import CoreSettings
from fastapi import Request
from pydantic import BaseModel, TypeAdapter, ValidationError

pytestmark = pytest.mark.unit

NUL = "bad\x00text"
SURROGATE = "bad\ud800text"


class Item(BaseModel):
    name: str
    tags: list[str] = []
    labels: dict[str, str] = {}


def _app(calls: list[str]):  # type: ignore[no-untyped-def]
    router = PolicyRouter(prefix="/api/v1/probe")

    @router.post("/items", dependencies=[public()])
    async def create(item: Item) -> dict[str, str]:
        calls.append("handler")
        return {"name": item.name}

    @router.get("/items", dependencies=[public()])
    async def listing(q: str | None = None) -> dict[str, str | None]:
        calls.append("handler")
        return {"q": q}

    @router.put("/raw", dependencies=[public()])
    async def raw(request: Request) -> dict[str, int]:
        calls.append("handler")
        return {"size": len(await request.body())}

    return create_app(
        service="probe",
        settings=CoreSettings(service="probe", database_url="postgresql+psycopg://x/y"),
        routers=[router],
    )


def _assert_nul_422(
    response: Any, pointer: str, detail: str = "NUL (U+0000) characters are not allowed"
) -> None:
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "urn:dc:problem:validation-error"
    assert body["errors"] == [{"pointer": pointer, "detail": detail}]
    # The submitted value is never echoed. The request id fields are random hex, and b, a and d
    # are hex digits, so they are left out of the search.
    echo_surface = {k: v for k, v in body.items() if k not in ("instance", "request_id")}
    assert "bad" not in json.dumps(echo_surface)


def test_policy_router_routes_are_nul_guarded() -> None:
    router = PolicyRouter()
    assert router.route_class is NulGuardRoute
    assert PolicyRouter(route_class=NulGuardRoute).route_class is NulGuardRoute


@pytest.mark.parametrize(
    ("payload", "pointer"),
    [
        ({"name": NUL}, "/body/name"),
        ({"name": "ok", "tags": ["fine", NUL]}, "/body/tags/1"),
        ({"name": "ok", "labels": {"k/x": NUL}}, "/body/labels/k~1x"),
        ({"name": "ok", "labels": {NUL: "v"}}, "/body/labels"),
    ],
)
async def test_nul_in_a_json_body_is_a_422_before_the_handler(
    asgi_client: Any, payload: dict[str, Any], pointer: str
) -> None:
    calls: list[str] = []
    async with asgi_client(_app(calls)) as client:
        _assert_nul_422(await client.post("/api/v1/probe/items", json=payload), pointer)
    assert calls == []


async def test_a_clean_body_and_an_escaped_backslash_pass(asgi_client: Any) -> None:
    calls: list[str] = []
    async with asgi_client(_app(calls)) as client:
        ok = await client.post("/api/v1/probe/items", json={"name": "plain"})
        assert ok.status_code == 200, ok.text
        # The literal text \u0000 (escaped backslash) is not a NUL and is accepted.
        literal = await client.post(
            "/api/v1/probe/items",
            content=b'{"name": "a\\\\u0000b"}',
            headers={"content-type": "application/json"},
        )
        assert literal.status_code == 200, literal.text
        assert literal.json() == {"name": "a\\u0000b"}
    assert calls == ["handler", "handler"]


async def test_invalid_json_keeps_fastapis_own_422(asgi_client: Any) -> None:
    async with asgi_client(_app([])) as client:
        response = await client.post(
            "/api/v1/probe/items",
            content=b'{"name": "\\u0000',
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 422
    assert response.json()["errors"][0]["detail"] == "JSON decode error"


async def test_nul_in_a_query_parameter_is_a_422(asgi_client: Any) -> None:
    calls: list[str] = []
    async with asgi_client(_app(calls)) as client:
        _assert_nul_422(await client.get("/api/v1/probe/items", params={"q": NUL}), "/query/q")
        _assert_nul_422(await client.get("/api/v1/probe/items?bad%00name=1"), "/query")
        ok = await client.get("/api/v1/probe/items", params={"q": "fine"})
        assert ok.json() == {"q": "fine"}
    assert calls == ["handler"]


async def test_a_raw_body_route_is_not_read_by_the_guard(asgi_client: Any) -> None:
    calls: list[str] = []
    async with asgi_client(_app(calls)) as client:
        response = await client.put(
            "/api/v1/probe/raw",
            content=b'{"name": "\\u0000"}',
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 200
    assert calls == ["handler"]


def test_find_nul_pointers() -> None:
    assert find_nul({"a": [1, "x", {"b": "ok"}]}) is None
    assert find_nul(NUL) == ""
    assert find_nul({"a": [1, "x", {"b": NUL}]}) == "/a/2/b"
    assert find_nul({"a~b": {NUL: 1}}) == "/a~0b"
    deep: Any = NUL
    for _ in range(5000):
        deep = [deep]
    assert find_nul(deep) == "/0" * 5000


def test_nul_problem_slugs() -> None:
    problem = nul_problem("/rules/0/message", slug="malformed-document")
    assert (problem.status, problem.slug) == (422, "malformed-document")
    assert problem.extensions["errors"][0]["pointer"] == "/rules/0/message"
    assert nul_problem("").extensions["errors"][0]["pointer"] == "/"


def test_no_nul_str() -> None:
    assert reject_nul("fine") == "fine"
    adapter = TypeAdapter(NoNulStr)
    assert adapter.validate_python("fine") == "fine"
    with pytest.raises(ValidationError):
        adapter.validate_python(NUL)


def _post_raw(client: Any, raw: bytes, content_type: str = "application/json") -> Any:
    return client.post("/api/v1/probe/items", content=raw, headers={"content-type": content_type})


@pytest.mark.parametrize(
    ("raw", "pointer"),
    [
        (b'{"name": "bad\\ud800text"}', "/body/name"),
        (b'{"name": "bad\\uDFFFtext"}', "/body/name"),  # hex digits in either case
        (b'{"name": "ok", "tags": ["fine", "bad\\udc00"]}', "/body/tags/1"),
        (b'{"name": "ok", "labels": {"k": "bad\\uDbFf"}}', "/body/labels/k"),
        (b'{"name": "ok", "labels": {"bad\\ud800": "v"}}', "/body/labels"),
        # Raw UTF-8-style bytes for U+D800: json.loads decodes bytes with surrogatepass.
        (b'{"name": "bad\xed\xa0\x80text"}', "/body/name"),
        # UTF-16 JSON is parsed too; it is always walked.
        ('{"name": "bad\\ud800text"}'.encode("utf-16-le"), "/body/name"),
    ],
)
async def test_a_lone_surrogate_in_a_json_body_is_a_422_before_the_handler(
    asgi_client: Any, raw: bytes, pointer: str
) -> None:
    calls: list[str] = []
    async with asgi_client(_app(calls)) as client:
        _assert_nul_422(await _post_raw(client, raw), pointer, SURROGATE_DETAIL)
    assert calls == []


async def test_a_nul_in_a_utf16_body_is_a_422(asgi_client: Any) -> None:
    calls: list[str] = []
    raw = '{"name": "bad\\u0000text"}'.encode("utf-16-be")
    async with asgi_client(_app(calls)) as client:
        _assert_nul_422(await _post_raw(client, raw), "/body/name")
    assert calls == []


async def test_a_surrogate_pair_and_astral_text_pass(asgi_client: Any) -> None:
    calls: list[str] = []
    async with asgi_client(_app(calls)) as client:
        paired = await _post_raw(client, b'{"name": "\\ud83d\\ude00"}')
        assert paired.status_code == 200, paired.text
        assert paired.json() == {"name": "\U0001f600"}
        utf8 = await client.post("/api/v1/probe/items", json={"name": "\U0001f600 \u00e9"})
        assert utf8.status_code == 200, utf8.text
        literal = await _post_raw(client, b'{"name": "a\\\\ud800b"}')
        assert literal.status_code == 200, literal.text
        assert literal.json() == {"name": "a\\ud800b"}
    assert calls == ["handler", "handler", "handler"]


async def test_percent_encoded_surrogate_bytes_in_a_query_decode_to_u_fffd(
    asgi_client: Any,
) -> None:
    calls: list[str] = []
    async with asgi_client(_app(calls)) as client:
        # Percent-decoding never yields a surrogate (invalid UTF-8 becomes U+FFFD), so the
        # decoded values are the ones checked.
        ok = await client.get("/api/v1/probe/items?q=%ED%A0%80")
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"q": "\ufffd\ufffd\ufffd"}
    assert calls == ["handler"]


def test_query_values_are_checked_after_decoding() -> None:
    from dc_core.nul_guard import _query_problem

    class _Fake:
        def __init__(self, items: list[tuple[str, str]]) -> None:
            self.query_params = self
            self._items = items

        def multi_items(self) -> list[tuple[str, str]]:
            return self._items

    assert _query_problem(_Fake([("q", SURROGATE)])) == ("/query/q", SURROGATE_DETAIL)  # type: ignore[arg-type]
    assert _query_problem(_Fake([(SURROGATE, "v")])) == ("/query", SURROGATE_DETAIL)  # type: ignore[arg-type]
    assert _query_problem(_Fake([("q", "fine")])) is None  # type: ignore[arg-type]


def test_find_unstorable_pointers_and_details() -> None:
    assert find_unstorable({"a": ["x", "\U0001f600"]}) is None
    assert find_unstorable({"a": [1, {"b": SURROGATE}]}) == ("/a/1/b", SURROGATE_DETAIL)
    assert find_unstorable({"a/b": {"\udfff": 1}}) == ("/a~1b", SURROGATE_DETAIL)
    assert find_unstorable({"n": NUL}) == ("/n", "NUL (U+0000) characters are not allowed")
    assert find_nul({"a": SURROGATE}) == "/a"


def test_nul_problem_carries_the_surrogate_detail() -> None:
    problem = nul_problem("/rules/0", slug="malformed-document", detail=SURROGATE_DETAIL)
    assert problem.detail == SURROGATE_DETAIL
    assert problem.extensions["errors"] == [{"pointer": "/rules/0", "detail": SURROGATE_DETAIL}]


def test_no_nul_str_refuses_a_lone_surrogate() -> None:
    with pytest.raises(ValueError, match="surrogate"):
        reject_nul(SURROGATE)
    with pytest.raises(ValidationError):
        TypeAdapter(NoNulStr).validate_python(SURROGATE)


def test_find_unstorable_walks_values_under_non_string_keys() -> None:
    assert find_unstorable({1: "fine", 2: ["ok"]}) is None
    assert find_unstorable({7: NUL}) == ("/7", "NUL (U+0000) characters are not allowed")


async def test_a_body_without_a_content_type_is_left_to_fastapi(asgi_client: Any) -> None:
    calls: list[str] = []
    async with asgi_client(_app(calls)) as client:
        response = await client.post("/api/v1/probe/items", content=b'{"name": "bad\\u0000"}')
    # FastAPI's strict content type refuses the body itself: the guard never parsed it.
    assert response.status_code == 422, response.text
    errors = response.json()["errors"]
    assert [e["pointer"] for e in errors] == ["/body"]
    assert errors[0]["detail"] != "NUL (U+0000) characters are not allowed"
    assert calls == []
