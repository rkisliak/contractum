"""Validating a stored draft on demand (plan 05-19; VAL-08, D-126, D-127, D-36).

``POST .../contracts/{contract_id}/drafts/{draft_id}/validate`` needs ``contract:read``: whoever may
read a draft may ask for its save-mode report without changing it or storing a run. A Policy Manager
holds ``contract:validate`` for their own uploads but no contract role, so stored drafts stay behind
``contract:read`` (T-5-48). An outage is 503 because the report was asked for explicitly.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"

NO_TMP_NAMES = '!this.properties.exists(p, p.name.startsWith("tmp_"))'


def doc(odcs_id: str, *names: str, version: str = "1.0.0") -> bytes:
    """A conformant ODCS v3.2.0 document with one table holding the named properties."""
    props = "".join(f"      - name: {n}\n        logicalType: string\n" for n in names)
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: Orders\nschema:\n  - name: orders\n    properties:\n{props}"
    ).encode()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_id() -> str:
    return f"urn:dc:draftval:{uuid.uuid4().hex}"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def no_tmp_rule() -> str:
    return json.dumps(
        {
            "target": "$.schema[*]",
            "condition": NO_TMP_NAMES,
            "severity": "error",
            "message": "no tmp_",
            "fixtures": {
                "pass": [{"document": table("id", "name")}],
                "fail": [{"document": table("tmp_id", "name")}],
            },
        }
    )


async def holder(stack: Any, role: str, folder: str) -> str:
    token, principal_id = stack.token(f"{role}-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal_id, role, folder)
    return str(token)


async def validate(stack: Any, contract: str, draft: str, token: str | None) -> Any:
    return await stack.client.post(
        f"{CONTRACTS}/{contract}/drafts/{draft}/validate",
        headers=bearer(token) if token else {},
    )


def draft_row(stack: Any, sql: Sql, draft: str) -> dict[str, Any]:
    return sql(
        stack.contracts_db_url,
        "SELECT revision, updated_at, content_sha256 FROM contract_version WHERE id = %s",
        draft,
    )[0]


def run_count(stack: Any, sql: Sql) -> int:
    return int(sql(stack.validation_db_url, "SELECT count(*) AS n FROM validation_run")[0]["n"])


# -- tracer ---------------------------------------------------------------------------------------


async def test_a_team_reader_validates_a_stored_draft_without_changing_it(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        await stack.mandatory_rule_set(chain.org)
        reader = await holder(stack, "team_reader", chain.team)
        contract, draft, _etag = await stack.create_contract(chain.team, doc(new_id(), "id", "id"))
        before = draft_row(stack, sql, draft)
        runs_before = run_count(stack, sql)

        response = await validate(stack, contract, draft, reader)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["draft_id"] == draft
        assert body["content_sha256"] == before["content_sha256"]
        assert body["engine_version"]
        assert body["schema_sha"]
        assert isinstance(body["pins"], list)
        report = body["report"]
        assert report["status"] == "completed"
        assert report["summary"]["publishable"] is False
        blocking = [f for f in report["findings"] if f["blocking"]]
        assert [(f["rule_key"], f["location"]["pointer"]) for f in blocking] == [
            ("unique-property-names", "/schema/0")
        ]
        after = draft_row(stack, sql, draft)
        assert after == before
        assert run_count(stack, sql) == runs_before


# -- who may ask ----------------------------------------------------------------------------------


async def test_a_policy_manager_without_a_contract_role_cannot_validate_a_stored_draft(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        pm = stack.policy_manager(chain.org)
        stranger = await holder(stack, "team_reader", await stack.team_in_org(chain.org, "other"))
        contract, draft, _etag = await stack.create_contract(chain.team, doc(new_id(), "id"))
        stack.validation_link.requests.clear()

        denied = await validate(stack, contract, draft, pm)
        unknown = await validate(stack, str(uuid.uuid4()), str(uuid.uuid4()), pm)
        hidden = await validate(stack, contract, draft, stranger)
        anonymous = await validate(stack, contract, draft, None)

        assert denied.status_code in (403, 404), denied.text
        # The denial is the answer for any unreadable draft, and validation was never asked.
        assert denied.status_code == unknown.status_code
        assert denied.json()["type"] == unknown.json()["type"]
        assert hidden.status_code == 404
        assert anonymous.status_code == 401
        assert stack.validation_link.requests == []


async def test_a_policy_manager_still_validates_their_own_upload_through_validation(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        pm = stack.policy_manager(chain.org)

        response = await stack.validation.post(
            f"/api/v1/validation/folders/{chain.team}/validate",
            content=doc(new_id(), "id"),
            headers={**bearer(pm), "Content-Type": "application/yaml"},
        )

        assert response.status_code == 200, response.text


# -- what can be validated ------------------------------------------------------------------------


async def test_a_published_or_unknown_draft_id_is_404(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_id(), "id"))
        published = await stack.publish(contract, draft, etag)
        assert published.status_code == 201, published.text

        done = await validate(stack, contract, draft, stack.admin_token)
        unknown = await validate(stack, contract, str(uuid.uuid4()), stack.admin_token)

        assert done.status_code == 404, done.text
        assert unknown.status_code == 404, unknown.text
        assert done.json()["type"] == "urn:dc:problem:not-found"


async def test_a_validation_outage_is_503_validation_unavailable(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, _etag = await stack.create_contract(chain.team, doc(new_id(), "id"))
        stack.validation_link.down = True

        response = await validate(stack, contract, draft, stack.admin_token)

        assert response.status_code == 503, response.text
        assert response.json()["type"] == "urn:dc:problem:validation-unavailable"


async def test_the_report_includes_the_findings_of_an_attached_rule_set(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        pm = stack.policy_manager(stack.root_id)
        rule_set = await stack.new_rule_set(
            stack.root_id, pm, {"no-tmp-names": no_tmp_rule()}, name="No tmp"
        )
        ed = await holder(stack, "team_editor", chain.team)
        contract, draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "tmp_id", "name"), token=ed
        )
        attached = await stack.client.put(
            f"{CONTRACTS}/{contract}/rule-sets/{rule_set}", json={}, headers=bearer(ed)
        )
        assert attached.status_code == 201, attached.text
        stack.validation_link.requests.clear()

        response = await validate(stack, contract, draft, ed)

        assert response.status_code == 200, response.text
        report = response.json()["report"]
        found = [f for f in report["findings"] if f["rule_key"] == "no-tmp-names"]
        assert len(found) == 1
        assert found[0]["source"] == "explicit"
        assert found[0]["blocking"] is True
        sent = json.loads(stack.validation_link.requests[0].content)
        assert [a["rule_set_id"] for a in sent["attachments"]] == [rule_set]
        assert sent["mode"] == "save"
