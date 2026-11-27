"""CONT-06: what a team submits is exactly what consumers fetch. Nothing is normalised,
reformatted, re-keyed or re-typed; problems are reported, never repaired. No document content
reaches audit events or logs."""

from __future__ import annotations

import asyncio
import itertools
import json
import string
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.integration

StackFactory = Callable[..., AbstractAsyncContextManager[Any]]
SECRET = "sk-test-SECRET-123"  # noqa: S105


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def yaml_doc(*, extra: str = "", version: str = '"1.0.0"', description: str = "Orders.") -> str:
    return (
        "kind: DataContract\n"
        f"id: urn:datacontract:fidelity:{uuid.uuid4()}\n"
        "apiVersion: v3.2.0\n"
        "name: Fidelity\n"
        f"version: {version}\n"
        "status: draft\n"
        f"description:\n  purpose: {description}\n" + extra
    )


async def roundtrip(stack: Any, team: str, raw: bytes, media_type: str) -> tuple[Any, Any]:
    created = await stack.post(team, raw, media_type)
    assert created.status_code == 201, created.text
    contract_id = created.json()["contract"]["id"]
    fetched = await stack.client.get(
        f"/api/v1/contracts/contracts/{contract_id}/document", headers=bearer(stack.admin_token)
    )
    assert fetched.status_code == 200
    return created, fetched


async def test_yaml_comments_and_formatting_are_preserved(make_stack: StackFactory) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        raw = (
            "# leading comment\n\n"
            + yaml_doc(extra="tags:   [b, a]   # inline comment, odd spacing\n")
            + "\n\n# trailing comment, no final newline"
        ).encode()
        _, fetched = await roundtrip(stack, chain.team, raw, "application/yaml")
        assert fetched.content == raw
        assert fetched.headers["content-type"] == "application/yaml"


async def test_crlf_line_endings_are_preserved(make_stack: StackFactory) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        raw = yaml_doc(extra="# a comment\n").replace("\n", "\r\n").encode()
        assert raw.count(b"\r\n") == raw.count(b"\n")
        _, fetched = await roundtrip(stack, chain.team, raw, "application/yaml")
        assert fetched.content == raw


async def test_utf8_bom_is_preserved_and_does_not_break_parsing(make_stack: StackFactory) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        raw = b"\xef\xbb\xbf" + yaml_doc().encode()
        created, fetched = await roundtrip(stack, chain.team, raw, "application/yaml")
        assert fetched.content == raw
        assert fetched.content.startswith(b"\xef\xbb\xbf")
        # The hash and size describe the exact submitted bytes, BOM included.
        assert created.json()["contract"]["size_bytes"] == len(raw)


