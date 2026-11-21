"""Draft identity, revisions and If-Match optimistic concurrency (CONT-03, D-42, A10)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
SECRET_TEXT = "TOPSECRET-description-text"  # noqa: S105


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, name: str = "Orders", *, version: str = "1.0.0") -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: {name}\n"
        f"description:\n  purpose: {SECRET_TEXT}\n"
        "customProperties:\n  - property: apiKey\n    value: TOPSECRET-custom-property\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def draft_url(contract: str, draft: str) -> str:
    return f"/api/v1/contracts/contracts/{contract}/drafts/{draft}"


async def put(
    stack: Any,
    contract: str,
    draft: str,
    body: bytes,
    *,
    if_match: str | None,
    token: str | None = None,
    media_type: str = "application/yaml",
) -> Any:
    headers = {**bearer(token or stack.admin_token), "Content-Type": media_type}
    if if_match is not None:
        headers["If-Match"] = if_match
    return await stack.client.put(draft_url(contract, draft), content=body, headers=headers)


async def make_contract(stack: Any, team: str) -> tuple[str, str, str]:
    odcs_id = f"urn:dc:drafts:{uuid.uuid4().hex}"
    created = await stack.post(team, doc(odcs_id))
    assert created.status_code == 201, created.text
    body = created.json()
    return body["contract"]["id"], body["draft"]["id"], odcs_id


def stored(stack: Any, sql: Sql, contract: str) -> dict[str, Any]:
    return sql(
        stack.contracts_db_url,
        "SELECT id, revision, raw_document, content_sha256 FROM contract_version "
        "WHERE contract_id = %s",
        contract,
    )[0]


def events(stack: Any, sql: Sql) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_type, aggregate_id, aggregate_version, before, after "
        "FROM outbox ORDER BY id",
    )


async def test_create_returns_the_draft_reference_and_keeps_the_version_summary(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, doc(f"urn:dc:drafts:{uuid.uuid4().hex}"))
        assert created.status_code == 201, created.text
        body = created.json()
        row = stored(stack, sql, body["contract"]["id"])
        assert body["draft"] == {"id": str(row["id"]), "revision": 1, "etag": '"r1"'}
        assert body["version"] == {"number": 1, "state": "draft"}


async def test_put_with_the_current_etag_saves_bumps_the_revision_and_records_one_event(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, odcs_id = await make_contract(stack, chain.team)
        before_events = len(events(stack, sql))

        new_bytes = doc(odcs_id, "Orders v2", version="1.1.0")
        response = await put(stack, contract, draft, new_bytes, if_match='"r1"')
        assert response.status_code == 200, response.text
        assert response.headers["etag"] == '"r2"'
        body = response.json()
        assert body["draft"]["revision"] == 2
        assert body["draft"]["etag"] == '"r2"'
        assert body["draft"]["declared_version"] == "1.1.0"
        assert "structural_validation" in body
        assert bytes(stored(stack, sql, contract)["raw_document"]) == new_bytes

        new = events(stack, sql)[before_events:]
        assert len(new) == 1
        event = new[0]
        assert event["action"] == "contract.draft_updated"
        assert event["aggregate_type"] == "contract"
        assert event["aggregate_id"] == contract
        assert event["aggregate_version"] == 2
        # D-42 / T-03-03: ids, revision, state, hash, size only; never document content.
        dumped = json.dumps([event["before"], event["after"]])
        for forbidden in ("schema", "customProperties", SECRET_TEXT, "TOPSECRET"):
            assert forbidden not in dumped
        assert event["before"]["revision"] == 1
        assert event["after"]["revision"] == 2


async def test_a_stale_if_match_is_412_and_leaves_the_draft_and_the_outbox_alone(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, odcs_id = await make_contract(stack, chain.team)
        first = await put(stack, contract, draft, doc(odcs_id, "v2"), if_match='"r1"')
        assert first.status_code == 200
        snapshot = stored(stack, sql, contract)
        count = len(events(stack, sql))

        stale = await put(stack, contract, draft, doc(odcs_id, "v3"), if_match='"r1"')
        assert stale.status_code == 412, stale.text
        assert stale.json()["type"] == "urn:dc:problem:precondition-failed"
        assert '"r1"' not in stale.text
        assert stored(stack, sql, contract) == snapshot
        assert len(events(stack, sql)) == count


async def test_a_missing_if_match_is_428_and_the_body_is_never_parsed(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, _ = await make_contract(stack, chain.team)
        snapshot = stored(stack, sql, contract)
        # malformed YAML: a 422 would mean the body was parsed before the header was checked.
        response = await put(stack, contract, draft, b"::: [not yaml", if_match=None)
        assert response.status_code == 428, response.text
        assert response.json()["type"] == "urn:dc:problem:precondition-required"
        blank = await put(stack, contract, draft, b"::: [not yaml", if_match="  ")
        assert blank.status_code == 428
        assert stored(stack, sql, contract) == snapshot


async def test_a_weak_tag_never_matches_and_star_matches_an_existing_draft(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, odcs_id = await make_contract(stack, chain.team)
        first = await put(stack, contract, draft, doc(odcs_id, "v2"), if_match='"r1"')
        assert first.status_code == 200

        weak = await put(stack, contract, draft, doc(odcs_id, "v3"), if_match='W/"r2"')
        assert weak.status_code == 412, weak.text
        garbage = await put(stack, contract, draft, doc(odcs_id, "v3"), if_match="r2")
        assert garbage.status_code == 412

        star = await put(stack, contract, draft, doc(odcs_id, "v3"), if_match="*")
        assert star.status_code == 200, star.text
        assert star.json()["draft"]["revision"] == 3
        assert stored(stack, sql, contract)["revision"] == 3


async def test_a_document_id_that_differs_from_the_contracts_is_422_and_changes_nothing(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, odcs_id = await make_contract(stack, chain.team)
        snapshot = stored(stack, sql, contract)
        count = len(events(stack, sql))
        response = await put(
            stack, contract, draft, doc("urn:dc:drafts:someone-else"), if_match='"r1"'
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["type"] == "urn:dc:problem:odcs-id-mismatch"
        # quick 261119-wk8: the refusal names both ids so the caller can fix the document.
        assert body["expected_odcs_id"] == odcs_id
        assert body["found_odcs_id"] == "urn:dc:drafts:someone-else"
        assert odcs_id in body["detail"]
        assert stored(stack, sql, contract) == snapshot
        assert len(events(stack, sql)) == count


async def test_a_save_of_identical_bytes_keeps_the_etag_and_records_no_event(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = f"urn:dc:drafts:{uuid.uuid4().hex}"
        original = doc(odcs_id)
        created = await stack.post(chain.team, original)
        contract = created.json()["contract"]["id"]
        draft = created.json()["draft"]["id"]
        count = len(events(stack, sql))
        response = await put(stack, contract, draft, original, if_match='"r1"')
        assert response.status_code == 200, response.text
        assert response.headers["etag"] == '"r1"'
        assert response.json()["draft"]["revision"] == 1
        assert len(events(stack, sql)) == count


async def test_an_unknown_draft_and_an_unknown_contract_are_the_same_404(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _, odcs_id = await make_contract(stack, chain.team)
        unknown_draft = await put(stack, contract, UNKNOWN, doc(odcs_id), if_match='"r1"')
        unknown_contract = await put(stack, UNKNOWN, UNKNOWN, doc(odcs_id), if_match='"r1"')
        assert unknown_draft.status_code == 404
        assert unknown_contract.status_code == 404

        def stable(response: Any) -> list[tuple[str, Any]]:
            body = dict(response.json())
            for volatile in ("request_id", "instance"):
                body.pop(volatile, None)
            return sorted(body.items())

        assert stable(unknown_draft) == stable(unknown_contract)


def stable(response: Any) -> tuple[int, str | None, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, response.headers.get("content-type"), sorted(body.items())


async def test_list_get_and_document_serve_the_draft_with_its_etag(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, odcs_id = await make_contract(stack, chain.team)
        new_bytes = doc(odcs_id, "Orders v2", version="2.0.0")
        assert (await put(stack, contract, draft, new_bytes, if_match='"r1"')).status_code == 200
        headers = bearer(stack.admin_token)
        base = f"/api/v1/contracts/contracts/{contract}/drafts"

        listed = await stack.client.get(base, headers=headers)
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert [i["id"] for i in items] == [draft]
        assert items[0]["revision"] == 2
        assert items[0]["etag"] == '"r2"'
        assert items[0]["declared_version"] == "2.0.0"

        one = await stack.client.get(f"{base}/{draft}", headers=headers)
        assert one.status_code == 200, one.text
        assert one.json()["etag"] == '"r2"'
        assert one.headers["etag"] == one.json()["etag"]
        assert one.json()["contract_id"] == contract

        document = await stack.client.get(f"{base}/{draft}/document", headers=headers)
        assert document.status_code == 200
        assert document.content == new_bytes
        assert document.headers["content-type"] == "application/yaml"
        assert document.headers["etag"] == '"r2"'
        assert document.headers["vary"] == "Accept"
        assert document.headers["x-content-type-options"] == "nosniff"
        assert document.headers["cache-control"] == "private, no-cache"

        converted = await stack.client.get(
            f"{base}/{draft}/document", headers={**headers, "Accept": "application/json"}
        )
        assert converted.status_code == 200
        assert converted.headers["content-type"] == "application/json"
        assert converted.headers["etag"] == '"r2-json"'
        assert converted.json()["id"] == odcs_id

        missing = await stack.client.get(f"{base}/{UNKNOWN}", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["type"] == "urn:dc:problem:not-found"


async def test_declared_version_is_null_when_the_document_version_is_not_a_string(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = f"urn:dc:drafts:{uuid.uuid4().hex}"
        body = f"apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: 1\n".encode()
        created = await stack.post(chain.team, body)
        assert created.status_code == 201, created.text
        contract = created.json()["contract"]["id"]
        listed = await stack.client.get(
            f"/api/v1/contracts/contracts/{contract}/drafts", headers=bearer(stack.admin_token)
        )
        assert listed.json()["items"][0]["declared_version"] is None


async def test_a_team_reader_reads_drafts_but_gets_403_on_put(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, odcs_id = await make_contract(stack, chain.team)
        token, principal = stack.token("reader")
        stack.grant_sql(principal, "team_reader", chain.team)
        base = f"/api/v1/contracts/contracts/{contract}/drafts"
        for url in (base, f"{base}/{draft}", f"{base}/{draft}/document"):
            assert (await stack.client.get(url, headers=bearer(token))).status_code == 200, url
        denied = await put(stack, contract, draft, doc(odcs_id, "x"), if_match='"r1"', token=token)
        assert denied.status_code == 403, denied.text
        assert denied.json()["type"] == "urn:dc:problem:forbidden"


async def test_no_existence_oracle_for_consumers_and_policy_managers_on_draft_routes(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, odcs_id = await make_contract(stack, chain.team)
        consumer, consumer_principal = stack.token("cs")
        stack.grant_sql(consumer_principal, "consumer", chain.team)
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", chain.org)
        base = f"/api/v1/contracts/contracts/{contract}/drafts"
        unknown = f"/api/v1/contracts/contracts/{UNKNOWN}/drafts"

        for token in (consumer, manager):
            pairs = [
                (base, unknown),
                (f"{base}/{draft}", f"{unknown}/{draft}"),
                (f"{base}/{draft}/document", f"{unknown}/{draft}/document"),
            ]
            for real, missing in pairs:
                got = await stack.client.get(real, headers=bearer(token))
                reference = await stack.client.get(missing, headers=bearer(token))
                assert got.status_code == 404, real
                assert stable(got) == stable(reference), real
            real_put = await put(
                stack, contract, draft, doc(odcs_id, "x"), if_match='"r1"', token=token
            )
            missing_put = await put(
                stack, UNKNOWN, draft, doc(odcs_id, "x"), if_match='"r1"', token=token
            )
            assert real_put.status_code == 404
            assert stable(real_put) == stable(missing_put)


async def test_draft_writes_in_an_archived_folder_are_409_but_reads_still_work(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, odcs_id = await make_contract(stack, chain.team)
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.team}/archive",
            headers=bearer(stack.admin_token),
        )
        assert archived.status_code in (200, 204), archived.text

        response = await put(stack, contract, draft, doc(odcs_id, "x"), if_match='"r1"')
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "urn:dc:problem:folder-archived"
        listed = await stack.client.get(
            f"/api/v1/contracts/contracts/{contract}/drafts", headers=bearer(stack.admin_token)
        )
        assert listed.status_code == 200


# --- Plan 03-04: the draft cap and discarding drafts (D-42, A7) ---


def drafts_url(contract: str, draft: str | None = None) -> str:
    base = f"/api/v1/contracts/contracts/{contract}/drafts"
    return base if draft is None else f"{base}/{draft}"


async def new_draft(stack: Any, contract: str, body: dict[str, Any]) -> Any:
    return await stack.client.post(
        drafts_url(contract), json=body, headers=bearer(stack.admin_token)
    )


async def delete(
    stack: Any, contract: str, draft: str, *, if_match: str | None, token: str | None = None
) -> Any:
    headers = bearer(token or stack.admin_token)
    if if_match is not None:
        headers["If-Match"] = if_match
    return await stack.client.delete(drafts_url(contract, draft), headers=headers)


async def test_the_draft_cap_is_409_until_a_draft_is_discarded(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(max_drafts_per_contract=2) as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, doc(f"urn:dc:drafts:{uuid.uuid4().hex}"))
        contract = created.json()["contract"]["id"]
        first = created.json()["draft"]
        second = await new_draft(stack, contract, {"from_draft_id": first["id"]})
        assert second.status_code == 201, second.text

        capped = await new_draft(stack, contract, {"from_draft_id": first["id"]})
        assert capped.status_code == 409, capped.text
        assert capped.json()["type"] == "urn:dc:problem:draft-limit-reached"
        rows = sql(stack.contracts_db_url, "SELECT id FROM contract_version")
        assert len(rows) == 2

        gone = await delete(stack, contract, second.json()["draft"]["id"], if_match='"r1"')
        assert gone.status_code == 204, gone.text
        assert gone.content == b""
        again = await new_draft(stack, contract, {"from_draft_id": first["id"]})
        assert again.status_code == 201, again.text
        # Row numbers are never reused.
        numbers = sorted(
            row["version_no"]
            for row in sql(stack.contracts_db_url, "SELECT version_no FROM contract_version")
        )
        assert numbers == [1, 3]


async def test_delete_needs_if_match_and_refuses_stale_published_and_last_rows(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, doc(f"urn:dc:drafts:{uuid.uuid4().hex}"))
        contract = created.json()["contract"]["id"]
        only = created.json()["draft"]["id"]
        count = len(sql(stack.contracts_db_url, "SELECT id FROM outbox"))

        assert (await delete(stack, contract, only, if_match=None)).status_code == 428
        protected = await delete(stack, contract, only, if_match='"r1"')
        assert protected.status_code == 409, protected.text
        assert protected.json()["type"] == "urn:dc:problem:last-draft-protected"

        extra = (await new_draft(stack, contract, {"from_draft_id": only})).json()["draft"]
        assert (await delete(stack, contract, extra["id"], if_match='"r7"')).status_code == 412
        assert (await delete(stack, contract, extra["id"], if_match='W/"r1"')).status_code == 412
        assert (await delete(stack, contract, UNKNOWN, if_match='"r1"')).status_code == 404

        published = await stack.publish(contract, only, '"r1"')
        assert published.status_code == 201, published.text
        already = await delete(stack, contract, only, if_match='"r1"')
        assert already.status_code == 409
        assert already.json()["type"] == "urn:dc:problem:draft-already-published"

        before = len(sql(stack.contracts_db_url, "SELECT id FROM outbox"))
        assert (
            await delete(stack, contract, extra["id"], if_match=extra["etag"])
        ).status_code == 204
        events = sql(stack.contracts_db_url, "SELECT action, before, after FROM outbox ORDER BY id")
        assert len(events) == before + 1
        assert events[-1]["action"] == "contract.draft_deleted"
        assert events[-1]["after"] is None
        assert events[-1]["before"]["id"] == extra["id"]
        assert "schema" not in json.dumps(events[-1]["before"])
        assert count < before
        remaining = sql(stack.contracts_db_url, "SELECT id FROM contract_version")
        assert [str(row["id"]) for row in remaining] == [only]


async def test_a_team_reader_cannot_delete_and_a_consumer_gets_the_unknown_404(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, doc(f"urn:dc:drafts:{uuid.uuid4().hex}"))
        contract = created.json()["contract"]["id"]
        draft = created.json()["draft"]["id"]
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        consumer, consumer_principal = stack.token("cs")
        stack.grant_sql(consumer_principal, "consumer", chain.team)
        assert (
            await delete(stack, contract, draft, if_match='"r1"', token=reader)
        ).status_code == 403
        real = await delete(stack, contract, draft, if_match='"r1"', token=consumer)
        missing = await delete(stack, UNKNOWN, draft, if_match='"r1"', token=consumer)
        assert real.status_code == missing.status_code == 404
