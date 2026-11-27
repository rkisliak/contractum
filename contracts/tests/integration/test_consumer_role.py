"""The Consumer role: reads folders and published contracts, never a draft, no writes (RBAC-09).

Since Phase 3 the lifecycle exists: a Consumer finds the contracts that have a Latest, through the
Latest's values only. One head row serves every caller: once a contract has a Latest, the list and
``GET /contracts/{id}`` show that Latest to editors, Policy Managers and Consumers alike (a newer
draft is read through the drafts routes); before the first publish editors keep their draft view
and Policy Managers keep metadata only (D-36). Nobody holds the role after bootstrap.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest
from dc_auth.roles import permissions_of

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
CONTRACTS = "/api/v1/contracts/contracts"
FOLDERS = "/api/v1/identity/folders"
UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, name: str) -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
        f"status: draft\nname: {name}\n"
    ).encode()


def stable(response: Any) -> tuple[int, str | None, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, response.headers.get("content-type"), sorted(body.items())


async def create(stack: Any, team: str, token: str, name: str) -> Any:
    return await stack.client.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=doc(f"urn:dc:consumer:{uuid.uuid4().hex}", name),
        headers={**bearer(token), "Content-Type": "application/yaml"},
    )


async def folder_ids(stack: Any, token: str) -> set[str]:
    response = await stack.identity.get(FOLDERS, params={"limit": 100}, headers=bearer(token))
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


async def test_empty_nobody_holds_consumer_and_a_consumer_sees_no_drafts(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        assert (
            sql(stack.identity_db_url, "SELECT id FROM role_assignments WHERE role = 'consumer'")
            == []
        )
        chain = await stack.chain()
        created = await create(stack, chain.team, stack.admin_token, "Draft")
        assert created.status_code == 201, created.text
        contract = created.json()["contract"]["id"]

        token, principal = stack.token("cs")
        stack.grant_sql(principal, "consumer", chain.team)

        listed = await stack.client.get(CONTRACTS, headers=bearer(token))
        assert listed.status_code == 200
        assert listed.json() == {"items": [], "next_cursor": None}

        unknown_meta = await stack.client.get(f"{CONTRACTS}/{UNKNOWN}", headers=bearer(token))
        unknown_doc = await stack.client.get(
            f"{CONTRACTS}/{UNKNOWN}/document", headers=bearer(token)
        )
        meta = await stack.client.get(f"{CONTRACTS}/{contract}", headers=bearer(token))
        document = await stack.client.get(f"{CONTRACTS}/{contract}/document", headers=bearer(token))
        assert meta.status_code == 404
        assert document.status_code == 404
        assert stable(meta) == stable(unknown_meta)
        assert stable(document) == stable(unknown_doc)

        # can read the folder (folder:read) but cannot write anything
        assert (
            await stack.identity.get(f"{FOLDERS}/{chain.team}", headers=bearer(token))
        ).status_code == 200
        denied = await create(stack, chain.team, token, "Nope")
        assert denied.status_code == 403
        assert denied.json()["type"].endswith("forbidden")
        template = await stack.client.post(
            f"/api/v1/contracts/folders/{chain.team}/templates",
            params={"name": "T"},
            content=(FIXTURES / "valid-v3.2.0.yaml").read_bytes(),
            headers={**bearer(token), "Content-Type": "application/yaml"},
        )
        assert template.status_code == 403


async def test_adjacency_union_with_an_editor_role_and_root_grant_obeys_block_flag(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        org_a = await stack.chain("orga")
        org_b = await stack.chain("orgb")

        both, principal = stack.token("cs-editor")
        stack.grant_sql(principal, "consumer", org_a.team)
        stack.grant_sql(principal, "team_editor", org_a.team)
        created = await create(stack, org_a.team, both, "Union")
        assert created.status_code == 201, created.text
        contract = created.json()["contract"]["id"]
        read = await stack.client.get(f"{CONTRACTS}/{contract}", headers=bearer(both))
        assert read.status_code == 200
        assert read.json()["content_redacted"] is False

        # a Consumer granted at ROOT sees folders everywhere until a block flag cuts the subtree
        root_consumer, root_principal = stack.token("rc")
        stack.grant_sql(root_principal, "consumer", stack.root_id)
        before = await folder_ids(stack, root_consumer)
        assert {org_b.org, org_b.project, org_b.team} <= before
        sql(
            stack.identity_db_url,
            "UPDATE folder SET block_inheritance = true WHERE id = %s",
            org_b.org,
        )
        after = await folder_ids(stack, root_consumer)
        assert not {org_b.org, org_b.project, org_b.team} & after
        assert {org_a.org, org_a.project, org_a.team} <= after
        # only the Global admin is exempt
        assert {org_b.org, org_b.team} <= await folder_ids(stack, stack.admin_token)


async def test_ordering_effective_access_unions_roles_and_permissions_sorted(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        token, principal = stack.token("cs-reader")
        stack.grant_sql(principal, "consumer", chain.team)
        stack.grant_sql(principal, "team_reader", chain.team)
        response = await stack.identity.get(
            f"{FOLDERS}/{chain.team}/effective-access", headers=bearer(token)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["roles"] == ["consumer", "team_reader"]
        assert body["permissions"] == sorted(permissions_of(["consumer", "team_reader"]))


async def test_policy_manager_in_one_organization_sees_nothing_of_another(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        org_a = await stack.chain("orga")
        org_b = await stack.chain("orgb")
        tag = uuid.uuid4().hex[:8]
        created = await create(stack, org_b.team, stack.admin_token, f"Hidden {tag}")
        assert created.status_code == 201, created.text
        hidden = created.json()["contract"]["id"]
        pm, principal = stack.token("pm-a")
        stack.grant_sql(principal, "policy_manager", org_a.org)
        for params in (
            {},
            {"name": f"Hidden {tag}"},
            {"q": tag},
            {"status": "draft"},
            {"folder_id": org_b.team},
            {"folder_id": org_b.org, "include_subtree": "true"},
        ):
            response = await stack.client.get(CONTRACTS, params=params, headers=bearer(pm))
            assert response.status_code == 200, response.text
            assert hidden not in [item["id"] for item in response.json()["items"]], params
        assert (
            await stack.client.get(f"{CONTRACTS}/{hidden}", headers=bearer(pm))
        ).status_code == 404


def tagged_doc(odcs_id: str, name: str, tags: list[str], version: str) -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: {name}\ntags: [{', '.join(tags)}]\n"
    ).encode()


class Head:
    """A published contract (Latest 1.0.0 "Orders", gold) with a newer renamed, retagged draft."""

    def __init__(self, contract: str, draft: str, team: str, latest_version_id: str) -> None:
        self.contract = contract
        self.draft = draft
        self.team = team
        self.latest_version_id = latest_version_id


async def published_with_newer_draft(stack: Any, team: str) -> Head:
    odcs_id = f"urn:dc:consumer:{uuid.uuid4().hex}"
    contract, draft, etag = await stack.create_contract(
        team, tagged_doc(odcs_id, "Orders", ["gold"], "1.0.0")
    )
    published = await stack.publish(contract, draft, etag)
    assert published.status_code == 201, published.text
    created = await stack.client.post(
        f"{CONTRACTS}/{contract}/drafts",
        json={"from_version": "1.0.0", "bump": "minor"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    newer = created.json()["draft"]
    saved = await stack.client.put(
        f"{CONTRACTS}/{contract}/drafts/{newer['id']}",
        content=tagged_doc(odcs_id, "Orders Next secret", ["internal"], "1.1.0"),
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": newer["etag"],
        },
    )
    assert saved.status_code == 200, saved.text
    return Head(contract, newer["id"], team, published.json()["version_id"])


async def list_items(stack: Any, token: str, **params: Any) -> dict[str, dict[str, Any]]:
    response = await stack.client.get(
        CONTRACTS, params={"limit": 100, **params}, headers=bearer(token)
    )
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["items"]}


async def test_consumer_lists_a_contract_only_after_its_first_publish(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        draft_only, _, _ = await stack.create_contract(
            chain.team, doc(f"urn:dc:consumer:{uuid.uuid4().hex}", "Only a draft")
        )
        head = await published_with_newer_draft(stack, chain.team)
        consumer, principal = stack.token("cs-list")
        stack.grant_sql(principal, "consumer", chain.team)
        editor, editor_principal = stack.token("ed-list")
        stack.grant_sql(editor_principal, "team_editor", chain.team)

        items = await list_items(stack, consumer)
        assert head.contract in items
        assert draft_only not in items

        # an editor sees both; the draft-only contract keeps its Phase 2 view
        editor_items = await list_items(stack, editor)
        assert {head.contract, draft_only} <= set(editor_items)
        only = editor_items[draft_only]
        assert only["latest_version"] is None
        assert only["name"] == "Only a draft"
        assert only["version"] == {"number": 1, "state": "draft", "deprecated": False}


async def test_consumer_list_item_comes_from_the_latest_not_the_newer_draft(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        head = await published_with_newer_draft(stack, chain.team)
        consumer, principal = stack.token("cs-latest")
        stack.grant_sql(principal, "consumer", chain.team)

        item = (await list_items(stack, consumer))[head.contract]
        assert item["name"] == "Orders"
        assert item["tags"] == ["gold"]
        assert item["content_redacted"] is False
        assert item["latest_version"] == {
            "version_id": head.latest_version_id,
            "semver": "1.0.0",
        }
        assert item["version"] == {"number": 1, "state": "published", "deprecated": False}
        # draft-derived values are no oracle for the Consumer
        for params in ({"tags": "internal"}, {"q": "secret"}, {"name": "Orders Next secret"}):
            assert head.contract not in await list_items(stack, consumer, **params), params
        for params in ({"tags": "gold"}, {"q": "Orders"}):
            assert head.contract in await list_items(stack, consumer, **params), params


async def test_head_row_is_latest_for_editor(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        head = await published_with_newer_draft(stack, chain.team)
        editor, principal = stack.token("ed-head")
        stack.grant_sql(principal, "team_editor", chain.team)

        item = (await list_items(stack, editor))[head.contract]
        assert item["name"] == "Orders"
        assert item["tags"] == ["gold"]
        assert item["latest_version"]["semver"] == "1.0.0"
        assert item["version"] == {"number": 1, "state": "published", "deprecated": False}
        assert item["content_redacted"] is False
        # filters match head values for the editor too
        for params in ({"tags": "internal"}, {"q": "secret"}):
            assert head.contract not in await list_items(stack, editor, **params), params

        by_id = await stack.client.get(f"{CONTRACTS}/{head.contract}", headers=bearer(editor))
        assert by_id.status_code == 200, by_id.text
        body = by_id.json()
        assert (body["name"], body["tags"], body["version"]) == (
            "Orders",
            ["gold"],
            {"number": 1, "state": "published", "deprecated": False},
        )

        drafts = await stack.client.get(
            f"{CONTRACTS}/{head.contract}/drafts", headers=bearer(editor)
        )
        assert drafts.status_code == 200, drafts.text
        newer = [d for d in drafts.json()["items"] if d["id"] == head.draft]
        assert [(d["name"], d["declared_version"]) for d in newer] == [
            ("Orders Next secret", "1.1.0")
        ]


async def test_head_row_is_latest_for_consumer(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        head = await published_with_newer_draft(stack, chain.team)
        consumer, principal = stack.token("cs-head")
        stack.grant_sql(principal, "consumer", chain.team)
        editor, editor_principal = stack.token("ed-head2")
        stack.grant_sql(editor_principal, "team_editor", chain.team)

        consumer_item = (await list_items(stack, consumer))[head.contract]
        editor_item = (await list_items(stack, editor))[head.contract]
        assert consumer_item["name"] == "Orders"
        assert consumer_item["tags"] == ["gold"]
        assert consumer_item["latest_version"]["semver"] == "1.0.0"
        assert consumer_item["version"] == {"number": 1, "state": "published", "deprecated": False}
        assert consumer_item == editor_item


async def test_policy_manager_sees_the_latest_row_redacted_for_a_published_contract(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        draft_only, _, _ = await stack.create_contract(
            chain.team, doc(f"urn:dc:consumer:{uuid.uuid4().hex}", "Only a draft")
        )
        head = await published_with_newer_draft(stack, chain.team)
        pm, principal = stack.token("pm-head")
        stack.grant_sql(principal, "policy_manager", chain.team)

        items = await list_items(stack, pm)
        published = items[head.contract]
        assert published["name"] == "Orders"
        assert published["latest_version"]["semver"] == "1.0.0"
        assert published["version"] == {"number": 1, "state": "published", "deprecated": False}
        assert published["content_redacted"] is True
        assert published["tags"] is None
        assert published["domain"] is None
        only = items[draft_only]
        assert only["content_redacted"] is True
        assert only["tags"] is None
        assert only["latest_version"] is None
        # filters on content never match for a metadata-only caller
        assert head.contract not in await list_items(stack, pm, tags="gold")


async def test_consumer_views_a_listed_contract_by_id_through_the_latest(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        head = await published_with_newer_draft(stack, chain.team)
        consumer, principal = stack.token("cs-view")
        stack.grant_sql(principal, "consumer", chain.team)

        listed = (await list_items(stack, consumer))[head.contract]
        response = await stack.client.get(f"{CONTRACTS}/{head.contract}", headers=bearer(consumer))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "Orders"
        assert body["tags"] == ["gold"]
        assert body["content_redacted"] is False
        assert body["latest_version"]["semver"] == "1.0.0"
        assert body["version"] == {"number": 1, "state": "published", "deprecated": False}
        assert body == listed
        # the hash is the Latest's, not the newer draft's
        draft = await stack.client.get(
            f"{CONTRACTS}/{head.contract}/drafts/{head.draft}", headers=bearer(stack.admin_token)
        )
        assert body["content_sha256"] != draft.json()["content_sha256"]


async def test_consumer_gets_the_unknown_404_for_a_draft_only_contract_on_every_route(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(f"urn:dc:consumer:{uuid.uuid4().hex}", "Only a draft")
        )
        consumer, principal = stack.token("cs-oracle")
        stack.grant_sql(principal, "consumer", chain.team)
        headers = bearer(consumer)

        def routes(cid: str, did: str) -> list[tuple[str, str, dict[str, Any]]]:
            return [
                ("GET", f"{CONTRACTS}/{cid}", {}),
                ("GET", f"{CONTRACTS}/{cid}/drafts", {}),
                (
                    "PUT",
                    f"{CONTRACTS}/{cid}/drafts/{did}",
                    {
                        "content": doc("urn:x", "x"),
                        "headers": {
                            **headers,
                            "Content-Type": "application/yaml",
                            "If-Match": etag,
                        },
                    },
                ),
                (
                    "POST",
                    f"{CONTRACTS}/{cid}/drafts/{did}/publish",
                    {"headers": {**headers, "If-Match": etag}},
                ),
            ]

        real = routes(contract, draft)
        unknown = routes(UNKNOWN, UNKNOWN)
        for (method, url, kwargs), (_, unknown_url, _kw) in zip(real, unknown, strict=True):
            kwargs = {"headers": headers, **kwargs}
            answered = await stack.client.request(method, url, **kwargs)
            expected = await stack.client.request(method, unknown_url, **kwargs)
            assert answered.status_code == 404, (method, url, answered.text)
            assert stable(answered) == stable(expected), (method, url)


async def test_policy_manager_and_editor_by_id_keep_their_views(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        draft_only, _, _ = await stack.create_contract(
            chain.team, doc(f"urn:dc:consumer:{uuid.uuid4().hex}", "Only a draft")
        )
        head = await published_with_newer_draft(stack, chain.team)
        pm, pm_principal = stack.token("pm-view")
        stack.grant_sql(pm_principal, "policy_manager", chain.team)
        editor, editor_principal = stack.token("ed-view")
        stack.grant_sql(editor_principal, "team_editor", chain.team)
        outsider, _ = stack.token("nobody-view")

        redacted = await stack.client.get(f"{CONTRACTS}/{head.contract}", headers=bearer(pm))
        assert redacted.status_code == 200, redacted.text
        assert redacted.json()["content_redacted"] is True
        assert redacted.json()["tags"] is None
        assert redacted.json()["version"] == {
            "number": 1,
            "state": "published",
            "deprecated": False,
        }

        draft_view = await stack.client.get(f"{CONTRACTS}/{draft_only}", headers=bearer(editor))
        assert draft_view.status_code == 200, draft_view.text
        assert draft_view.json()["name"] == "Only a draft"
        assert draft_view.json()["latest_version"] is None
        assert draft_view.json()["version"] == {"number": 1, "state": "draft", "deprecated": False}

        unknown = await stack.client.get(f"{CONTRACTS}/{UNKNOWN}", headers=bearer(outsider))
        hidden = await stack.client.get(f"{CONTRACTS}/{head.contract}", headers=bearer(outsider))
        assert hidden.status_code == 404
        assert stable(hidden) == stable(unknown)