async def test_trailing_spaces_and_tabs_inside_quoted_values_are_preserved(
    make_stack: StackFactory, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        raw = yaml_doc(description='"a\tb  c"   ').replace("name: Fidelity", "name: Fidelity  ")
        _, fetched = await roundtrip(stack, chain.team, raw.encode(), "application/yaml")
        assert fetched.content == raw.encode()
        stored = sql(
            stack.contracts_db_url,
            "SELECT document -> 'description' ->> 'purpose' AS p FROM contract_version",
        )
        assert stored[0]["p"] == "a\tb  c"


async def test_json_formatting_key_order_and_escapes_are_preserved(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        raw = (
            "{\n"
            '    "kind": "DataContract",\n'
            '    "name": "Caf\\u00e9 orders",\n'
            f'    "id": "urn:datacontract:fidelity:{uuid.uuid4()}",\n'
            '    "version": "1.0.0",\n'
            '    "apiVersion": "v3.2.0",\n'
            '    "status": "draft"\n'
            "}\n"
        ).encode()
        assert json.loads(raw)["name"] == "Café orders"
        _, fetched = await roundtrip(stack, chain.team, raw, "application/json")
        assert fetched.content == raw
        assert fetched.headers["content-type"] == "application/json"


async def test_float_version_is_stored_and_fetched_unchanged_and_only_reported(
    make_stack: StackFactory, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        raw = yaml_doc(version="1.0").encode()
        created, fetched = await roundtrip(stack, chain.team, raw, "application/yaml")
        assert b"version: 1.0\n" in fetched.content
        assert fetched.content == raw  # not coerced to "1.0", not rewritten

        stored = sql(
            stack.contracts_db_url,
            "SELECT jsonb_typeof(document -> 'version') AS t, "
            "(document -> 'version')::text AS v FROM contract_version",
        )
        assert stored[0] == {"t": "number", "v": "1.0"}  # jsonb keeps the number 1.0

        report = created.json()["structural_validation"]
        assert report["valid"] is False
        assert "/version" in [error["pointer"] for error in report["errors"]]
        # The contract is saved anyway (D-19): problems are reported, not repaired or blocked.
        assert created.json()["version"] == {"number": 1, "state": "draft", "deprecated": False}


async def test_custom_properties_are_present_in_the_jsonb_copy(
    make_stack: StackFactory, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        extra = (
            "customProperties:\n"
            "  - property: costCentre\n"
            "    value: cc-4711\n"
            "  - property: reviewedOn\n"
            "    value: 2026-11-09T10:00:00Z\n"
            "  - property: nested\n"
            "    value: {a: 1, b: [true, null]}\n"
        )
        await roundtrip(stack, chain.team, yaml_doc(extra=extra).encode(), "application/yaml")
        stored = sql(
            stack.contracts_db_url,
            "SELECT document -> 'customProperties' AS props FROM contract_version",
        )
        assert stored[0]["props"] == [
            {"property": "costCentre", "value": "cc-4711"},
            {"property": "reviewedOn", "value": "2026-11-09T10:00:00Z"},
            {"property": "nested", "value": {"a": 1, "b": [True, None]}},
        ]


async def test_no_document_content_reaches_events_or_logs(
    make_stack: StackFactory,
    sql: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    async with make_stack() as stack:
        chain = await stack.chain()
        extra = f"customProperties:\n  - property: apiKey\n    value: {SECRET}\n"
        created, fetched = await roundtrip(
            stack, chain.team, yaml_doc(extra=extra).encode(), "application/yaml"
        )
        assert SECRET.encode() in fetched.content  # the author's document is served as-is
        assert SECRET not in created.text  # ...but is not echoed by the create response

        # A rejected submission must not echo or log the secret either.
        rejected = await stack.post(
            chain.team,
            yaml_doc(extra=extra).replace("v3.2.0", "v9.9.9").encode(),
            "application/yaml",
        )
        assert rejected.status_code == 422
        assert SECRET not in rejected.text

        rows = sql(stack.contracts_db_url, "SELECT to_jsonb(outbox) AS row FROM outbox")
        assert len(rows) == 1
        assert SECRET not in json.dumps(rows, default=str)
        assert SECRET not in json.dumps(
            sql(stack.contracts_db_url, "SELECT after FROM outbox"), default=str
        )

    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert SECRET not in caplog.text


# --- property: comment lines never change what is fetched -------------------------------------

_TEXT = st.text(alphabet=string.ascii_letters + string.digits + " -_é", max_size=20)


@st.composite
def commented_documents(draw: st.DrawFn) -> bytes:
    document: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": draw(st.sampled_from(["v3.1.0", "v3.2.0"])),
        "id": "placeholder",
        "version": draw(_TEXT),
        "status": draw(st.sampled_from(["draft", "active"])),
        "name": draw(_TEXT),
        "tags": draw(st.lists(_TEXT, max_size=4)),
        "customProperties": draw(
            st.lists(st.fixed_dictionaries({"property": _TEXT, "value": _TEXT}), max_size=3)
        ),
    }
    document["id"] = f"urn:datacontract:property:{uuid.uuid4()}"
    lines = yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True, default_flow_style=False, width=10_000
    ).split("\n")
    comments = draw(st.lists(st.tuples(st.integers(0, len(lines)), _TEXT), max_size=6))
    for position, text in sorted(comments, reverse=True):
        lines.insert(position, f"# {text}")
    return "\n".join(lines).encode("utf-8")


def test_property_comment_lines_never_change_the_fetched_bytes(make_stack: StackFactory) -> None:
    # Hypothesis drives a synchronous function, so the test owns its event loop.
    loop = asyncio.new_event_loop()
    manager = make_stack()
    try:
        stack = loop.run_until_complete(manager.__aenter__())
        chain = loop.run_until_complete(stack.chain())
        checked = itertools.count(1)

        async def check(raw: bytes) -> None:
            created = await stack.post(chain.team, raw, "application/yaml")
            assert created.status_code == 201, created.text
            fetched = await stack.client.get(
                f"/api/v1/contracts/contracts/{created.json()['contract']['id']}/document",
                headers=bearer(stack.admin_token),
            )
            assert fetched.content == raw
            next(checked)

        @settings(
            max_examples=30,
            deadline=None,
            database=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
        )
        @given(commented_documents())
        def prop(raw: bytes) -> None:
            loop.run_until_complete(check(raw))

        prop()
        assert next(checked) > 20  # the property really ran over generated documents
    finally:
        loop.run_until_complete(manager.__aexit__(None, None, None))
        loop.close()
