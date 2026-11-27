"""A Policy Manager sees contract metadata but never content (RBAC-06, D-36, T-02-35 to T-02-39)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
CONTRACTS = "/api/v1/contracts/contracts"
UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
CONTENT_FIELDS = ("api_version", "domain", "tags", "media_type", "content_sha256", "size_bytes")

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(
    odcs_id: str,
    name: str,
    *,
    domain: str | None = None,
    tags: list[str] | None = None,
    purpose: str | None = None,
) -> bytes:
    lines = [
        "apiVersion: v3.2.0",
        "kind: DataContract",
        f"id: {odcs_id}",
        'version: "1.0.0"',
        "status: draft",
        f"name: {name}",
    ]
    if domain is not None:
        lines.append(f"domain: {domain}")
    if tags:
        lines.append(f"tags: [{', '.join(tags)}]")
    if purpose is not None:
        lines += ["description:", f"  purpose: {purpose}"]
    return ("\n".join(lines) + "\n").encode()


async def create(stack: Any, team: str, body: bytes) -> str:
    created = await stack.post(team, body)
    assert created.status_code == 201, created.text
    return str(created.json()["contract"]["id"])


async def get(stack: Any, token: str, contract_id: str, suffix: str = "") -> Any:
    return await stack.client.get(f"{CONTRACTS}/{contract_id}{suffix}", headers=bearer(token))


async def listing(stack: Any, token: str, **params: Any) -> Any:
    return await stack.client.get(CONTRACTS, params=params, headers=bearer(token))


def ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def stable(response: Any) -> tuple[int, str | None, bytes]:
    """Status, Content-Type and the body without per-request fields."""
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return (
        response.status_code,
        response.headers.get("content-type"),
        repr(sorted(body.items())).encode(),
    )


async def test_policy_manager_reads_metadata_but_never_content(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        tag = uuid.uuid4().hex[:8]
        contract = await create(
            stack,
            chain.team,
            doc(f"urn:dc:pm:{tag}", f"Orders {tag}", domain="sales", tags=["pii"], purpose="p"),
        )
        pm, pm_principal = stack.token("pm")
        stack.grant_sql(pm_principal, "policy_manager", chain.org)
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)

        redacted = await get(stack, pm, contract)
        assert redacted.status_code == 200, redacted.text
        body = redacted.json()
        assert body["id"] == contract
        assert body["folder_id"] == chain.team
        assert body["organization_id"] == chain.org
        assert body["odcs_id"] == f"urn:dc:pm:{tag}"
        assert body["notation"]
        assert body["name"] == f"Orders {tag}"
        assert body["status"] == "draft"
        assert body["version"] == {
            "number": 1,
            "state": body["version"]["state"],
            "deprecated": False,
        }
        for populated in ("created_by", "created_at", "updated_by", "updated_at"):
            assert body[populated]
        for field in CONTENT_FIELDS:
            assert body[field] is None, field
        assert body["content_redacted"] is True

        # the document is a 404 byte-identical to the one for an unknown contract
        denied = await get(stack, pm, contract, "/document")
        unknown = await get(stack, pm, UNKNOWN, "/document")
        assert denied.status_code == 404
        assert stable(denied) == stable(unknown)
        assert denied.headers["content-type"] == unknown.headers["content-type"]

        # the same shape for the metadata route of an unknown contract: indistinguishable 404
        assert (await get(stack, pm, UNKNOWN)).status_code == 404

        for token in (stack.admin_token, reader):
            full = await get(stack, token, contract)
            assert full.status_code == 200, full.text
            full_body = full.json()
            assert full_body["content_redacted"] is False
            for field in CONTENT_FIELDS:
                assert full_body[field] is not None, field
            assert full_body["domain"] == "sales"
            assert full_body["tags"] == ["pii"]
            assert (await get(stack, token, contract, "/document")).status_code == 200

        # RBAC-06: the same Policy Manager still manages templates at their folder
        created = await stack.client.post(
            f"/api/v1/contracts/folders/{chain.org}/templates",
            params={"name": "Starter"},
            content=(FIXTURES / "valid-v3.2.0.yaml").read_bytes(),
            headers={**bearer(pm), "Content-Type": "application/yaml"},
        )
        assert created.status_code == 201, created.text
        template_id = created.json()["template"]["id"]
        published = await stack.client.post(
            f"/api/v1/contracts/templates/{template_id}/publish", headers=bearer(pm)
        )
        assert published.status_code in (200, 201), published.text
        assert (await get(stack, pm, contract, "/document")).status_code == 404


async def test_list_shows_metadata_rows_and_never_other_organizations(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        org_a = await stack.chain("orga")
        org_b = await stack.chain("orgb")
        t2 = await stack.team_in_org(org_a.org, "second")
        tag = uuid.uuid4().hex[:8]
        c1 = await create(
            stack,
            org_a.team,
            doc(
                f"urn:dc:pm:{tag}:1",
                f"Alpha {tag}",
                domain="sales",
                tags=["pii"],
                purpose="secret pricing model",
            ),
        )
        c2 = await create(stack, t2, doc(f"urn:dc:pm:{tag}:2", f"Beta {tag}"))
        c3 = await create(
            stack,
            org_b.team,
            doc(f"urn:dc:pm:{tag}:3", f"Gamma {tag}", domain="sales", tags=["pii"]),
        )
        pm, principal = stack.token("pm")
        stack.grant_sql(principal, "policy_manager", org_a.org)

        page = await listing(stack, pm)
        assert ids(page) == sorted([c1, c2])
        for item in page.json()["items"]:
            assert item["content_redacted"] is True
            for field in CONTENT_FIELDS:
                assert item[field] is None, field
        assert ids(await listing(stack, pm, name=f"Alpha {tag}")) == [c1]
        assert ids(await listing(stack, pm, odcs_id=f"urn:dc:pm:{tag}:2")) == [c2]
        assert ids(await listing(stack, pm, status="draft")) == sorted([c1, c2])
        assert ids(await listing(stack, pm, q="Alph")) == [c1]
        assert ids(await listing(stack, pm, q=tag)) == sorted([c1, c2])

        # filter oracle: content-derived predicates never match a metadata-only row
        assert ids(await listing(stack, pm, domain="sales")) == []
        assert ids(await listing(stack, pm, tags="pii")) == []
        assert ids(await listing(stack, pm, q="pricing")) == []
        assert ids(await listing(stack, pm, domain="sales", name=f"Alpha {tag}")) == []
        # ... while the admin, who reads the content in full, does match
        assert c1 in ids(await listing(stack, stack.admin_token, domain="sales"))
        assert c1 in ids(await listing(stack, stack.admin_token, tags="pii"))
        assert ids(await listing(stack, stack.admin_token, q="pricing")) == [c1]

        # organization isolation (RBAC-07, RBAC-08): org B is invisible to every filter
        for params in ({}, {"name": f"Gamma {tag}"}, {"q": tag}, {"status": "draft"}):
            assert c3 not in ids(await listing(stack, pm, **params))
        assert ids(await listing(stack, pm, folder_id=org_b.org, include_subtree=True)) == []


async def test_adjacency_contract_role_adds_content_and_projects_scope_the_rows(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        org = await stack.chain("orga")
        sibling_project_team = await stack.team_in_org(org.org, "sibling")
        tag = uuid.uuid4().hex[:8]
        c1 = await create(
            stack,
            org.team,
            doc(f"urn:dc:adj:{tag}:1", f"One {tag}", domain="sales", purpose="secret pricing"),
        )
        c2 = await create(stack, sibling_project_team, doc(f"urn:dc:adj:{tag}:2", f"Two {tag}"))

        both, both_principal = stack.token("pm-reader")
        stack.grant_sql(both_principal, "policy_manager", org.org)
        stack.grant_sql(both_principal, "team_reader", org.team)
        page = await listing(stack, both)
        by_id = {item["id"]: item for item in page.json()["items"]}
        assert set(by_id) == {c1, c2}
        assert by_id[c1]["content_redacted"] is False
        assert by_id[c1]["domain"] == "sales"
        assert by_id[c2]["content_redacted"] is True
        assert ids(await listing(stack, both, domain="sales")) == [c1]
        assert ids(await listing(stack, both, q="pricing")) == [c1]
        assert (await get(stack, both, c1, "/document")).status_code == 200
        assert (await get(stack, both, c2, "/document")).status_code == 404

        # a Policy Manager at a Project sees its own teams' contracts, not a sibling Project's
        pm, pm_principal = stack.token("pm-project")
        stack.grant_sql(pm_principal, "policy_manager", org.project)
        assert ids(await listing(stack, pm)) == [c1]
        assert (await get(stack, pm, c2)).status_code == 404


async def test_empty_policy_manager_scope_returns_an_empty_page(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        org = await stack.chain("orga")
        other_team = await stack.team_in_org(org.org, "other")
        await create(stack, org.team, doc(f"urn:dc:empty:{uuid.uuid4().hex[:8]}", "Elsewhere"))
        pm, principal = stack.token("pm-empty")
        stack.grant_sql(principal, "policy_manager", other_team)
        response = await listing(stack, pm)
        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None}


async def test_ordering_of_mixed_visibility_pages_is_by_id_without_gaps(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        org = await stack.chain("orga")
        visible_team = org.team
        hidden_team = await stack.team_in_org(org.org, "meta")
        tag = uuid.uuid4().hex[:8]
        created: list[str] = []
        for n in range(5):
            team = visible_team if n in (0, 2, 4) else hidden_team
            created.append(await create(stack, team, doc(f"urn:dc:ord:{tag}:{n}", f"N{n} {tag}")))
        token, principal = stack.token("pm-order")
        stack.grant_sql(principal, "policy_manager", org.org)
        stack.grant_sql(principal, "team_reader", visible_team)

        seen: list[str] = []
        flags: dict[str, bool] = {}
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            response = await listing(stack, token, **params)
            assert response.status_code == 200, response.text
            pages += 1
            for item in response.json()["items"]:
                seen.append(item["id"])
                flags[item["id"]] = item["content_redacted"]
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break
        assert pages == 3
        assert seen == sorted(created)
        assert len(set(seen)) == 5
        assert [flags[c] for c in created] == [False, True, False, True, False]
