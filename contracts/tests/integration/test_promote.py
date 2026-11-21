"""Promote a Preview or an older published version to Latest (VER-05, D-39, D-53).

The owner moves the Latest pointer with ``POST .../versions/{semver}/promote``. Exactly one Latest
exists at all times, a promote of the current Latest is a no-op without an event, and only a
published version can be promoted.
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
    return f"urn:dc:promote:{uuid.uuid4().hex}"


def versions_url(contract: str, ref: str | None = None) -> str:
    base = f"/api/v1/contracts/contracts/{contract}/versions"
    return base if ref is None else f"{base}/{ref}"


def promote_url(contract: str, ref: str) -> str:
    return f"{versions_url(contract, ref)}/promote"


def drafts_url(contract: str) -> str:
    return f"/api/v1/contracts/contracts/{contract}/drafts"


def events(stack: Any, sql: Sql) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_id, before, after FROM outbox ORDER BY id",
    )


def stable(response: Any) -> tuple[int, str | None, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, response.headers.get("content-type"), sorted(body.items())


async def publish_version(
    stack: Any, contract: str, odcs_id: str, version: str, *, from_version: str
) -> dict[str, Any]:
    created = await stack.client.post(
        drafts_url(contract),
        json={"from_version": from_version, "bump": "none"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        f"{drafts_url(contract)}/{draft['id']}",
        content=doc(odcs_id, version=version),
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": draft["etag"],
        },
    )
    assert saved.status_code == 200, saved.text
    published = await stack.publish(contract, draft["id"], saved.headers["etag"])
    assert published.status_code == 201, published.text
    return dict(published.json())


async def two_versions(stack: Any, team: str) -> tuple[str, str]:
    """A contract with 1.0.0 (Latest) and 1.1.0 (Preview); returns (contract id, ODCS id)."""
    odcs_id = new_odcs_id()
    contract, draft, etag = await stack.create_contract(team, doc(odcs_id))
    first = await stack.publish(contract, draft, etag)
    assert first.status_code == 201, first.text
    second = await publish_version(stack, contract, odcs_id, "1.1.0", from_version="1.0.0")
    assert second["status"] == "preview"
    return contract, odcs_id


async def listed(stack: Any, contract: str, token: str) -> list[tuple[str, str]]:
    response = await stack.client.get(versions_url(contract), headers=bearer(token))
    assert response.status_code == 200, response.text
    return [(i["semver"], i["status"]) for i in response.json()["items"]]


async def consumer_at(stack: Any, team: str, sub: str = "cs") -> str:
    token, principal = stack.token(sub)
    stack.grant_sql(principal, "consumer", team)
    return token


async def test_promoting_a_preview_makes_it_the_latest_every_consumer_fetches(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await two_versions(stack, chain.team)
        consumer = await consumer_at(stack, chain.team)
        before = len(events(stack, sql))

        response = await stack.client.post(
            promote_url(contract, "1.1.0"), headers=bearer(stack.admin_token)
        )
        assert response.status_code == 200, response.text
        out = response.json()
        assert out["semver"] == "1.1.0"
        assert out["status"] == "latest"
        assert out["lifecycle"] == "published"

        assert await listed(stack, contract, consumer) == [("1.1.0", "latest"), ("1.0.0", "older")]
        latest = await stack.client.get(versions_url(contract, "latest"), headers=bearer(consumer))
        pinned = await stack.client.get(versions_url(contract, "1.1.0"), headers=bearer(consumer))
        assert latest.status_code == 200, latest.text
        assert latest.content == pinned.content
        assert latest.headers["etag"] == pinned.headers["etag"] == f'"{out["content_sha256"]}"'

        new_events = events(stack, sql)[before:]
        assert [e["action"] for e in new_events] == ["contract.version_promoted"]
        event = new_events[0]
        assert event["aggregate_id"] == contract
        assert event["before"]["latest_semver"] == "1.0.0"
        assert event["after"]["latest_semver"] == "1.1.0"
        assert event["before"]["latest_version_id"] != event["after"]["latest_version_id"]
        assert event["after"]["latest_version_id"] == out["version_id"]
        assert "document" not in event["after"]["version"]
        assert "raw_document" not in event["after"]["version"]
        assert invariant(stack.contracts_db_url) == []


async def test_promoting_an_older_version_rolls_back_and_the_old_latest_becomes_a_preview(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await two_versions(stack, chain.team)
        admin = bearer(stack.admin_token)
        assert (
            await stack.client.post(promote_url(contract, "1.1.0"), headers=admin)
        ).status_code == 200

        back = await stack.client.post(promote_url(contract, "1.0.0"), headers=admin)
        assert back.status_code == 200, back.text
        assert back.json()["status"] == "latest"
        assert await listed(stack, contract, stack.admin_token) == [
            ("1.1.0", "preview"),
            ("1.0.0", "latest"),
        ]
        assert invariant(stack.contracts_db_url) == []


async def test_promoting_the_current_latest_is_a_200_no_op_without_an_event(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await two_versions(stack, chain.team)
        admin = bearer(stack.admin_token)
        count = len(events(stack, sql))

        again = await stack.client.post(promote_url(contract, "1.0.0"), headers=admin)
        assert again.status_code == 200, again.text
        assert again.json()["status"] == "latest"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_promote_addresses_versions_by_semver_only(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await two_versions(stack, chain.team)
        admin = bearer(stack.admin_token)
        count = len(events(stack, sql))

        for ref in ("latest", "9.9.9", "v1.1.0", "not-a-version"):
            response = await stack.client.post(promote_url(contract, ref), headers=admin)
            assert response.status_code == 404, (ref, response.text)
            assert response.json()["type"] == "urn:dc:problem:not-found"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_a_team_reader_is_403_and_consumers_and_policy_managers_get_the_unknown_404(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await two_versions(stack, chain.team)
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        consumer = await consumer_at(stack, chain.team)
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", chain.org)
        count = len(events(stack, sql))

        denied = await stack.client.post(promote_url(contract, "1.1.0"), headers=bearer(reader))
        assert denied.status_code == 403, denied.text
        assert denied.json()["type"] == "urn:dc:problem:forbidden"
        for token in (consumer, manager):
            real = await stack.client.post(promote_url(contract, "1.1.0"), headers=bearer(token))
            missing = await stack.client.post(promote_url(UNKNOWN, "1.1.0"), headers=bearer(token))
            assert real.status_code == 404, real.text
            assert stable(real) == stable(missing)
        anonymous = await stack.client.post(promote_url(contract, "1.1.0"))
        assert anonymous.status_code == 401

        assert await listed(stack, contract, stack.admin_token) == [
            ("1.1.0", "preview"),
            ("1.0.0", "latest"),
        ]
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_promote_in_an_archived_folder_is_409_folder_archived(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await two_versions(stack, chain.team)
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.team}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code in (200, 204), archived.text
        response = await stack.client.post(
            promote_url(contract, "1.1.0"), headers=bearer(stack.admin_token)
        )
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "urn:dc:problem:folder-archived"
        assert invariant(stack.contracts_db_url) == []


async def test_promoting_an_unpublished_version_is_409_version_not_published(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await two_versions(stack, chain.team)
        # The lifecycle write route for unpublish arrives in the next task; state it directly.
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET state = 'unpublished', unpublished_at = now() "
            "WHERE contract_id = %s AND semver = '1.1.0'",
            contract,
        )
        count = len(events(stack, sql))
        response = await stack.client.post(
            promote_url(contract, "1.1.0"), headers=bearer(stack.admin_token)
        )
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "urn:dc:problem:version-not-published"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_promote_leaves_exactly_one_latest_when_two_promotes_race(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    import asyncio

    async with make_stack() as stack:
        chain = await stack.chain()
        contract, odcs = await two_versions(stack, chain.team)
        await publish_version(stack, contract, odcs, "1.2.0", from_version="1.1.0")
        admin = bearer(stack.admin_token)
        results = await asyncio.gather(
            stack.client.post(promote_url(contract, "1.1.0"), headers=admin),
            stack.client.post(promote_url(contract, "1.2.0"), headers=admin),
            stack.client.post(promote_url(contract, "1.0.0"), headers=admin),
        )
        assert [r.status_code for r in results] == [200, 200, 200], [r.text for r in results]
        rows = sql(
            stack.contracts_db_url,
            "SELECT count(*) AS n FROM contract_version WHERE contract_id = %s AND is_latest",
            contract,
        )
        assert rows[0]["n"] == 1
        assert invariant(stack.contracts_db_url) == []
