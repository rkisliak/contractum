"""Governance settings at runtime in contracts (quick 261120-0ye, D-35, D-36): the default ODCS
apiVersion of a new contract and the optional or required reason of unpublish and deprecate.

A stand-in for identity's runtime route serves the stored ``governance`` section
(``governance_client``), installed on ``app.state.settings_client``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from dc_core.platform_settings import RuntimeSettingsClient, normalise_section
from dc_odcs import load_yaml

pytestmark = pytest.mark.integration

CONTRACTS = "/api/v1/contracts/contracts"
MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def governance_client(value: dict[str, Any] | None, *, down: bool = False) -> RuntimeSettingsClient:
    section = normalise_section("governance", value or {})

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("identity is unreachable", request=request)
        return httpx.Response(200, json={"sections": {"governance": section}})

    return RuntimeSettingsClient(
        "http://identity:8000", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def sunset() -> str:
    return (datetime.now(UTC) + timedelta(days=60)).date().isoformat()


async def start_contract(stack: Any, folder: str, token: str) -> dict[str, Any]:
    response = await stack.client.post(
        f"/api/v1/contracts/folders/{folder}/contracts/starter",
        json={"odcs_id": f"urn:dc:gov:{uuid.uuid4().hex}", "name": "Orders"},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def starter_api_version(stack: Any, folder: str, token: str) -> str:
    body = await start_contract(stack, folder, token)
    document = await stack.client.get(
        f"{CONTRACTS}/{body['contract']['id']}/drafts/{body['draft']['id']}/document",
        headers=bearer(token),
    )
    assert document.status_code == 200
    return str(load_yaml(document.content.decode())["apiVersion"])


async def editor(stack: Any, folder: str) -> str:
    token, principal = stack.token(f"ed-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "team_editor", folder)
    return token


# --- G2: the default ODCS apiVersion -------------------------------------------------------------


async def test_the_starter_uses_the_stored_default_api_version(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)
        assert await starter_api_version(stack, chain.team, ed) == "v3.2.0"  # nothing stored
        stack.contracts_app.state.settings_client = governance_client(
            {"default_odcs_api_version": "v3.1.0"}
        )
        assert await starter_api_version(stack, chain.team, ed) == "v3.1.0"


async def test_an_unsupported_stored_version_falls_back_to_the_env_default(
    make_stack: MakeStack,
) -> None:
    async with make_stack(
        supported_odcs_versions=frozenset({"v3.2.0"}), default_odcs_api_version="v3.2.0"
    ) as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)
        stack.contracts_app.state.settings_client = governance_client(
            {"default_odcs_api_version": "v3.1.0"}  # vendored, but this deployment does not take it
        )
        assert await starter_api_version(stack, chain.team, ed) == "v3.2.0"


async def test_the_env_default_applies_when_identity_cannot_answer(make_stack: MakeStack) -> None:
    async with make_stack(default_odcs_api_version="v3.1.0") as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)
        stack.contracts_app.state.settings_client = governance_client(None, down=True)
        assert await starter_api_version(stack, chain.team, ed) == "v3.1.0"


# --- G3: the lifecycle reason --------------------------------------------------------------------


async def two_versions(stack: Any) -> tuple[str, dict[str, str]]:
    chain = await stack.chain()
    return await stack.publish_versions(chain.team, ["1.0.0", "1.1.0"])


def outbox_actions(stack: Any, sql: Sql) -> list[str]:
    rows = sql(stack.contracts_db_url, "SELECT action FROM outbox ORDER BY id")
    return [r["action"] for r in rows]


async def unpublish(stack: Any, contract: str, ref: str, body: Any = None) -> httpx.Response:
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    return await stack.client.post(
        f"{CONTRACTS}/{contract}/versions/{ref}/unpublish",
        headers=bearer(stack.admin_token),
        **kwargs,
    )


async def deprecate(stack: Any, contract: str, ref: str, body: dict[str, Any]) -> httpx.Response:
    return await stack.client.post(
        f"{CONTRACTS}/{contract}/versions/{ref}/deprecate",
        json=body,
        headers=bearer(stack.admin_token),
    )


async def test_with_the_requirement_on_a_missing_reason_is_422_with_no_event_and_no_write(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, _ = await two_versions(stack)
        stack.contracts_app.state.settings_client = governance_client(
            {"require_lifecycle_reason": True}
        )
        before = outbox_actions(stack, sql)

        refused = await unpublish(stack, contract, "1.0.0")
        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:lifecycle-reason-required"
        assert refused.json()["errors"][0]["pointer"] == "/reason"
        also = await deprecate(stack, contract, "1.0.0", {"sunset_date": sunset()})
        assert also.status_code == 422
        assert also.json()["type"] == "urn:dc:problem:lifecycle-reason-required"
        blank = await unpublish(stack, contract, "1.0.0", {"reason": "   "})
        assert blank.status_code == 422  # a blank reason is no reason
        assert outbox_actions(stack, sql) == before
        state = sql(
            stack.contracts_db_url,
            "SELECT semver, state, deprecated_at FROM contract_version ORDER BY semver",
        )
        assert [(r["semver"], r["state"], r["deprecated_at"]) for r in state] == [
            ("1.0.0", "published", None),
            ("1.1.0", "published", None),
        ]


async def test_a_reason_is_recorded_in_the_event_and_carried_by_the_deprecated_notice(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await stack.publish_versions(chain.team, ["1.0.0", "1.1.0"])
        token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "consumer", chain.team)
        card = await stack.create_card(chain.team, token=token)
        assert (await stack.subscribe(card["id"], contract, "1.0.0")).status_code == 201
        stack.contracts_app.state.settings_client = governance_client(
            {"require_lifecycle_reason": True}
        )

        done = await deprecate(
            stack, contract, "1.0.0", {"sunset_date": sunset(), "reason": "  Use 1.1.0  "}
        )
        assert done.status_code == 200, done.text
        (event,) = sql(
            stack.contracts_db_url,
            "SELECT after FROM outbox WHERE action = 'contract.version_deprecated'",
        )
        assert event["after"]["reason"] == "Use 1.1.0"
        assert await stack.pump_notices() == 1
        (notice,) = sql(
            stack.contracts_db_url,
            "SELECT payload FROM card_notice WHERE event_type = 'contract.version.deprecated'",
        )
        assert notice["payload"]["reason"] == "Use 1.1.0"

        # The lifecycle guards still decide after the reason check: 1.0.0 is the Latest.
        guarded = await unpublish(stack, contract, "1.0.0", {"reason": "retired"})
        assert guarded.status_code == 409
        assert guarded.json()["type"] == "urn:dc:problem:latest-version-protected"


async def test_an_unpublish_with_a_reason_records_it(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        contract, _ = await two_versions(stack)
        stack.contracts_app.state.settings_client = governance_client(
            {"require_lifecycle_reason": True}
        )
        done = await unpublish(stack, contract, "1.1.0", {"reason": "Superseded"})
        assert done.status_code == 200, done.text
        assert done.json()["lifecycle"] == "unpublished"
        (event,) = sql(
            stack.contracts_db_url,
            "SELECT after FROM outbox WHERE action = 'contract.version_unpublished'",
        )
        assert event["after"]["reason"] == "Superseded"


async def test_with_the_requirement_off_todays_calls_still_work(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, _ = await two_versions(stack)
        stack.contracts_app.state.settings_client = governance_client(
            {"require_lifecycle_reason": False}
        )
        dep = await deprecate(stack, contract, "1.0.0", {"sunset_date": sunset()})
        assert dep.status_code == 200, dep.text
        (event,) = sql(
            stack.contracts_db_url,
            "SELECT after FROM outbox WHERE action = 'contract.version_deprecated'",
        )
        assert "reason" not in event["after"]
        done = await unpublish(stack, contract, "1.1.0")  # no body at all
        assert done.status_code == 200, done.text
        extra = await unpublish(stack, contract, "1.1.0", {"reason": "x", "other": 1})
        assert extra.status_code == 422  # extra fields are refused


async def test_the_env_requirement_applies_when_nothing_is_stored(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(require_lifecycle_reason=True) as stack:
        contract, _ = await two_versions(stack)
        stack.contracts_app.state.settings_client = governance_client(None, down=True)
        refused = await unpublish(stack, contract, "1.0.0")
        assert refused.status_code == 422
        assert refused.json()["type"] == "urn:dc:problem:lifecycle-reason-required"
