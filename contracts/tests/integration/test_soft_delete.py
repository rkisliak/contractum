"""Soft delete and restore of a contract (CONT-04, D-55, D-56).

Tracer: an editor deletes a contract; afterwards it is hidden from everyone on every route and
list, every row stays in the database, and its ODCS id stays taken in the Organization.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]

UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
CONTRACTS = "/api/v1/contracts/contracts"
VOLATILE_CONTRACT_COLUMNS = {"deleted_at", "deleted_by", "updated_at", "updated_by"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *, version: str = "1.0.0") -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        "status: draft\nname: Orders\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def new_odcs_id() -> str:
    # No ``:``: a relationship ``to`` file reference (``<id>.yaml#/schema/x``) must match the ODCS
    # JSON Schema's reference pattern, which the real publish gate enforces and which has no colon.
    return f"softdelete-{uuid.uuid4().hex}"


def stable(response: Any) -> tuple[int, str | None, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, response.headers.get("content-type"), sorted(body.items())


async def role_at(stack: Any, folder: str, role: str, sub: str) -> str:
    token, principal = stack.token(sub)
    stack.grant_sql(principal, role, folder)
    return token


async def published_with_draft(stack: Any, team: str) -> tuple[str, str]:
    """Contract with 1.0.0 published (Latest) and one more draft; returns (contract, ODCS id)."""
    odcs_id = new_odcs_id()
    contract, draft, etag = await stack.create_contract(team, doc(odcs_id))
    published = await stack.publish(contract, draft, etag)
    assert published.status_code == 201, published.text
    extra = await stack.client.post(
        f"{CONTRACTS}/{contract}/drafts",
        json={"from_version": "1.0.0", "bump": "minor"},
        headers=bearer(stack.admin_token),
    )
    assert extra.status_code == 201, extra.text
    return contract, odcs_id


def contract_routes(contract: str) -> list[tuple[str, str]]:
    base = f"{CONTRACTS}/{contract}"
    return [
        ("GET", base),
        ("GET", f"{base}/document"),
        ("GET", f"{base}/drafts"),
        ("GET", f"{base}/versions"),
        ("GET", f"{base}/versions/latest"),
        ("GET", f"{base}/versions/1.0.0"),
        ("GET", f"{base}/versions/1.0.0/metadata"),
        ("POST", f"{base}/versions/1.0.0/promote"),
        ("POST", f"{base}/versions/1.0.0/unpublish"),
        ("POST", f"{base}/versions/1.0.0/republish"),
    ]


def events(stack: Any, sql: Sql, action: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_id, before, after FROM outbox WHERE action = %s ORDER BY id",
        action,
    )


def contract_row(stack: Any, sql: Sql, contract: str) -> dict[str, Any]:
    return sql(stack.contracts_db_url, "SELECT * FROM contract WHERE id = %s", contract)[0]


def version_rows(stack: Any, sql: Sql, contract: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM contract_version WHERE contract_id = %s ORDER BY version_no",
        contract,
    )


async def test_soft_delete_an_editor_removes_a_contract_and_it_is_hidden_everywhere(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor = await role_at(stack, chain.team, "team_editor", "editor")
        consumer = await role_at(stack, chain.team, "consumer", "consumer")
        contract, odcs_id = await published_with_draft(stack, chain.team)
        rows_before = version_rows(stack, sql, contract)
        contract_before = contract_row(stack, sql, contract)

        deleted = await stack.client.delete(f"{CONTRACTS}/{contract}", headers=bearer(editor))
        assert deleted.status_code == 204, deleted.text
        assert deleted.content == b""

        # every route is the unknown-contract 404, byte-identical, for everyone
        for token in (stack.admin_token, editor, consumer):
            for method, url in contract_routes(contract):
                unknown = url.replace(contract, UNKNOWN)
                got = await stack.client.request(method, url, headers=bearer(token))
                reference = await stack.client.request(method, unknown, headers=bearer(token))
                assert got.status_code == 404, (method, url, got.text)
                assert stable(got) == stable(reference), (method, url)
            listed = await stack.client.get(CONTRACTS, headers=bearer(token))
            assert listed.status_code == 200, listed.text
            assert contract not in [item["id"] for item in listed.json()["items"]]
            by_odcs = await stack.client.get(
                CONTRACTS, params={"odcs_id": odcs_id}, headers=bearer(token)
            )
            assert by_odcs.json()["items"] == []

        # nothing is removed or rewritten: only the two soft-delete columns change
        assert version_rows(stack, sql, contract) == rows_before
        after = contract_row(stack, sql, contract)
        assert after["deleted_at"] is not None
        assert after["deleted_by"] == "fake:editor"
        keep = set(after) - VOLATILE_CONTRACT_COLUMNS
        assert {k: after[k] for k in keep} == {k: contract_before[k] for k in keep}
        assert invariant(stack.contracts_db_url) == []

        # the ODCS id stays taken in the Organization
        again = await stack.post(chain.team, doc(odcs_id))
        assert again.status_code == 409, again.text
        assert again.json()["type"] == "urn:dc:problem:duplicate-contract-id"

        # one metadata-only event
        recorded = events(stack, sql, "contract.deleted")
        assert len(recorded) == 1
        assert str(recorded[0]["aggregate_id"]) == contract
        assert recorded[0]["before"]["deleted_at"] is None
        assert recorded[0]["after"]["deleted_at"] is not None
        assert "document" not in recorded[0]["after"]
        assert "raw_document" not in recorded[0]["after"]


async def test_soft_delete_of_an_already_deleted_contract_is_404_and_writes_no_event(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await published_with_draft(stack, chain.team)
        first = await stack.client.delete(
            f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token)
        )
        assert first.status_code == 204, first.text
        second = await stack.client.delete(
            f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token)
        )
        assert second.status_code == 404, second.text
        assert len(events(stack, sql, "contract.deleted")) == 1


async def test_soft_delete_works_for_a_contract_with_drafts_only(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _draft, _etag = await stack.create_contract(chain.team, doc(new_odcs_id()))
        deleted = await stack.client.delete(
            f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token)
        )
        assert deleted.status_code == 204, deleted.text
        gone = await stack.client.get(
            f"{CONTRACTS}/{contract}/drafts", headers=bearer(stack.admin_token)
        )
        assert gone.status_code == 404


async def test_soft_delete_denials_are_403_for_a_reader_and_404_for_everyone_who_cannot_read(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        reader = await role_at(stack, chain.team, "team_reader", "reader")
        consumer = await role_at(stack, chain.team, "consumer", "consumer")
        manager = await role_at(stack, chain.org, "policy_manager", "manager")
        stranger, _ = stack.token("stranger")
        contract, _ = await published_with_draft(stack, chain.team)

        forbidden = await stack.client.delete(f"{CONTRACTS}/{contract}", headers=bearer(reader))
        assert forbidden.status_code == 403, forbidden.text
        unknown = await stack.client.delete(f"{CONTRACTS}/{UNKNOWN}", headers=bearer(consumer))
        for token in (consumer, manager, stranger):
            denied = await stack.client.delete(f"{CONTRACTS}/{contract}", headers=bearer(token))
            assert stable(denied) == stable(unknown), token
        unauthenticated = await stack.client.delete(f"{CONTRACTS}/{contract}")
        assert unauthenticated.status_code == 401

        assert contract_row(stack, sql, contract)["deleted_at"] is None
        assert events(stack, sql, "contract.deleted") == []


async def test_soft_delete_is_refused_in_an_archived_folder(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await published_with_draft(stack, chain.team)
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.project}/archive",
            headers=bearer(stack.admin_token),
        )
        assert archived.status_code == 200, archived.text
        refused = await stack.client.delete(
            f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token)
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["type"] == "urn:dc:problem:folder-archived"
        assert contract_row(stack, sql, contract)["deleted_at"] is None


async def test_soft_delete_source_of_create_from_version_is_404(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await published_with_draft(stack, chain.team)
        url = f"/api/v1/contracts/folders/{chain.team}/contracts/from-version"

        copied = await stack.client.post(
            url,
            json={
                "source_contract_id": contract,
                "source_version": "1.0.0",
                "odcs_id": new_odcs_id(),
            },
            headers=bearer(stack.admin_token),
        )
        assert copied.status_code == 201, copied.text

        deleted = await stack.client.delete(
            f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token)
        )
        assert deleted.status_code == 204, deleted.text
        refused = await stack.client.post(
            url,
            json={
                "source_contract_id": contract,
                "source_version": "1.0.0",
                "odcs_id": new_odcs_id(),
            },
            headers=bearer(stack.admin_token),
        )
        unknown = await stack.client.post(
            url,
            json={
                "source_contract_id": UNKNOWN,
                "source_version": "1.0.0",
                "odcs_id": new_odcs_id(),
            },
            headers=bearer(stack.admin_token),
        )
        assert refused.status_code == 404, refused.text
        assert stable(refused) == stable(unknown)


async def test_restore_an_org_admin_brings_a_deleted_contract_back_and_nobody_else_can_see_it(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor = await role_at(stack, chain.team, "team_editor", "editor")
        team_admin = await role_at(stack, chain.team, "team_admin", "tadmin")
        project_admin = await role_at(stack, chain.project, "project_admin", "padmin")
        org_admin = await role_at(stack, chain.org, "org_admin", "oadmin")
        contract, odcs_id = await published_with_draft(stack, chain.team)
        deleted = await stack.client.delete(f"{CONTRACTS}/{contract}", headers=bearer(editor))
        assert deleted.status_code == 204, deleted.text
        restore = f"{CONTRACTS}/{contract}/restore"

        unknown = await stack.client.post(f"{CONTRACTS}/{UNKNOWN}/restore", headers=bearer(editor))
        assert unknown.status_code == 404
        for token in (editor, team_admin, project_admin):
            denied = await stack.client.post(restore, headers=bearer(token))
            assert stable(denied) == stable(unknown), token
        assert contract_row(stack, sql, contract)["deleted_at"] is not None
        assert events(stack, sql, "contract.restored") == []

        restored = await stack.client.post(restore, headers=bearer(org_admin))
        assert restored.status_code == 200, restored.text
        body = restored.json()
        assert body["id"] == contract
        assert body["odcs_id"] == odcs_id
        assert body["latest_version"]["semver"] == "1.0.0"

        # everything is visible again
        for method, url in contract_routes(contract)[:7]:
            got = await stack.client.request(method, url, headers=bearer(org_admin))
            assert got.status_code == 200, (method, url, got.text)
        latest = await stack.client.get(
            f"{CONTRACTS}/{contract}/versions/latest", headers=bearer(editor)
        )
        assert latest.status_code == 200, latest.text
        listed = await stack.client.get(CONTRACTS, headers=bearer(editor))
        assert contract in [item["id"] for item in listed.json()["items"]]
        row = contract_row(stack, sql, contract)
        assert row["deleted_at"] is None
        assert row["deleted_by"] is None

        recorded = events(stack, sql, "contract.restored")
        assert len(recorded) == 1
        assert str(recorded[0]["aggregate_id"]) == contract
        assert recorded[0]["before"]["deleted_at"] is not None
        assert recorded[0]["after"]["deleted_at"] is None
        assert "document" not in recorded[0]["after"]
        assert invariant(stack.contracts_db_url) == []


async def test_restore_a_global_admin_restores_another_deleted_contract(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await published_with_draft(stack, chain.team)
        deleted = await stack.client.delete(
            f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token)
        )
        assert deleted.status_code == 204, deleted.text
        restored = await stack.client.post(
            f"{CONTRACTS}/{contract}/restore", headers=bearer(stack.admin_token)
        )
        assert restored.status_code == 200, restored.text
        assert len(events(stack, sql, "contract.restored")) == 1


async def test_restore_of_a_contract_that_is_not_deleted_is_a_200_no_op_without_an_event(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        org_admin = await role_at(stack, chain.org, "org_admin", "oadmin")
        contract, _ = await published_with_draft(stack, chain.team)
        before = contract_row(stack, sql, contract)
        restored = await stack.client.post(
            f"{CONTRACTS}/{contract}/restore", headers=bearer(org_admin)
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["id"] == contract
        assert contract_row(stack, sql, contract) == before
        assert events(stack, sql, "contract.restored") == []


async def test_restore_is_refused_in_an_archived_folder(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await published_with_draft(stack, chain.team)
        deleted = await stack.client.delete(
            f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token)
        )
        assert deleted.status_code == 204, deleted.text
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.project}/archive",
            headers=bearer(stack.admin_token),
        )
        assert archived.status_code == 200, archived.text
        refused = await stack.client.post(
            f"{CONTRACTS}/{contract}/restore", headers=bearer(stack.admin_token)
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["type"] == "urn:dc:problem:folder-archived"
        assert contract_row(stack, sql, contract)["deleted_at"] is not None


# --- the reference guard (CONT-04, D-54) ----------------------------------------------------


def linked_doc(
    odcs_id: str,
    *,
    urls: tuple[str, ...] = (),
    tos: tuple[str, ...] = (),
    version: str = "1.0.0",
) -> bytes:
    """A contract document linking to other contracts by authoritativeDefinitions url and
    relationships to (the two field families D-54 names)."""
    text = doc(odcs_id, version=version).decode()
    if urls:
        text += "authoritativeDefinitions:\n" + "".join(
            f'  - type: businessDefinition\n    url: "{url}"\n' for url in urls
        )
    if tos:
        # schema-level relationships belong to the first schema object (indented under it)
        text = text.replace(
            "    properties:\n",
            "    relationships:\n"
            + "".join(
                f'      - type: foreignKey\n        from: orders.id\n        to: "{to}"\n'
                for to in tos
            )
            + "    properties:\n",
            1,
        )
    return text.encode()


def platform_url(contract: str, suffix: str = "") -> str:
    return f"https://dc.example{CONTRACTS}/{contract}{suffix}"


def reference_rows(stack: Any, sql: Sql, contract: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT r.*, v.state FROM contract_reference r JOIN contract_version v "
        "ON v.id = r.source_version_id WHERE r.source_contract_id = %s ORDER BY r.ref_kind, "
        "r.ref_value",
        contract,
    )


async def published_link(
    stack: Any,
    team: str,
    *,
    urls: tuple[str, ...] = (),
    tos: tuple[str, ...] = (),
    odcs_id: str | None = None,
    publish: bool = True,
) -> tuple[str, str]:
    """A contract in ``team`` whose first version links as given; returns (contract, ODCS id)."""
    odcs_id = odcs_id or new_odcs_id()
    contract, draft, etag = await stack.create_contract(
        team, linked_doc(odcs_id, urls=urls, tos=tos)
    )
    if publish:
        published = await stack.publish(contract, draft, etag)
        assert published.status_code == 201, published.text
    return contract, odcs_id


async def delete_as(stack: Any, contract: str, token: str) -> Any:
    return await stack.client.delete(f"{CONTRACTS}/{contract}", headers=bearer(token))


async def test_referenced_same_org_published_platform_url_blocks_delete_and_names_the_referrer(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        sibling_team = await stack.team_in_org(chain.org, "sib")
        editor = await role_at(stack, chain.team, "team_editor", "editor")
        await role_at(stack, sibling_team, "team_reader", "editor")  # the editor can read R's team
        target, _ = await published_with_draft(stack, chain.team)
        referrer, referrer_odcs = await published_link(
            stack, sibling_team, urls=(platform_url(target),)
        )

        refused = await delete_as(stack, target, editor)

        assert refused.status_code == 409, refused.text
        body = refused.json()
        assert body["type"] == "urn:dc:problem:contract-referenced"
        assert body["referrers"] == [{"contract_id": referrer, "odcs_id": referrer_odcs}]
        assert body["hidden_referrer_count"] == 0
        # nothing changed: still visible, no deleted_at, no event
        assert contract_row(stack, sql, target)["deleted_at"] is None
        assert events(stack, sql, "contract.deleted") == []
        still = await stack.client.get(f"{CONTRACTS}/{target}", headers=bearer(editor))
        assert still.status_code == 200, still.text
        assert invariant(stack.contracts_db_url) == []


async def test_a_team_contract_linking_to_an_organization_level_contract_blocks_its_delete(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    """The link resolves inside the Organization scope, whatever level the target lives at."""
    async with make_stack() as stack:
        chain = await stack.chain()
        target, target_odcs = await published_with_draft(stack, chain.org)
        referrer, referrer_odcs = await published_link(
            stack, chain.team, tos=(file_ref(target_odcs),)
        )

        refused = await delete_as(stack, target, stack.admin_token)

        assert refused.status_code == 409, refused.text
        body = refused.json()
        assert body["type"] == "urn:dc:problem:contract-referenced"
        assert body["referrers"] == [{"contract_id": referrer, "odcs_id": referrer_odcs}]
        assert body["hidden_referrer_count"] == 0
        assert contract_row(stack, sql, target)["deleted_at"] is None
        assert events(stack, sql, "contract.deleted") == []
        assert invariant(stack.contracts_db_url) == []


async def test_referenced_cross_org_publish_blocks_delete_and_is_only_counted(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        editor = await role_at(stack, chain.team, "team_editor", "editor")  # nothing in `other`
        target, _ = await published_with_draft(stack, chain.team)
        referrer, referrer_odcs = await published_link(
            stack, other.team, urls=(platform_url(target),)
        )

        refused = await delete_as(stack, target, editor)

        assert refused.status_code == 409, refused.text
        body = refused.json()
        assert body["type"] == "urn:dc:problem:contract-referenced"
        assert body["referrers"] == []
        assert body["hidden_referrer_count"] == 1
        # the response reveals nothing about the invisible referrer or where it lives
        for secret in (referrer, referrer_odcs, other.org, other.project, other.team):
            assert secret not in refused.text
        assert contract_row(stack, sql, target)["deleted_at"] is None
        assert events(stack, sql, "contract.deleted") == []
        assert invariant(stack.contracts_db_url) == []


async def test_referenced_cross_org_visible_and_hidden_referrers_are_split_by_the_callers_scope(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        third = await stack.chain()
        editor = await role_at(stack, chain.team, "team_editor", "editor")
        await role_at(stack, other.team, "consumer", "editor")  # published read in `other` only
        target, _ = await published_with_draft(stack, chain.team)
        visible_ref, visible_odcs = await published_link(
            stack, other.team, urls=(platform_url(target),)
        )
        await published_link(stack, third.team, urls=(platform_url(target, "/versions/1.0.0"),))

        refused = await delete_as(stack, target, editor)

        assert refused.status_code == 409, refused.text
        body = refused.json()
        assert body["referrers"] == [{"contract_id": visible_ref, "odcs_id": visible_odcs}]
        assert body["hidden_referrer_count"] == 1
        assert third.org not in refused.text and third.team not in refused.text


async def test_referenced_publish_indexes_one_row_per_link_and_drafts_index_nothing(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        target, _ = await published_with_draft(stack, chain.team)

        draft_only, _ = await published_link(
            stack, other.team, urls=(platform_url(target),), publish=False
        )
        assert reference_rows(stack, sql, draft_only) == []

        referrer, _ = await published_link(
            stack,
            other.team,
            urls=(platform_url(target),),
            tos=("somewhere-else.yaml#/schema/x",),
        )
        rows = reference_rows(stack, sql, referrer)
        assert [(r["ref_kind"], r["ref_value"], r["state"]) for r in rows] == [
            ("contract_uuid", target, "published"),
            ("odcs_id", "somewhere-else", "published"),
        ]
        assert {str(r["organization_id"]) for r in rows} == {other.org}
        assert {r["pointer"] for r in rows} == {
            "/authoritativeDefinitions/0/url",
            "/schema/0/relationships/0/to",
        }
        # the publish still records exactly one audit event, and the index adds none
        published_events = [
            e
            for e in events(stack, sql, "contract.version_published")
            if str(e["aggregate_id"]) == referrer
        ]
        assert len(published_events) == 1


# --- file-name references, the resolution rule and the cases that never block (D-54, A14) ----


def file_ref(odcs_id: str, fragment: str = "/schema/x") -> str:
    return f"{odcs_id}.yaml#{fragment}"


async def put_draft(stack: Any, contract: str, draft: str, etag: str, body: bytes) -> None:
    saved = await stack.client.put(
        f"{CONTRACTS}/{contract}/drafts/{draft}",
        content=body,
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": etag,
        },
    )
    assert saved.status_code == 200, saved.text


async def publish_next_version(
    stack: Any,
    contract: str,
    odcs_id: str,
    version: str,
    *,
    urls: tuple[str, ...] = (),
    tos: tuple[str, ...] = (),
    publish: bool = True,
) -> str:
    """A new draft of ``contract`` (from 1.0.0) saved with these links, published by default."""
    started = await stack.client.post(
        f"{CONTRACTS}/{contract}/drafts",
        json={"from_version": "1.0.0", "bump": "minor"},
        headers=bearer(stack.admin_token),
    )
    assert started.status_code == 201, started.text
    draft = started.json()["draft"]
    body = linked_doc(odcs_id, urls=urls, tos=tos, version=version)
    await put_draft(stack, contract, draft["id"], draft["etag"], body)
    if publish:
        saved = await stack.client.get(
            f"{CONTRACTS}/{contract}/drafts/{draft['id']}", headers=bearer(stack.admin_token)
        )
        published = await stack.publish(contract, draft["id"], saved.json()["etag"])
        assert published.status_code == 201, published.text
    return str(draft["id"])


async def test_referenced_file_name_link_in_the_same_org_blocks_and_names_the_referrer(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        sibling_team = await stack.team_in_org(chain.org, "sib")
        target, target_odcs = await published_with_draft(stack, chain.team)
        referrer, referrer_odcs = await published_link(
            stack, sibling_team, tos=(file_ref(target_odcs),)
        )

        refused = await delete_as(stack, target, stack.admin_token)

        assert refused.status_code == 409, refused.text
        body = refused.json()
        assert body["type"] == "urn:dc:problem:contract-referenced"
        assert body["referrers"] == [{"contract_id": referrer, "odcs_id": referrer_odcs}]
        assert body["hidden_referrer_count"] == 0
        assert contract_row(stack, sql, target)["deleted_at"] is None
        assert events(stack, sql, "contract.deleted") == []
        assert invariant(stack.contracts_db_url) == []


async def test_referenced_file_name_link_to_a_foreign_odcs_id_follows_the_local_namesake_rule(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    """namesake: B has no contract with the ODCS id, so the link names A's; once B has one it is
    the local contract the link names, and A's is free again."""
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        editor = await role_at(stack, chain.team, "team_editor", "editor")  # nothing in `other`
        target, target_odcs = await published_with_draft(stack, chain.team)
        referrer, referrer_odcs = await published_link(
            stack, other.team, tos=(file_ref(target_odcs),)
        )

        refused = await delete_as(stack, target, editor)
        assert refused.status_code == 409, refused.text
        assert refused.json()["referrers"] == []
        assert refused.json()["hidden_referrer_count"] == 1
        assert referrer not in refused.text and referrer_odcs not in refused.text

        # B now owns a contract with that ODCS id: the link resolves locally
        namesake, _draft, _etag = await stack.create_contract(other.team, doc(target_odcs))
        deleted = await delete_as(stack, target, editor)
        assert deleted.status_code == 204, deleted.text
        assert contract_row(stack, sql, target)["deleted_at"] is not None

        # ... and it is the local namesake that is now protected
        guarded = await delete_as(stack, namesake, stack.admin_token)
        assert guarded.status_code == 409, guarded.text
        assert guarded.json()["referrers"] == [{"contract_id": referrer, "odcs_id": referrer_odcs}]
        assert invariant(stack.contracts_db_url) == []


