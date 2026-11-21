"""Create a contract from a published template (CONT-02, D-38): copied, never linked.

Tracer: an editor in a Team folder starts a contract from a published Organization template; the
contract's document is a copy with the new id and the origin (template and version) is recorded
and enforced by the database.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from dc_odcs import load_yaml

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
TEMPLATES = "/api/v1/contracts/templates"
CONTRACTS = "/api/v1/contracts/contracts"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def v32_yaml() -> bytes:
    return (FIXTURES / "valid-v3.2.0.yaml").read_bytes()


async def make_template(
    stack: Any,
    folder_id: str,
    token: str | None = None,
    body: bytes | None = None,
    *,
    publish: bool = True,
    name: str = "Orders template",
) -> str:
    """Create a template draft and (by default) publish version 1; returns the template id."""
    token = token or stack.admin_token
    created = await stack.client.post(
        f"/api/v1/contracts/folders/{folder_id}/templates",
        params={"name": name},
        content=v32_yaml() if body is None else body,
        headers={**bearer(token), "Content-Type": "application/yaml"},
    )
    assert created.status_code == 201, created.text
    template_id = str(created.json()["template"]["id"])
    if publish:
        published = await stack.client.post(
            f"{TEMPLATES}/{template_id}/publish", headers=bearer(token)
        )
        assert published.status_code == 201, published.text
    return template_id


async def from_template(stack: Any, folder_id: str, token: str, payload: dict[str, Any]) -> Any:
    return await stack.client.post(
        f"/api/v1/contracts/folders/{folder_id}/contracts/from-template",
        json=payload,
        headers=bearer(token),
    )


async def get_document(stack: Any, token: str, contract_id: str, accept: str | None = None) -> Any:
    headers = bearer(token)
    if accept is not None:
        headers["Accept"] = accept
    return await stack.client.get(f"{CONTRACTS}/{contract_id}/document", headers=headers)


async def test_editor_creates_a_contract_from_an_org_template_and_the_origin_is_recorded(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        pm, pm_principal = stack.token("pm")
        stack.grant_sql(pm_principal, "policy_manager", chain.org)
        ed, ed_principal = stack.token("ed")
        stack.grant_sql(ed_principal, "team_editor", chain.team)
        template_id = await make_template(stack, chain.org, pm)

        created = await from_template(
            stack, chain.team, ed, {"template_id": template_id, "odcs_id": "urn:dc:orders:1"}
        )
        assert created.status_code == 201, created.text
        body = created.json()
        contract_id = body["contract"]["id"]
        assert created.headers["location"] == f"{CONTRACTS}/{contract_id}"
        assert body["contract"]["template"] == {"template_id": template_id, "version_no": 1}
        assert body["contract"]["odcs_id"] == "urn:dc:orders:1"
        assert body["contract"]["folder_id"] == chain.team
        assert body["contract"]["organization_id"] == chain.org
        assert body["structural_validation"]["valid"] is True

        # the document is the template's, with the new id, in the template's key order
        document = await get_document(stack, ed, contract_id)
        assert document.status_code == 200
        assert document.headers["content-type"] == "application/json"
        expected = load_yaml(v32_yaml().decode())
        expected["id"] = "urn:dc:orders:1"
        actual = json.loads(document.content)
        assert actual == expected
        assert list(actual) == list(load_yaml(v32_yaml().decode()))
        rendered = await get_document(stack, ed, contract_id, "application/yaml")
        assert rendered.status_code == 200
        assert rendered.headers["content-type"] == "application/yaml"
        assert load_yaml(rendered.content.decode()) == expected
        assert b"# Orders contract" not in rendered.content  # template comments are not carried

        metadata = await stack.client.get(f"{CONTRACTS}/{contract_id}", headers=bearer(ed))
        assert metadata.json()["template"] == {"template_id": template_id, "version_no": 1}

        row = sql(
            stack.contracts_db_url,
            "SELECT template_id, template_version FROM contract WHERE id = %s",
            contract_id,
        )[0]
        assert (str(row["template_id"]), row["template_version"]) == (template_id, 1)

        recorded = sql(
            stack.contracts_db_url,
            "SELECT after FROM outbox WHERE action = 'contract.created'",
        )
        assert len(recorded) == 1
        after = recorded[0]["after"]
        assert after["template_id"] == template_id
        assert after["template_version"] == 1
        text = json.dumps(recorded)
        for fragment in ("Records every order", "checkout", "costCentre", "schema"):
            assert fragment not in text


async def test_a_plain_contract_has_no_template_origin_and_its_event_says_so(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, v32_yaml())
        assert created.status_code == 201, created.text
        assert created.json()["contract"]["template"] is None
        event = sql(stack.contracts_db_url, "SELECT after FROM outbox")[0]["after"]
        assert event["template_id"] is None
        assert event["template_version"] is None


async def test_the_database_enforces_the_pair_and_the_existence_of_the_origin(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        template_id = await make_template(stack, chain.org)
        created = await stack.post(chain.team, v32_yaml())
        contract_id = created.json()["contract"]["id"]
        url = stack.contracts_db_url.replace("postgresql+psycopg://", "postgresql://")

        def update(query: LiteralString, *params: object) -> None:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(query, (*params, contract_id))

        with pytest.raises(psycopg.errors.CheckViolation):
            update("UPDATE contract SET template_id = %s WHERE id = %s", template_id)
        with pytest.raises(psycopg.errors.CheckViolation):
            update("UPDATE contract SET template_version = 1 WHERE id = %s")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            update(
                "UPDATE contract SET template_id = %s, template_version = 9 WHERE id = %s",
                template_id,
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            update(
                "UPDATE contract SET template_id = %s, template_version = 1 WHERE id = %s",
                str(uuid.uuid4()),
            )
        update(  # a real pair is accepted
            "UPDATE contract SET template_id = %s, template_version = 1 WHERE id = %s", template_id
        )


# ---------------------------------------------------------------------------------------------
# Task 3: the rules around create-from-template
# ---------------------------------------------------------------------------------------------

UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"


def assert_problem(response: Any, status: int, slug: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    assert body["status"] == status
    return body


def stable(response: Any) -> dict[str, Any]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return body


async def publish_next(stack: Any, template_id: str, token: str | None = None) -> None:
    """Change the draft and publish the next version."""
    token = token or stack.admin_token
    updated = await stack.client.put(
        f"{TEMPLATES}/{template_id}/draft",
        content=v32_yaml().replace(b"Payments Orders", b"Payments Orders v2"),
        headers={**bearer(token), "Content-Type": "application/yaml"},
    )
    assert updated.status_code == 200, updated.text
    published = await stack.client.post(f"{TEMPLATES}/{template_id}/publish", headers=bearer(token))
    assert published.status_code == 201, published.text


async def deprecate(stack: Any, template_id: str, version_no: int) -> None:
    response = await stack.client.post(
        f"{TEMPLATES}/{template_id}/versions/{version_no}/deprecate",
        headers=bearer(stack.admin_token),
    )
    assert response.status_code == 200, response.text


async def editor(stack: Any, team: str, name: str = "ed") -> str:
    token, principal = stack.token(name)
    stack.grant_sql(principal, "team_editor", team)
    return token


async def test_copy_not_link_later_template_changes_never_touch_the_contract(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        template_id = await make_template(stack, chain.org)
        ed = await editor(stack, chain.team)
        created = await from_template(
            stack, chain.team, ed, {"template_id": template_id, "odcs_id": "urn:dc:copy:1"}
        )
        assert created.status_code == 201, created.text
        contract_id = created.json()["contract"]["id"]
        before = await get_document(stack, ed, contract_id)

        await publish_next(stack, template_id)
        await deprecate(stack, template_id, 1)

        after = await get_document(stack, ed, contract_id)
        assert after.content == before.content
        metadata = await stack.client.get(f"{CONTRACTS}/{contract_id}", headers=bearer(ed))
        assert metadata.json()["template"] == {"template_id": template_id, "version_no": 1}
        assert metadata.json()["name"] == "Payments Orders"


async def test_the_default_version_is_the_latest_published_non_deprecated_one(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        template_id = await make_template(stack, chain.org)
        await publish_next(stack, template_id)
        await deprecate(stack, template_id, 2)
        ed = await editor(stack, chain.team)

        created = await from_template(
            stack, chain.team, ed, {"template_id": template_id, "odcs_id": "urn:dc:v:1"}
        )
        assert created.status_code == 201, created.text
        assert created.json()["contract"]["template"]["version_no"] == 1
        assert created.json()["contract"]["name"] == "Payments Orders"

        await deprecate(stack, template_id, 1)
        none_left = await from_template(
            stack, chain.team, ed, {"template_id": template_id, "odcs_id": "urn:dc:v:2"}
        )
        assert_problem(none_left, 404, "not-found")

        fresh = await make_template(stack, chain.project, name="two versions")
        await publish_next(stack, fresh)
        explicit = await from_template(
            stack,
            chain.team,
            ed,
            {"template_id": fresh, "template_version": 1, "odcs_id": "urn:dc:v:3"},
        )
        latest = await from_template(
            stack, chain.team, ed, {"template_id": fresh, "odcs_id": "urn:dc:v:4"}
        )
        assert explicit.json()["contract"]["name"] == "Payments Orders"
        assert latest.json()["contract"]["template"]["version_no"] == 2
        assert latest.json()["contract"]["name"] == "Payments Orders v2"


async def test_overrides_replace_top_level_fields_and_are_gated_like_any_contract(
    make_stack: MakeStack,
) -> None:
    async with make_stack(max_document_bytes=4000) as stack:
        chain = await stack.chain()
        template_id = await make_template(stack, chain.org)
        ed = await editor(stack, chain.team)

        created = await from_template(
            stack,
            chain.team,
            ed,
            {
                "template_id": template_id,
                "odcs_id": "urn:dc:o:1",
                "overrides": {"name": "Orders", "domain": "sales"},
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["contract"]["name"] == "Orders"
        assert created.json()["contract"]["domain"] == "sales"
        stored = json.loads(
            (await get_document(stack, ed, created.json()["contract"]["id"])).content
        )
        assert stored["name"] == "Orders"
        assert stored["domain"] == "sales"
        assert stored["id"] == "urn:dc:o:1"
        assert stored["schema"][0]["name"] == "orders"  # untouched fields are copied

        for index, overrides in enumerate(
            ({"apiVersion": "v3.1.0"}, {"kind": "X"}, {"id": "y"}, {"name": "ok", "id": "y"})
        ):
            rejected = await from_template(
                stack,
                chain.team,
                ed,
                {
                    "template_id": template_id,
                    "odcs_id": f"urn:dc:o:bad{index}",
                    "overrides": overrides,
                },
            )
            assert_problem(rejected, 422, "template-override-not-allowed")

        too_big = await from_template(
            stack,
            chain.team,
            ed,
            {
                "template_id": template_id,
                "odcs_id": "urn:dc:o:big",
                "overrides": {"description": {"purpose": "x" * 5000}},
            },
        )
        assert_problem(too_big, 413, "payload-too-large")

        # a JSON Schema violation is reported, never blocking (D-19)
        invalid = await from_template(
            stack,
            chain.team,
            ed,
            {
                "template_id": template_id,
                "odcs_id": "urn:dc:o:invalid",
                "overrides": {"tags": "not-a-list"},
            },
        )
        assert invalid.status_code == 201, invalid.text
        assert invalid.json()["structural_validation"]["valid"] is False
        assert invalid.json()["structural_validation"]["error_count"] >= 1


async def test_deprecated_foreign_and_unknown_templates_and_versions(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        sibling = await stack.team_in_org(chain.org, "sibling")
        template_id = await make_template(stack, chain.org)
        await publish_next(stack, template_id)
        foreign = await make_template(stack, other.org, name="foreign")
        sibling_template = await make_template(stack, sibling, name="sibling")
        ed = await editor(stack, chain.team)

        await deprecate(stack, template_id, 1)
        deprecated = await from_template(
            stack,
            chain.team,
            ed,
            {"template_id": template_id, "template_version": 1, "odcs_id": "urn:dc:d:1"},
        )
        assert_problem(deprecated, 422, "template-version-deprecated")

        unknown_template = await from_template(
            stack, chain.team, ed, {"template_id": UNKNOWN, "odcs_id": "urn:dc:d:2"}
        )
        foreign_template = await from_template(
            stack, chain.team, ed, {"template_id": foreign, "odcs_id": "urn:dc:d:3"}
        )
        sibling_denied = await from_template(
            stack, chain.team, ed, {"template_id": sibling_template, "odcs_id": "urn:dc:d:4"}
        )
        unknown_version = await from_template(
            stack,
            chain.team,
            ed,
            {"template_id": template_id, "template_version": 9, "odcs_id": "urn:dc:d:5"},
        )
        for denied in (unknown_template, foreign_template, sibling_denied, unknown_version):
            assert_problem(denied, 404, "not-found")
            assert stable(denied) == stable(unknown_template)


async def test_target_folder_rules_any_level_and_archive(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        template_id = await make_template(stack, chain.org)
        pe, pe_principal = stack.token("pe")
        stack.grant_sql(pe_principal, "project_editor", chain.project)
        at_project = await from_template(
            stack, chain.project, pe, {"template_id": template_id, "odcs_id": "urn:dc:f:1"}
        )
        assert at_project.status_code == 201, at_project.text

        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.project}/archive",
            headers=bearer(stack.admin_token),
        )
        assert archived.status_code == 200, archived.text
        response = await from_template(
            stack, chain.team, pe, {"template_id": template_id, "odcs_id": "urn:dc:f:2"}
        )
        assert_problem(response, 409, "folder-archived")


async def test_who_may_create_from_a_template(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        template_id = await make_template(stack, chain.org)
        pm, pm_principal = stack.token("pm")
        stack.grant_sql(pm_principal, "policy_manager", chain.org)
        consumer, consumer_principal = stack.token("consumer")
        stack.grant_sql(consumer_principal, "consumer", chain.org)
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        stranger, _ = stack.token("stranger")

        def payload(odcs_id: str) -> dict[str, Any]:
            return {"template_id": template_id, "odcs_id": odcs_id}

        for token in (pm, consumer, reader):
            denied = await from_template(stack, chain.team, token, payload("urn:dc:w:1"))
            assert_problem(denied, 403, "forbidden")
        hidden = await from_template(stack, chain.team, stranger, payload("urn:dc:w:2"))
        unknown_folder = await from_template(stack, UNKNOWN, stranger, payload("urn:dc:w:2"))
        assert hidden.status_code == 404
        assert stable(hidden) == stable(unknown_folder)
        anonymous = await stack.client.post(
            f"/api/v1/contracts/folders/{chain.team}/contracts/from-template",
            json=payload("urn:dc:w:3"),
        )
        assert anonymous.status_code == 401
        assert sql(stack.contracts_db_url, "SELECT count(*) AS n FROM contract")[0]["n"] == 0

        allowed = await from_template(stack, chain.team, stack.admin_token, payload("urn:dc:w:4"))
        assert allowed.status_code == 201, allowed.text


async def test_idempotency_the_same_odcs_id_twice_is_201_then_409_and_two_ids_are_two_contracts(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        template_id = await make_template(stack, chain.org)
        ed = await editor(stack, chain.team)

        first = await from_template(
            stack, chain.team, ed, {"template_id": template_id, "odcs_id": "urn:dc:i:1"}
        )
        again = await from_template(
            stack, chain.team, ed, {"template_id": template_id, "odcs_id": "urn:dc:i:1"}
        )
        assert first.status_code == 201, first.text
        assert_problem(again, 409, "duplicate-contract-id")
        second = await from_template(
            stack, chain.team, ed, {"template_id": template_id, "odcs_id": "urn:dc:i:2"}
        )
        assert second.status_code == 201, second.text
        assert first.json()["contract"]["id"] != second.json()["contract"]["id"]
        rows = sql(
            stack.contracts_db_url,
            "SELECT template_id, template_version FROM contract ORDER BY odcs_id",
        )
        assert [(str(r["template_id"]), r["template_version"]) for r in rows] == [
            (template_id, 1),
            (template_id, 1),
        ]
        events = sql(stack.contracts_db_url, "SELECT action FROM outbox ORDER BY id")
        # the rejected duplicate recorded nothing
        assert [e["action"] for e in events if e["action"].startswith("contract.")] == [
            "contract.created",
            "contract.created",
        ]


async def test_concurrency_two_creates_with_one_odcs_id_yield_one_201_and_one_409(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        template_id = await make_template(stack, chain.org)
        ed = await editor(stack, chain.team)
        payload = {"template_id": template_id, "odcs_id": "urn:dc:race:1"}

        first, second = await asyncio.gather(
            from_template(stack, chain.team, ed, payload),
            from_template(stack, chain.team, ed, payload),
        )
        assert sorted([first.status_code, second.status_code]) == [201, 409]
        loser = first if first.status_code == 409 else second
        assert_problem(loser, 409, "duplicate-contract-id")
        assert sql(stack.contracts_db_url, "SELECT count(*) AS n FROM contract")[0]["n"] == 1
        assert (
            sql(stack.contracts_db_url, "SELECT count(*) AS n FROM contract_version")[0]["n"] == 1
        )


async def test_invalid_bodies_are_422(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        template_id = await make_template(stack, chain.org)
        ed = await editor(stack, chain.team)
        bodies: list[dict[str, Any]] = [
            {"template_id": template_id},
            {"template_id": template_id, "odcs_id": ""},
            {"template_id": template_id, "odcs_id": "urn:dc:\x00:1"},
            {"template_id": template_id, "template_version": 0, "odcs_id": "urn:dc:b:1"},
            {"template_id": template_id, "odcs_id": "urn:dc:b:2", "folder_id": chain.team},
            {"template_id": "not-a-uuid", "odcs_id": "urn:dc:b:3"},
            {"template_id": template_id, "odcs_id": "urn:dc:b:4", "overrides": ["name"]},
        ]
        for body in bodies:
            response = await from_template(stack, chain.team, ed, body)
            assert response.status_code == 422, (body, response.text)
        # a lone surrogate or NUL inside an override never reaches the database
        nul = await from_template(
            stack,
            chain.team,
            ed,
            {
                "template_id": template_id,
                "odcs_id": "urn:dc:b:5",
                "overrides": {"name": "bad\x00name"},
            },
        )
        assert nul.status_code == 422, nul.text


async def test_the_odcs_id_bound_is_255_on_create_from_template(make_stack: MakeStack) -> None:
    """Quick 261120-lbz, D-02."""
    async with make_stack() as stack:
        chain = await stack.chain()
        pm, pm_principal = stack.token("pm")
        stack.grant_sql(pm_principal, "policy_manager", chain.org)
        ed, ed_principal = stack.token("ed")
        stack.grant_sql(ed_principal, "team_editor", chain.team)
        template_id = await make_template(stack, chain.org, pm)

        refused = await from_template(
            stack, chain.team, ed, {"template_id": template_id, "odcs_id": "x" * 256}
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["errors"][0]["pointer"] == "/body/odcs_id"
        accepted = await from_template(
            stack, chain.team, ed, {"template_id": template_id, "odcs_id": "x" * 255}
        )
        assert accepted.status_code == 201, accepted.text
