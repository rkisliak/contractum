"""How ``ValidationClient`` maps each answer of the validation service (fail-closed, D-131, D-161).

Every call is answered by an in-process ``httpx.MockTransport``, so the status mapping is pinned
without a network: a clean 200 is the body, a 404 is "not yours", a 401 is the caller's own 401,
and anything doubtful (unreadable body, wrong shape, saturation, a limit mismatch) is 503
``validation-unavailable``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from dc_auth.authorizer import Decision, FolderFacts, Outcome
from dc_contracts.settings import ContractsSettings
from dc_contracts.validation_client import (
    GateContext,
    ValidationClient,
    _path_id,
    _problem_type,
    _save_mode_run,
)
from dc_core.problems import ProblemError

pytestmark = pytest.mark.unit

BEARER = "caller-token"  # noqa: S105 - a test token, not a secret
FOLDER_ID = uuid.UUID("0b9f3d8e-6c1a-4f61-9a55-1f1d7f2a0c01")
RULE_SET_ID = uuid.UUID("5a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
RUN_ID = uuid.UUID("7c2d1e0f-3a4b-4c5d-9e6f-0a1b2c3d4e5f")
CONTRACT_ID = uuid.UUID("1e2d3c4b-5a69-4788-9a0b-c1d2e3f4a5b6")
DRAFT_ID = uuid.UUID("2f3e4d5c-6b7a-4899-8a1b-d2e3f4a5b6c7")
FACTS = FolderFacts(
    id=FOLDER_ID,
    kind="team",
    slug="team",
    path="root.org.project.team",
    ancestor_ids=(FOLDER_ID,),
    organization_id=None,
    archived=False,
)

Handler = Callable[[httpx.Request], httpx.Response]


def client_for(handler: Handler) -> ValidationClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://validation")
    return ValidationClient(http, ContractsSettings())


def answering(status: int, *, body: bytes = b"", headers: dict[str, str] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers)

    return handler


def answering_json(status: int, payload: Any) -> Handler:
    return answering(status, body=json.dumps(payload).encode())


def assert_unavailable(exc: pytest.ExceptionInfo[ProblemError]) -> None:
    assert exc.value.status == 503
    assert exc.value.slug == "validation-unavailable"


def ctx(**extra: Any) -> GateContext:
    return GateContext(
        contract_id=CONTRACT_ID, draft_id=DRAFT_ID, facts=FACTS, bearer=BEARER, **extra
    )


# --- _problem_type -------------------------------------------------------------------------------


def test_problem_type_reads_only_validation_urns() -> None:
    assert _problem_type(b"not json") is None
    assert _problem_type(b"[1, 2]") is None
    assert _problem_type(b'{"type": "https://example.test/x"}') is None
    assert _problem_type(b'{"type": "urn:dc:problem:payload-too-large"}') == (
        "urn:dc:problem:payload-too-large"
    )
    long_type = "urn:dc:problem:" + "x" * 200
    assert _problem_type(json.dumps({"type": long_type}).encode()) == long_type[:100]


# --- run -----------------------------------------------------------------------------------------


async def test_run_without_link_facts_sends_no_ref_facts_and_returns_the_answer() -> None:
    seen: dict[str, Any] = {}
    answer = {"report": {"summary": {"publishable": True}}, "run_id": str(RUN_ID)}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=answer)

    result = await client_for(handler).run(ctx(), b"doc", "application/yaml", "save")

    assert result == answer
    assert "ref_facts" not in seen["payload"]
    assert seen["payload"]["mode"] == "save"
    assert seen["auth"] == f"Bearer {BEARER}"


async def test_run_with_link_facts_wraps_them_under_contracts() -> None:
    seen: dict[str, Any] = {}
    facts = {"urn:x": {"state": "published"}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"report": {}})

    await client_for(handler).run(ctx(ref_facts=facts), b"doc", "application/yaml", "publish")

    assert seen["payload"]["ref_facts"] == {"contracts": facts}


async def test_run_maps_an_unrecognised_403_to_forbidden() -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering_json(403, {"type": "urn:dc:problem:forbidden"})).run(
            ctx(), b"doc", "application/yaml", "publish"
        )
    assert exc.value.status == 403
    assert exc.value.slug == "forbidden"


# --- available_rule_set --------------------------------------------------------------------------


async def test_available_rule_set_returns_the_body_or_none() -> None:
    body = {"rule_set": {"id": str(RULE_SET_ID)}}
    found = await client_for(answering_json(200, body)).available_rule_set(
        BEARER, FOLDER_ID, RULE_SET_ID
    )
    assert found == body
    missing = await client_for(answering(404)).available_rule_set(BEARER, FOLDER_ID, RULE_SET_ID)
    assert missing is None


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2, 3]"])
async def test_available_rule_set_with_an_unreadable_200_is_unavailable(content: bytes) -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(200, body=content)).available_rule_set(
            BEARER, FOLDER_ID, RULE_SET_ID
        )
    assert_unavailable(exc)


# --- get_run -------------------------------------------------------------------------------------


async def test_get_run_returns_a_run_with_a_report_and_none_for_a_404() -> None:
    run = {"run_id": str(RUN_ID), "report": {"summary": {}}}
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["contract_id"] = request.url.params["contract_id"]
        return httpx.Response(200, json=run)

    assert await client_for(handler).get_run(RUN_ID, CONTRACT_ID, BEARER) == run
    assert seen["contract_id"] == str(CONTRACT_ID)
    assert await client_for(answering(404)).get_run(RUN_ID, CONTRACT_ID, BEARER) is None


async def test_get_run_passes_the_callers_401_through() -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(401)).get_run(RUN_ID, CONTRACT_ID, BEARER)
    assert exc.value.status == 401
    assert exc.value.slug == "unauthenticated"


@pytest.mark.parametrize(
    "content", [b"{not json", b'{"run_id": "x"}', b'{"report": "not an object"}']
)
async def test_get_run_with_an_unreadable_or_reportless_200_is_unavailable(content: bytes) -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(200, body=content)).get_run(RUN_ID, CONTRACT_ID, BEARER)
    assert_unavailable(exc)


async def test_get_run_keeps_a_numeric_retry_after_on_a_5xx() -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(503, headers={"Retry-After": "7"})).get_run(
            RUN_ID, CONTRACT_ID, BEARER
        )
    assert_unavailable(exc)
    assert exc.value.headers == {"Retry-After": "7"}


# --- rule_set_folder -----------------------------------------------------------------------------


async def test_rule_set_folder_reads_the_folder_of_the_rule_set() -> None:
    body = {"rule_set": {"folder_id": str(FOLDER_ID)}}
    assert await client_for(answering_json(200, body)).rule_set_folder(RULE_SET_ID, BEARER) == (
        FOLDER_ID
    )


@pytest.mark.parametrize("status", [403, 404])
async def test_rule_set_folder_is_none_for_a_rule_set_the_caller_cannot_read(status: int) -> None:
    assert await client_for(answering(status)).rule_set_folder(RULE_SET_ID, BEARER) is None


async def test_rule_set_folder_is_none_for_a_platform_rule_set() -> None:
    body = {"rule_set": {"folder_id": None}}
    assert await client_for(answering_json(200, body)).rule_set_folder(RULE_SET_ID, BEARER) is None


async def test_rule_set_folder_with_a_5xx_is_unavailable() -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(502)).rule_set_folder(RULE_SET_ID, BEARER)
    assert_unavailable(exc)


async def test_rule_set_folder_passes_the_callers_401_through() -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(401)).rule_set_folder(RULE_SET_ID, BEARER)
    assert exc.value.status == 401


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[]",
        b'{"rule_set": "x"}',
        b'{"rule_set": {"id": "no folder key"}}',
        b'{"rule_set": {"folder_id": "not-a-uuid"}}',
    ],
)
async def test_rule_set_folder_with_a_doubtful_200_is_unavailable(content: bytes) -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(200, body=content)).rule_set_folder(RULE_SET_ID, BEARER)
    assert_unavailable(exc)


# --- dry_run and drift (_batch) ------------------------------------------------------------------


async def test_dry_run_posts_the_page_and_returns_the_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    items = [{"contract_id": str(CONTRACT_ID)}]
    answer = await client_for(handler).dry_run(RULE_SET_ID, "draft", FOLDER_ID, items, BEARER)

    assert answer == {"results": []}
    assert seen["path"] == f"/internal/v1/validation/rule-sets/{RULE_SET_ID}/dry-run"
    assert seen["payload"] == {
        "version": "draft",
        "scope_folder_id": str(FOLDER_ID),
        "items": items,
    }


@pytest.mark.parametrize("content", [b"{not json", b"[1]"])
async def test_batch_with_an_unreadable_200_is_unavailable(content: bytes) -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(200, body=content)).drift(FOLDER_ID, [], BEARER)
    assert_unavailable(exc)
    assert exc.value.detail == "Contract validation answered with an unreadable response."


async def test_batch_passes_the_callers_401_through() -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(401)).drift(FOLDER_ID, [], BEARER)
    assert exc.value.status == 401


async def test_batch_passes_a_known_identity_403_through() -> None:
    body = {"type": "urn:dc:problem:interactive-session-required"}
    with pytest.raises(ProblemError) as exc:
        await client_for(answering_json(403, body)).drift(FOLDER_ID, [], BEARER)
    assert exc.value.status == 403
    assert exc.value.slug == "interactive-session-required"


async def test_batch_404_is_the_uniform_not_found() -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(404)).drift(FOLDER_ID, [], BEARER)
    assert exc.value.status == 404
    assert exc.value.slug == "not-found"


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_batch_saturation_or_failure_is_unavailable_with_retry_after(status: int) -> None:
    with pytest.raises(ProblemError) as exc:
        await client_for(answering(status, headers={"Retry-After": "3"})).drift(
            FOLDER_ID, [], BEARER
        )
    assert_unavailable(exc)
    assert exc.value.headers == {"Retry-After": "3"}


@pytest.mark.parametrize("status", [400, 403, 413, 422])
async def test_batch_any_other_4xx_is_a_limit_mismatch_and_unavailable(status: int) -> None:
    body = {"type": "urn:dc:problem:batch-too-large"}
    with pytest.raises(ProblemError) as exc:
        await client_for(answering_json(status, body)).drift(FOLDER_ID, [], BEARER)
    assert_unavailable(exc)
    assert exc.value.detail == "Contract validation could not process the request."
    assert exc.value.headers is None


async def test_batch_transport_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProblemError) as exc:
        await client_for(handler).drift(FOLDER_ID, [], BEARER)
    assert_unavailable(exc)


# --- _save_mode_run ------------------------------------------------------------------------------


def fake_request(*, folder: FolderFacts | None, client: ValidationClient | None) -> Any:
    state = SimpleNamespace() if client is None else SimpleNamespace(validation_client=client)
    return SimpleNamespace(
        app=SimpleNamespace(state=state),
        state=SimpleNamespace(decision=Decision(outcome=Outcome.ALLOW, folder=folder)),
    )


@pytest.mark.parametrize("configured", ["no-folder", "no-client"])
async def test_save_mode_run_without_folder_facts_or_client_is_unavailable(
    configured: str,
) -> None:
    validation = client_for(answering(200))
    request = (
        fake_request(folder=None, client=validation)
        if configured == "no-folder"
        else fake_request(folder=FACTS, client=None)
    )
    with pytest.raises(ProblemError) as exc:
        await _save_mode_run(request, CONTRACT_ID, DRAFT_ID, b"doc", "application/yaml")
    assert_unavailable(exc)
    assert exc.value.detail == "Contract validation is not configured."


def test_a_path_id_is_the_canonical_text_of_a_uuid() -> None:
    assert _path_id(FOLDER_ID) == "0b9f3d8e-6c1a-4f61-9a55-1f1d7f2a0c01"
    assert _path_id(uuid.UUID("0B9F3D8E6C1A4F619A551F1D7F2A0C01")) == str(FOLDER_ID)


@pytest.mark.parametrize(
    "value",
    [
        "../../internal/v1/audit",
        "0b9f3d8e-6c1a-4f61-9a55-1f1d7f2a0c01/../x",
        "0b9f3d8e-6c1a-4f61-9a55-1f1d7f2a0c01?x=1",
        "0B9F3D8E-6C1A-4F61-9A55-1F1D7F2A0C01",
        "",
    ],
)
def test_anything_but_a_canonical_uuid_never_reaches_a_validation_path(value: str) -> None:
    with pytest.raises(ValueError, match="canonical UUID"):
        _path_id(cast(uuid.UUID, value))