async def test_referenced_ambiguous_file_name_link_blocks_every_namesake_fail_closed(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        org_a = await stack.chain()
        org_b = await stack.chain()
        org_c = await stack.chain()
        shared_id = new_odcs_id()
        in_a, _, _ = await stack.create_contract(org_a.team, doc(shared_id))
        in_c, _, _ = await stack.create_contract(org_c.team, doc(shared_id))
        await published_link(stack, org_b.team, tos=(file_ref(shared_id),))

        for blocked in (in_a, in_c):
            refused = await delete_as(stack, blocked, stack.admin_token)
            assert refused.status_code == 409, refused.text
            assert refused.json()["type"] == "urn:dc:problem:contract-referenced"
            assert contract_row(stack, sql, blocked)["deleted_at"] is None
        assert events(stack, sql, "contract.deleted") == []
        assert invariant(stack.contracts_db_url) == []


async def test_referenced_by_a_link_published_before_the_target_existed_still_blocks(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        sibling_team = await stack.team_in_org(chain.org, "sib")
        future_id = new_odcs_id()
        referrer, _ = await published_link(stack, sibling_team, tos=(file_ref(future_id),))

        target, _draft, _etag = await stack.create_contract(chain.team, doc(future_id))
        refused = await delete_as(stack, target, stack.admin_token)

        assert refused.status_code == 409, refused.text
        assert refused.json()["referrers"] == [
            {"contract_id": referrer, "odcs_id": reference_rows_odcs(stack, sql, referrer)}
        ]
        assert contract_row(stack, sql, target)["deleted_at"] is None


def reference_rows_odcs(stack: Any, sql: Sql, contract: str) -> str:
    return str(
        sql(stack.contracts_db_url, "SELECT odcs_id FROM contract WHERE id = %s", contract)[0][
            "odcs_id"
        ]
    )


async def test_referenced_only_by_an_unpublished_version_does_not_block(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        target, _ = await published_with_draft(stack, chain.team)
        referrer, referrer_odcs = await published_link(stack, other.team)  # 1.0.0, no link
        await publish_next_version(
            stack, referrer, referrer_odcs, "1.1.0", urls=(platform_url(target),)
        )

        blocked = await delete_as(stack, target, stack.admin_token)
        assert blocked.status_code == 409, blocked.text

        unpublished = await stack.client.post(
            f"{CONTRACTS}/{referrer}/versions/1.1.0/unpublish",
            headers=bearer(stack.admin_token),
        )
        assert unpublished.status_code == 200, unpublished.text
        deleted = await delete_as(stack, target, stack.admin_token)
        assert deleted.status_code == 204, deleted.text
        # the reference row is never removed or rewritten
        assert len(reference_rows(stack, sql, referrer)) == 1
        assert invariant(stack.contracts_db_url) == []


async def test_referenced_only_by_a_draft_does_not_block_and_drafts_only_can_be_deleted(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        target, _, _ = await stack.create_contract(chain.team, doc(new_odcs_id()))  # drafts only
        draft_referrer, _ = await published_link(
            stack, other.team, urls=(platform_url(target),), publish=False
        )
        published_referrer, published_odcs = await published_link(stack, other.team)
        await publish_next_version(
            stack,
            published_referrer,
            published_odcs,
            "1.1.0",
            urls=(platform_url(target),),
            publish=False,
        )

        deleted = await delete_as(stack, target, stack.admin_token)

        assert deleted.status_code == 204, deleted.text
        assert reference_rows(stack, sql, draft_referrer) == []
        assert reference_rows(stack, sql, published_referrer) == []


async def test_referenced_by_a_soft_deleted_contract_does_not_block(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        target, _ = await published_with_draft(stack, chain.team)
        referrer, _ = await published_link(stack, other.team, urls=(platform_url(target),))

        blocked = await delete_as(stack, target, stack.admin_token)
        assert blocked.status_code == 409, blocked.text
        assert (await delete_as(stack, referrer, stack.admin_token)).status_code == 204

        deleted = await delete_as(stack, target, stack.admin_token)
        assert deleted.status_code == 204, deleted.text


async def test_referenced_by_itself_does_not_block_by_uuid_or_by_file_name(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        by_name_odcs = new_odcs_id()
        by_name, by_name_draft, by_name_etag = await stack.create_contract(
            chain.team, linked_doc(by_name_odcs, tos=(file_ref(by_name_odcs),))
        )
        published = await stack.publish(by_name, by_name_draft, by_name_etag)
        assert published.status_code == 201, published.text

        by_uuid_odcs = new_odcs_id()
        by_uuid, by_uuid_draft, by_uuid_etag = await stack.create_contract(
            chain.team, doc(by_uuid_odcs)
        )
        await put_draft(
            stack,
            by_uuid,
            by_uuid_draft,
            by_uuid_etag,
            linked_doc(by_uuid_odcs, urls=(platform_url(by_uuid),)),
        )
        published = await stack.publish(by_uuid, by_uuid_draft, '"r2"')
        assert published.status_code == 201, published.text
        assert len(reference_rows(stack, sql, by_name)) == 1
        assert len(reference_rows(stack, sql, by_uuid)) == 1

        for contract in (by_name, by_uuid):
            deleted = await delete_as(stack, contract, stack.admin_token)
            assert deleted.status_code == 204, deleted.text


# --- bringing links back after a target was deleted (WR-03) ----------------------------------


async def restore_as(stack: Any, contract: str, token: str) -> Any:
    return await stack.client.post(f"{CONTRACTS}/{contract}/restore", headers=bearer(token))


def version_state(stack: Any, sql: Sql, contract: str, semver: str) -> str:
    return str(
        sql(
            stack.contracts_db_url,
            "SELECT state FROM contract_version WHERE contract_id = %s AND semver = %s",
            contract,
            semver,
        )[0]["state"]
    )


@pytest.mark.parametrize("link", ["platform-url", "file-name"])
async def test_republish_of_a_version_linking_to_a_deleted_contract_is_refused_until_restored(
    make_stack: MakeStack, sql: Sql, invariant: Invariant, link: str
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        target, target_odcs = await published_with_draft(stack, chain.team)
        referrer, referrer_odcs = await published_link(stack, other.team)  # 1.0.0, no link
        links: dict[str, tuple[str, ...]] = (
            {"urls": (platform_url(target),)}
            if link == "platform-url"
            else {"tos": (file_ref(target_odcs),)}
        )
        await publish_next_version(stack, referrer, referrer_odcs, "1.1.0", **links)
        republish = f"{CONTRACTS}/{referrer}/versions/1.1.0/republish"
        unpublished = await stack.client.post(
            f"{CONTRACTS}/{referrer}/versions/1.1.0/unpublish", headers=bearer(stack.admin_token)
        )
        assert unpublished.status_code == 200, unpublished.text
        assert (await delete_as(stack, target, stack.admin_token)).status_code == 204

        refused = await stack.client.post(republish, headers=bearer(stack.admin_token))

        assert refused.status_code == 409, refused.text
        body = refused.json()
        assert body["type"] == "urn:dc:problem:contract-referenced"
        assert body["deleted_target_count"] == 1
        assert body["reasons"] == [{"code": "contract-referenced", "deleted_target_count": 1}]
        # a deleted contract is never named
        assert target not in refused.text
        assert target_odcs not in refused.text
        assert version_state(stack, sql, referrer, "1.1.0") == "unpublished"
        assert events(stack, sql, "contract.version_republished") == []

        # restoring the target lifts the refusal, and the target is protected again
        assert (await restore_as(stack, target, stack.admin_token)).status_code == 200
        republished = await stack.client.post(republish, headers=bearer(stack.admin_token))
        assert republished.status_code == 200, republished.text
        assert version_state(stack, sql, referrer, "1.1.0") == "published"
        blocked = await delete_as(stack, target, stack.admin_token)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["type"] == "urn:dc:problem:contract-referenced"
        assert invariant(stack.contracts_db_url) == []


async def test_republish_of_a_version_linking_to_a_live_contract_still_works(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        target, _ = await published_with_draft(stack, chain.team)
        referrer, referrer_odcs = await published_link(stack, chain.team)
        await publish_next_version(
            stack, referrer, referrer_odcs, "1.1.0", urls=(platform_url(target),)
        )
        for action in ("unpublish", "republish"):
            done = await stack.client.post(
                f"{CONTRACTS}/{referrer}/versions/1.1.0/{action}",
                headers=bearer(stack.admin_token),
            )
            assert done.status_code == 200, (action, done.text)
        assert len(events(stack, sql, "contract.version_republished")) == 1


async def test_restore_of_a_referrer_whose_published_link_target_was_deleted_is_refused(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        target, target_odcs = await published_with_draft(stack, chain.team)
        referrer, _ = await published_link(stack, other.team, urls=(platform_url(target),))
        assert (await delete_as(stack, referrer, stack.admin_token)).status_code == 204
        assert (await delete_as(stack, target, stack.admin_token)).status_code == 204

        refused = await restore_as(stack, referrer, stack.admin_token)

        assert refused.status_code == 409, refused.text
        body = refused.json()
        assert body["type"] == "urn:dc:problem:contract-referenced"
        assert body["deleted_target_count"] == 1
        assert target not in refused.text
        assert target_odcs not in refused.text
        assert contract_row(stack, sql, referrer)["deleted_at"] is not None
        assert events(stack, sql, "contract.restored") == []

        # restore order: the target first, then the referrer; the target is protected again
        assert (await restore_as(stack, target, stack.admin_token)).status_code == 200
        restored = await restore_as(stack, referrer, stack.admin_token)
        assert restored.status_code == 200, restored.text
        assert contract_row(stack, sql, referrer)["deleted_at"] is None
        blocked = await delete_as(stack, target, stack.admin_token)
        assert blocked.status_code == 409, blocked.text
        assert invariant(stack.contracts_db_url) == []


async def test_restore_ignores_links_of_unpublished_versions_and_self_links(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        target, _ = await published_with_draft(stack, chain.team)
        referrer_odcs = new_odcs_id()
        referrer, _ = await published_link(
            stack, other.team, odcs_id=referrer_odcs, tos=(file_ref(referrer_odcs),)
        )
        await publish_next_version(
            stack,
            referrer,
            referrer_odcs,
            "1.1.0",
            urls=(platform_url(target),),
            tos=(file_ref(referrer_odcs),),
        )
        unpublished = await stack.client.post(
            f"{CONTRACTS}/{referrer}/versions/1.1.0/unpublish", headers=bearer(stack.admin_token)
        )
        assert unpublished.status_code == 200, unpublished.text
        assert (await delete_as(stack, referrer, stack.admin_token)).status_code == 204
        assert (await delete_as(stack, target, stack.admin_token)).status_code == 204

        restored = await restore_as(stack, referrer, stack.admin_token)

        assert restored.status_code == 200, restored.text
        assert version_state(stack, sql, referrer, "1.1.0") == "unpublished"
