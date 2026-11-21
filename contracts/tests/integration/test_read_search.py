"""Tracer: the admin lists and searches by every D-12 filter; a stranger gets an empty page."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
from dc_auth.testing import FakeIssuer

pytestmark = pytest.mark.integration

LIST = "/api/v1/contracts/contracts"
StackFactory = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def contract_doc(
    odcs_id: str,
    name: str | None,
    *,
    domain: str | None = None,
    tags: tuple[str, ...] = (),
    status: str = "draft",
    purpose: str | None = None,
) -> bytes:
    lines = [
        "apiVersion: v3.2.0",
        "kind: DataContract",
        f"id: {odcs_id}",
        'version: "1.0.0"',
        f"status: {status}",
    ]
    if name is not None:
        lines.append(f"name: {name}")
    if domain is not None:
        lines.append(f"domain: {domain}")
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    if purpose is not None:
        lines.append("description:")
        lines.append(f"  purpose: {purpose}")
    return ("\n".join(lines) + "\n").encode("utf-8")


async def seed(stack: Any, team: str, odcs_id: str, name: str | None, **fields: Any) -> str:
    created = await stack.post(team, contract_doc(odcs_id, name, **fields))
    assert created.status_code == 201, created.text
    return str(created.json()["contract"]["id"])


async def listing(stack: Any, params: Any = None, token: str | None = None) -> httpx.Response:
    return await stack.client.get(LIST, params=params, headers=bearer(token or stack.admin_token))


def odcs_ids(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["odcs_id"] for item in response.json()["items"]]


async def test_admin_lists_and_searches_by_every_filter_and_a_stranger_gets_an_empty_page(
    make_stack: StackFactory, fake_issuer: FakeIssuer
) -> None:
    async with make_stack() as stack:
        org = await stack.folder(stack.root_id, f"o{uuid.uuid4().hex[:10]}")
        project = await stack.folder(org, "proj")
        t1 = await stack.folder(project, "t1")
        t2 = await stack.folder(project, "t2")
        tag = uuid.uuid4().hex[:8]
        c1, c2, c3 = (f"urn:dc:read:{tag}:{n}" for n in (1, 2, 3))
        await seed(stack, t1, c1, "Orders", domain="sales", tags=("pii", "finance"))
        await seed(stack, t1, c2, "Order Lines", domain="sales", tags=("finance",))
        await seed(stack, t2, c3, "Customers", purpose="customer master data")

        # q: case-insensitive over name, ODCS id and description
        assert odcs_ids(await listing(stack, {"q": "order"})) == [c1, c2]
        assert odcs_ids(await listing(stack, {"q": "MASTER"})) == [c3]
        assert odcs_ids(await listing(stack, {"q": f"{tag}:2"})) == [c2]

        # folder, with and without the subtree
        both = {"folder_id": project, "include_subtree": "true"}
        assert odcs_ids(await listing(stack, both)) == [c1, c2, c3]
        assert odcs_ids(await listing(stack, {"folder_id": project})) == []
        assert odcs_ids(await listing(stack, {"folder_id": t1})) == [c1, c2]
        assert odcs_ids(await listing(stack, {"folder_id": org, "include_subtree": "true"})) == [
            c1,
            c2,
            c3,
        ]

        # tags (all-of), domain (case-insensitive), ODCS id, status
        assert odcs_ids(
            await listing(
                stack, {"folder_id": org, "include_subtree": "true", "tags": ["pii", "finance"]}
            )
        ) == [c1]
        assert odcs_ids(
            await listing(stack, {"folder_id": org, "include_subtree": "true", "tags": "finance"})
        ) == [c1, c2]
        assert odcs_ids(
            await listing(stack, {"folder_id": org, "include_subtree": "true", "domain": "SALES"})
        ) == [c1, c2]
        assert odcs_ids(await listing(stack, {"odcs_id": c3})) == [c3]
        assert odcs_ids(
            await listing(stack, {"folder_id": org, "include_subtree": "true", "status": "draft"})
        ) == [c1, c2, c3]
        assert odcs_ids(await listing(stack, {"name": "orders"})) == [c1]

        # shape: items and next_cursor only; never a count
        body = (await listing(stack, {"folder_id": org, "include_subtree": "true"})).json()
        assert set(body) == {"items", "next_cursor"}
        assert body["next_cursor"] is None
        assert not {"total", "count"} & set(body)

        # D-35: a caller without grants gets 200 and an empty page for every combination
        stranger = fake_issuer.mint("nobody")
        for params in (
            None,
            {"q": "order"},
            {"folder_id": t1},
            {"folder_id": t1, "include_subtree": "true"},
        ):
            empty = await listing(stack, params, token=stranger)
            assert empty.status_code == 200, empty.text
            assert empty.headers["content-type"].startswith("application/json")
            assert empty.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "permission", ["contract:read-metadata", "contract:read", "contract:read-published"]
)
async def test_list_during_an_identity_outage_is_503_service_unavailable(
    make_stack: StackFactory, permission: str
) -> None:
    """WR-01: a scope call that fails after require() is the routes' 503, never a 500."""
    async with make_stack() as stack:
        with stack.flaky_authz(fail_permissions=frozenset({permission})):
            refused = await stack.client.get(LIST, headers=bearer(stack.admin_token))

        assert refused.status_code == 503, refused.text
        assert refused.headers["content-type"].startswith("application/problem+json")
        assert refused.json()["type"] == "urn:dc:problem:service-unavailable"
        assert "items" not in refused.json()

        recovered = await stack.client.get(LIST, headers=bearer(stack.admin_token))
        assert recovered.status_code == 200, recovered.text
