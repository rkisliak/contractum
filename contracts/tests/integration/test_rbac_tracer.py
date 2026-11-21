"""Tracer (RBAC-03, RBAC-06, RBAC-09): a role grant acts in its team and nowhere else.

role_assignments -> identity evaluator -> RemoteAuthorizer -> contracts create/read, end to end.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
IDENTITY_FOLDERS = "/api/v1/identity/folders"
CHECK = "/internal/v1/authz/check"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def v32_yaml(odcs_id: str) -> bytes:
    raw = (FIXTURES / "valid-v3.2.0.yaml").read_bytes()
    return raw.replace(b"urn:datacontract:payments:orders", odcs_id.encode())


def _unique() -> str:
    return f"urn:datacontract:rbac:{uuid.uuid4().hex[:12]}"


async def _get_contract(stack: Any, contract_id: str, token: str) -> Any:
    return await stack.client.get(
        f"/api/v1/contracts/contracts/{contract_id}", headers=bearer(token)
    )


async def test_team_editor_creates_in_its_team_and_is_invisible_elsewhere(
    make_stack: Callable[..., AbstractAsyncContextManager[Any]],
) -> None:
    async with make_stack() as stack:
        mine = await stack.chain("org-one")
        other = await stack.chain("org-two")
        token, principal = stack.token("ed")
        stack.grant_sql(principal, "team_editor", mine.team)

        created = await stack.post(mine.team, v32_yaml(_unique()), token=token)
        assert created.status_code == 201, created.text
        contract_id = created.json()["contract"]["id"]
        assert (await _get_contract(stack, contract_id, token)).status_code == 200

        elsewhere = await stack.post(other.team, v32_yaml(_unique()), token=token)
        assert elsewhere.status_code == 404
        assert elsewhere.json()["type"] == "urn:dc:problem:not-found"

        above = await stack.identity.get(f"{IDENTITY_FOLDERS}/{mine.org}", headers=bearer(token))
        assert above.status_code == 404
        own = await stack.identity.get(f"{IDENTITY_FOLDERS}/{mine.team}", headers=bearer(token))
        assert own.status_code == 200


async def test_team_reader_reads_but_creating_is_forbidden_not_hidden(
    make_stack: Callable[..., AbstractAsyncContextManager[Any]],
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed_token, ed = stack.token("ed")
        rd_token, rd = stack.token("rd")
        stack.grant_sql(ed, "team_editor", chain.team)
        stack.grant_sql(rd, "team_reader", chain.team)
        created = await stack.post(chain.team, v32_yaml(_unique()), token=ed_token)
        assert created.status_code == 201, created.text
        contract_id = created.json()["contract"]["id"]

        denied = await stack.post(chain.team, v32_yaml(_unique()), token=rd_token)
        assert denied.status_code == 403
        assert denied.json()["type"] == "urn:dc:problem:forbidden"
        assert (await _get_contract(stack, contract_id, rd_token)).status_code == 200


async def test_a_principal_without_grants_gets_404_for_every_folder_addressed_by_id(
    make_stack: Callable[..., AbstractAsyncContextManager[Any]],
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        token, _ = stack.token("stranger")
        for folder_id in (chain.org, chain.project, chain.team, stack.root_id):
            read = await stack.identity.get(
                f"{IDENTITY_FOLDERS}/{folder_id}", headers=bearer(token)
            )
            assert read.status_code == 404, folder_id
        created = await stack.post(chain.team, v32_yaml(_unique()), token=token)
        assert created.status_code == 404


async def test_policy_manager_reads_metadata_permission_but_never_content_or_create(
    make_stack: Callable[..., AbstractAsyncContextManager[Any]],
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed_token, ed = stack.token("ed")
        pm_token, pm = stack.token("pm")
        stack.grant_sql(ed, "team_editor", chain.team)
        stack.grant_sql(pm, "policy_manager", chain.org)
        created = await stack.post(chain.team, v32_yaml(_unique()), token=ed_token)
        assert created.status_code == 201, created.text
        contract_id = created.json()["contract"]["id"]

        denied = await stack.post(chain.team, v32_yaml(_unique()), token=pm_token)
        assert denied.status_code == 403
        # The metadata route needs contract:read-metadata only (D-36): 200 with content redacted.
        metadata = await _get_contract(stack, contract_id, pm_token)
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["content_redacted"] is True

        decision = await stack.identity.post(
            CHECK,
            json={"permission": "contract:read-metadata", "folder_id": chain.team},
            headers=bearer(pm_token),
        )
        assert decision.status_code == 200, decision.text
        body = decision.json()
        assert body["outcome"] == "allow"
        assert "contract:read-metadata" in body["permissions"]
        assert "contract:read" not in body["permissions"]


async def test_consumer_cannot_create_and_cannot_read_draft_content(
    make_stack: Callable[..., AbstractAsyncContextManager[Any]],
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed_token, ed = stack.token("ed")
        cs_token, cs = stack.token("cs")
        stack.grant_sql(ed, "team_editor", chain.team)
        stack.grant_sql(cs, "consumer", chain.team)
        created = await stack.post(chain.team, v32_yaml(_unique()), token=ed_token)
        assert created.status_code == 201, created.text
        contract_id = created.json()["contract"]["id"]

        assert (
            await stack.post(chain.team, v32_yaml(_unique()), token=cs_token)
        ).status_code == 403
        assert (await _get_contract(stack, contract_id, cs_token)).status_code == 404


async def test_global_admin_keeps_full_access(
    make_stack: Callable[..., AbstractAsyncContextManager[Any]],
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, v32_yaml(_unique()))
        assert created.status_code == 201, created.text
        contract_id = created.json()["contract"]["id"]
        assert (await _get_contract(stack, contract_id, stack.admin_token)).status_code == 200
