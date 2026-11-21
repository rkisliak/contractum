"""RBAC-07, RBAC-08, RBAC-04: lists show only what the caller may see (D-35, D-29).

role_assignments -> identity compact scope -> contracts list, and the SQL visibility subquery ->
identity folder list, end to end. A caller with no access gets 200 with an empty page, never a 403
and never a count. Organizations stay isolated in every list, filter and search result.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from dc_auth.authorizer import FolderScope
from dc_contracts.search import ContractSearchFilters, build_search_query

pytestmark = pytest.mark.integration

LIST = "/api/v1/contracts/contracts"
FOLDERS = "/api/v1/identity/folders"
EMPTY = {"items": [], "next_cursor": None}
StackFactory = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, name: str) -> bytes:
    return (
        f"apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: 1.0.0\n"
        f"status: draft\nname: {name}\n"
    ).encode()


async def seed(stack: Any, folder: str, tag: str, name: str) -> str:
    odcs_id = f"urn:dc:vis:{tag}:{uuid.uuid4().hex[:8]}"
    created = await stack.post(folder, doc(odcs_id, name))
    assert created.status_code == 201, created.text
    return str(created.json()["contract"]["id"])


async def contracts_of(stack: Any, token: str, **params: Any) -> list[str]:
    response = await stack.client.get(LIST, params=params, headers=bearer(token))
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def folders_of(stack: Any, token: str, **params: Any) -> list[str]:
    response = await stack.identity.get(FOLDERS, params=params, headers=bearer(token))
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def block(stack: Any, folder_id: str, *, blocked: bool = True) -> None:
    url = stack.identity_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE folder SET block_inheritance = %s WHERE id = %s", (blocked, folder_id))


def revoke(stack: Any, grant_id: str) -> None:
    url = stack.identity_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DELETE FROM role_assignments WHERE id = %s", (grant_id,))


@dataclass
class World:
    org_a: str
    team1: str
    team2: str
    org_b: str
    team3: str
    c1: str
    c2: str
    c3: str


async def build(stack: Any) -> World:
    a = await stack.chain("org-a")
    t2 = await stack.team_in_org(a.org, "two")
    b = await stack.chain("org-b")
    return World(
        org_a=a.org,
        team1=a.team,
        team2=t2,
        org_b=b.org,
        team3=b.team,
        c1=await seed(stack, a.team, "w", "Shared Name"),
        c2=await seed(stack, t2, "w", "Shared Name"),
        c3=await seed(stack, b.team, "w", "Shared Name"),
    )


async def test_a_team_reader_lists_only_their_team_and_a_stranger_gets_an_empty_page(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        w = await build(stack)
        reader, reader_id = stack.token("rd")
        stack.grant_sql(reader_id, "team_reader", w.team1)

        assert await contracts_of(stack, reader) == [w.c1]
        assert await contracts_of(stack, reader, q="Shared") == [w.c1]
        assert await contracts_of(stack, reader, folder_id=w.team3) == []
        assert await contracts_of(stack, reader, folder_id=w.org_a, include_subtree="true") == [
            w.c1
        ]
        assert await folders_of(stack, reader) == [w.team1]
        assert await folders_of(stack, reader, path="org-a") == []  # the organization is invisible
        assert await folders_of(stack, reader, parent_id=w.org_b) == []

        stranger, _ = stack.token("nobody")
        for params in (
            {},
            {"q": "Shared"},
            {"folder_id": w.team1},
            {"folder_id": w.org_a, "include_subtree": "true"},
        ):
            blind = await stack.client.get(LIST, params=params, headers=bearer(stranger))
            assert blind.status_code == 200 and blind.json() == EMPTY
        for params in ({}, {"path": "org-a"}, {"parent_id": w.org_a}):
            blind = await stack.identity.get(FOLDERS, params=params, headers=bearer(stranger))
            assert blind.status_code == 200 and blind.json() == EMPTY


async def test_a_contract_above_team_level_is_invisible_to_grants_below_it(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        at_org = await seed(stack, chain.org, "org", "At org")
        at_project = await seed(stack, chain.project, "proj", "At project")
        at_root = await seed(stack, stack.root_id, "root", "At root")

        team_reader, principal = stack.token("tr")
        stack.grant_sql(principal, "team_reader", chain.team)
        editor, editor_principal = stack.token("pe")
        stack.grant_sql(editor_principal, "project_editor", chain.project)
        org_reader, org_principal = stack.token("or")
        stack.grant_sql(org_principal, "org_reader", chain.org)

        for token, hidden in (
            (team_reader, {at_org, at_project, at_root}),
            (editor, {at_org, at_root}),
        ):
            listed = set(await contracts_of(stack, token))
            assert not (listed & hidden)
            for contract in hidden:
                response = await stack.client.get(f"{LIST}/{contract}", headers=bearer(token))
                assert response.status_code == 404, response.text
        assert at_project in set(await contracts_of(stack, editor))

        listed = set(await contracts_of(stack, org_reader))
        assert {at_org, at_project} <= listed
        assert at_root not in listed
        for contract in (at_org, at_project):
            response = await stack.client.get(f"{LIST}/{contract}", headers=bearer(org_reader))
            assert response.status_code == 200, response.text
        denied = await stack.client.get(f"{LIST}/{at_root}", headers=bearer(org_reader))
        assert denied.status_code == 404, denied.text


async def test_organization_isolation_a_team_reader_in_b_never_sees_a(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        w = await build(stack)
        reader, reader_id = stack.token("rb")
        stack.grant_sql(reader_id, "team_reader", w.team3)
        assert await contracts_of(stack, reader) == [w.c3]
        assert await folders_of(stack, reader) == [w.team3]
        assert await contracts_of(stack, reader, q="Shared", folder_id=w.team1) == []
        assert await contracts_of(stack, reader, folder_id=w.org_a, include_subtree="true") == []


async def test_a_block_cuts_a_root_grant_in_lists_but_not_a_grant_at_the_blocked_folder(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        w = await build(stack)
        gr, gr_id = stack.token("gr")
        br, br_id = stack.token("br")
        stack.grant_sql(gr_id, "global_reader", stack.root_id)
        stack.grant_sql(br_id, "org_reader", w.org_b)

        assert set(await contracts_of(stack, gr)) == {w.c1, w.c2, w.c3}
        assert await contracts_of(stack, br) == [w.c3]
        block(stack, w.org_b)

        assert set(await contracts_of(stack, gr)) == {w.c1, w.c2}
        gr_folders = set(await folders_of(stack, gr, limit=100))
        assert w.org_b not in gr_folders and w.team3 not in gr_folders
        assert {w.org_a, w.team1, w.team2} <= gr_folders
        # D-29: a grant made at the blocked folder keeps applying at it and below
        assert await contracts_of(stack, br) == [w.c3]
        br_folders = set(await folders_of(stack, br, limit=100))
        assert {w.org_b, w.team3} <= br_folders and w.org_a not in br_folders
        # D-28: the global admin is never cut
        assert set(await contracts_of(stack, stack.admin_token)) >= {w.c1, w.c2, w.c3}
        admin_folders = set(await folders_of(stack, stack.admin_token, limit=100))
        assert {w.org_b, w.team3} <= admin_folders


async def test_adjacency_pages_concatenate_to_exactly_the_visible_set(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        a = await stack.chain("adj-a")
        hidden_team = await stack.team_in_org(a.org, "hidden")
        reader, reader_id = stack.token("adj")
        stack.grant_sql(reader_id, "team_reader", a.team)
        visible: list[str] = []
        for index in range(10):
            if index in (1, 4, 7):  # invisible items interleaved with the visible ones
                await seed(stack, hidden_team, "adj", f"Hidden {index}")
            else:
                visible.append(await seed(stack, a.team, "adj", f"Visible {index}"))
        assert len(visible) == 7

        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": 3}
            if cursor:
                params["cursor"] = cursor
            response = await stack.client.get(LIST, params=params, headers=bearer(reader))
            assert response.status_code == 200, response.text
            body = response.json()
            pages += 1
            seen.extend(item["id"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert seen == visible  # no duplicate, no gap, no invisible item, id order
        assert pages == 3


async def test_empty_scope_answers_an_empty_page_for_every_filter_and_runs_no_list_query(
    make_stack: StackFactory,
) -> None:
    # No query at all for an empty scope: the builder refuses to produce one.
    none = FolderScope(all=False)
    assert build_search_query(ContractSearchFilters(), none, none) is None
    async with make_stack() as stack:
        w = await build(stack)
        stranger, _ = stack.token("empty")
        combos: list[dict[str, Any]] = [
            {},
            {"q": "Shared"},
            {"folder_id": w.team1},
            {"folder_id": w.team1, "include_subtree": "true"},
            {"status": "draft", "tags": "x", "domain": "d", "name": "Shared Name"},
            {"limit": 1},
        ]
        for params in combos:
            response = await stack.client.get(LIST, params=params, headers=bearer(stranger))
            assert response.status_code == 200, response.text
            assert response.json() == EMPTY
            assert "total" not in response.json() and "count" not in response.json()


async def test_ordering_trimmed_pages_stay_in_id_order_while_items_are_inserted(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        a = await stack.chain("ord-a")
        hidden_team = await stack.team_in_org(a.org, "hidden")
        reader, reader_id = stack.token("ord")
        stack.grant_sql(reader_id, "team_reader", a.team)
        first = [await seed(stack, a.team, "ord", "Same Name") for _ in range(4)]

        page1 = await stack.client.get(LIST, params={"limit": 2}, headers=bearer(reader))
        body1 = page1.json()
        assert [i["id"] for i in body1["items"]] == first[:2]  # equal names never tie

        # new visible and invisible items arrive between the two page requests
        await seed(stack, hidden_team, "ord", "Same Name")
        late = await seed(stack, a.team, "ord", "Same Name")
        page2 = await stack.client.get(
            LIST, params={"limit": 2, "cursor": body1["next_cursor"]}, headers=bearer(reader)
        )
        body2 = page2.json()
        assert [i["id"] for i in body2["items"]] == first[2:]
        page3 = await stack.client.get(
            LIST, params={"limit": 2, "cursor": body2["next_cursor"]}, headers=bearer(reader)
        )
        assert [i["id"] for i in page3.json()["items"]] == [late]
        ids = first + [late]
        assert ids == sorted(ids)  # uuidv7: creation order is id order


async def test_concurrency_a_revoked_grant_hides_the_items_on_the_very_next_page(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        a = await stack.chain("rev-a")
        reader, reader_id = stack.token("rev")
        grant = stack.grant_sql(reader_id, "team_reader", a.team)
        for index in range(5):
            await seed(stack, a.team, "rev", f"Item {index}")

        page1 = await stack.client.get(LIST, params={"limit": 2}, headers=bearer(reader))
        body1 = page1.json()
        assert len(body1["items"]) == 2 and body1["next_cursor"] is not None
        assert await folders_of(stack, reader) == [a.team]

        revoke(stack, grant)  # no authorization cache: the next request sees it at once
        page2 = await stack.client.get(
            LIST, params={"limit": 2, "cursor": body1["next_cursor"]}, headers=bearer(reader)
        )
        assert page2.status_code == 200 and page2.json() == EMPTY
        assert await folders_of(stack, reader) == []
