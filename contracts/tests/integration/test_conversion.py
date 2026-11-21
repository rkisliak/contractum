"""Accept-driven document conversion (D-11, CONT-06): the default stays byte-faithful.

A conversion is re-rendered from the stored raw bytes (never the jsonb copy, which loses key
order), so it is not byte-faithful by definition; the stored representation always is.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx
import pytest
from dc_auth.testing import FakeIssuer
from dc_odcs import load_json, load_yaml

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
StackFactory = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def yaml_contract() -> bytes:
    return (FIXTURES / "valid-v3.2.0.yaml").read_bytes()


def json_contract() -> bytes:
    document = load_yaml(yaml_contract().decode("utf-8"))
    # YAML 1.1 look-alikes: these must stay strings through a JSON -> YAML conversion.
    document["name"] = "NO"
    document["tags"] = ["yes", "on", "2026-01-01", "1_000", "null", "~"]
    document["description"] = {"purpose": "café – multi\nline text: with colon # and hash"}
    return json.dumps(document, indent=1, ensure_ascii=False).encode("utf-8")


async def stored(stack: Any, raw: bytes, media_type: str) -> str:
    created = await stack.post(await stack_team(stack), raw, media_type)
    assert created.status_code == 201, created.text
    return str(created.json()["contract"]["id"])


async def stack_team(stack: Any) -> str:
    return str((await stack.chain()).team)


async def fetch(stack: Any, contract_id: str, accept: str | None) -> httpx.Response:
    headers = bearer(stack.admin_token)
    if accept is not None:
        headers["Accept"] = accept
    return await stack.client.get(
        f"/api/v1/contracts/contracts/{contract_id}/document", headers=headers
    )


async def test_yaml_contract_as_json_keeps_the_original_key_order(make_stack: StackFactory) -> None:
    async with make_stack() as stack:
        raw = yaml_contract()
        contract_id = await stored(stack, raw, "application/yaml")
        response = await fetch(stack, contract_id, "application/json")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/json"
        assert response.headers["vary"] == "Accept"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-disposition"] == "inline"
        expected = load_yaml(raw.decode("utf-8"))
        assert load_json(response.text) == expected
        # same mapping AND the same key order, at every depth
        assert json.dumps(json.loads(response.text), ensure_ascii=False) == json.dumps(
            expected, ensure_ascii=False
        )
        assert response.content != raw  # a conversion is not the submitted bytes


async def test_json_contract_as_yaml_keeps_yaml_look_alikes_as_strings(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        raw = json_contract()
        contract_id = await stored(stack, raw, "application/json")
        response = await fetch(stack, contract_id, "application/yaml")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/yaml"
        assert response.headers["vary"] == "Accept"
        parsed = load_yaml(response.text)  # the strict loader: NO / yes / dates stay strings
        assert parsed == json.loads(raw)
        assert parsed["name"] == "NO"
        assert parsed["tags"] == ["yes", "on", "2026-01-01", "1_000", "null", "~"]
        assert list(parsed) == list(json.loads(raw))  # key order survives


@pytest.mark.parametrize(
    "accept",
    [None, "*/*", "application/*", "application/yaml", "application/x-yaml", "text/yaml"],
)
async def test_yaml_document_without_a_preference_or_with_its_own_type_is_the_original_bytes(
    make_stack: StackFactory, accept: str | None
) -> None:
    async with make_stack() as stack:
        raw = yaml_contract()
        contract_id = await stored(stack, raw, "application/yaml")
        response = await fetch(stack, contract_id, accept)
        assert response.status_code == 200
        assert response.content == raw
        assert response.headers["content-type"] == "application/yaml"
        assert response.headers["vary"] == "Accept"


@pytest.mark.parametrize("stored_type", ["application/x-yaml", "text/yaml"])
async def test_every_yaml_alias_is_the_original_for_a_yaml_document(
    make_stack: StackFactory, stored_type: str
) -> None:
    async with make_stack() as stack:
        raw = yaml_contract()
        contract_id = await stored(stack, raw, stored_type)
        for accept in ("application/yaml", "application/x-yaml", "text/yaml"):
            response = await fetch(stack, contract_id, accept)
            assert response.status_code == 200
            assert response.content == raw
            assert response.headers["content-type"] == stored_type


async def test_json_document_with_its_own_type_is_the_original_bytes(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        raw = json_contract()
        contract_id = await stored(stack, raw, "application/json")
        for accept in (None, "application/json", "*/*"):
            response = await fetch(stack, contract_id, accept)
            assert response.status_code == 200
            assert response.content == raw
            assert response.headers["content-type"] == "application/json"
            assert response.headers["vary"] == "Accept"


async def test_quality_values_decide_between_the_original_and_the_conversion(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        raw = yaml_contract()
        contract_id = await stored(stack, raw, "application/yaml")
        prefers_json = await fetch(stack, contract_id, "application/yaml;q=0.2, application/json")
        assert prefers_json.headers["content-type"] == "application/json"
        prefers_yaml = await fetch(stack, contract_id, "application/json;q=0.2, application/yaml")
        assert prefers_yaml.content == raw
        # a wildcard and a tie both mean "the original"
        assert (await fetch(stack, contract_id, "text/html, */*;q=0.1")).content == raw


async def test_an_unsupported_accept_is_406_problem_json(make_stack: StackFactory) -> None:
    async with make_stack() as stack:
        contract_id = await stored(stack, yaml_contract(), "application/yaml")
        for accept in ("text/html", "text/html, image/png", "application/yaml;q=0", "garbage"):
            response = await fetch(stack, contract_id, accept)
            assert response.status_code == 406, (accept, response.text)
            assert response.headers["content-type"].startswith("application/problem+json")
            assert response.json()["type"] == "urn:dc:problem:not-acceptable"


async def test_authorization_is_answered_before_accept_is_looked_at(
    make_stack: StackFactory, fake_issuer: FakeIssuer
) -> None:
    async with make_stack() as stack:
        contract_id = await stored(stack, yaml_contract(), "application/yaml")
        url = f"/api/v1/contracts/contracts/{contract_id}/document"
        stranger = await stack.client.get(
            url, headers={**bearer(fake_issuer.mint("nobody")), "Accept": "text/html"}
        )
        assert stranger.status_code == 404
        anonymous = await stack.client.get(url, headers={"Accept": "text/html"})
        assert anonymous.status_code == 401
