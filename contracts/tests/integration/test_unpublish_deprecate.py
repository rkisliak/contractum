"""Unpublish, republish and deprecate published versions (VER-06, VER-07, D-39, D-49, D-52).

Unpublishing hides a version from everyone without ``contract:publish`` but never deletes it; the
current Latest cannot be unpublished. Republishing restores the same bytes as a Preview or an older
version. Deprecation is a flag plus a sunset date that consumers see in headers and listings.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import psycopg
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
    return f"urn:dc:unpublish:{uuid.uuid4().hex}"


def base(contract: str) -> str:
    return f"/api/v1/contracts/contracts/{contract}"


def versions_url(contract: str, ref: str | None = None, *, metadata: bool = False) -> str:
    url = f"{base(contract)}/versions"
    if ref is None:
        return url
    return f"{url}/{ref}/metadata" if metadata else f"{url}/{ref}"


def action_url(contract: str, ref: str, action: str) -> str:
    return f"{base(contract)}/versions/{ref}/{action}"


def events(stack: Any, sql: Sql) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_id, before, after FROM outbox ORDER BY id",
    )


def row_of(stack: Any, sql: Sql, contract: str, semver: str) -> dict[str, Any]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM contract_version WHERE contract_id = %s AND semver = %s",
        contract,
        semver,
    )[0]


def stable(response: Any) -> tuple[int, str | None, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, response.headers.get("content-type"), sorted(body.items())


async def publish_version(
    stack: Any, contract: str, odcs_id: str, version: str, *, from_version: str
) -> dict[str, Any]:
    created = await stack.client.post(
        f"{base(contract)}/drafts",
        json={"from_version": from_version, "bump": "none"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        f"{base(contract)}/drafts/{draft['id']}",
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


async def three_versions(stack: Any, team: str) -> tuple[str, str]:
    """1.0.0 (Latest), 1.1.0 and 1.2.0 (Previews); returns (contract id, ODCS id)."""
    odcs_id = new_odcs_id()
    contract, draft, etag = await stack.create_contract(team, doc(odcs_id))
    first = await stack.publish(contract, draft, etag)
    assert first.status_code == 201, first.text
    await publish_version(stack, contract, odcs_id, "1.1.0", from_version="1.0.0")
    await publish_version(stack, contract, odcs_id, "1.2.0", from_version="1.1.0")
    return contract, odcs_id


async def consumer_at(stack: Any, team: str, sub: str = "cs") -> str:
    token, principal = stack.token(sub)
    stack.grant_sql(principal, "consumer", team)
    return token


async def team_reader_at(stack: Any, team: str) -> str:
    token, principal = stack.token("reader")
    stack.grant_sql(principal, "team_reader", team)
    return token


async def editor_at(stack: Any, team: str) -> str:
    token, principal = stack.token("editor")
    stack.grant_sql(principal, "team_editor", team)
    return token


async def semvers(stack: Any, contract: str, token: str) -> list[tuple[str, str, str]]:
    response = await stack.client.get(versions_url(contract), headers=bearer(token))
    assert response.status_code == 200, response.text
    return [(i["semver"], i["status"], i["lifecycle"]) for i in response.json()["items"]]


async def test_unpublish_keeps_the_row_and_bytes_and_hides_the_version_from_consumers(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        consumer = await consumer_at(stack, chain.team)
        reader = await team_reader_at(stack, chain.team)
        editor = await editor_at(stack, chain.team)
        before = row_of(stack, sql, contract, "1.1.0")
        count = len(events(stack, sql))

        response = await stack.client.post(
            action_url(contract, "1.1.0", "unpublish"), headers=bearer(editor)
        )
        assert response.status_code == 200, response.text
        assert response.json()["lifecycle"] == "unpublished"
        assert response.json()["semver"] == "1.1.0"

        after = row_of(stack, sql, contract, "1.1.0")
        assert after["state"] == "unpublished"
        assert bytes(after["raw_document"]) == bytes(before["raw_document"])
        assert after["content_sha256"] == before["content_sha256"]
        assert after["unpublished_by"] == "fake:editor"
        assert after["unpublished_at"] is not None

        new_events = events(stack, sql)[count:]
        assert [e["action"] for e in new_events] == ["contract.version_unpublished"]
        assert "raw_document" not in str(new_events[0]["after"])

        # Consumers and a Team reader no longer see it, and the 404 is the unknown-contract 404.
        for token in (consumer, reader):
            assert [s for s, _st, _lc in await semvers(stack, contract, token)] == [
                "1.2.0",
                "1.0.0",
            ]
            for make_url in (
                lambda c: versions_url(c, "1.1.0"),
                lambda c: versions_url(c, "1.1.0", metadata=True),
            ):
                real = await stack.client.get(make_url(contract), headers=bearer(token))
                missing = await stack.client.get(make_url(UNKNOWN), headers=bearer(token))
                assert real.status_code == 404, real.text
                assert stable(real) == stable(missing)

        # The owner still lists and fetches it.
        listed = await semvers(stack, contract, editor)
        assert ("1.1.0", "preview", "unpublished") in listed
        fetched = await stack.client.get(versions_url(contract, "1.1.0"), headers=bearer(editor))
        assert fetched.status_code == 200, fetched.text
        assert fetched.content == bytes(before["raw_document"])
        assert invariant(stack.contracts_db_url) == []


async def test_unpublishing_an_unpublished_version_again_is_a_no_op_without_an_event(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        admin = bearer(stack.admin_token)
        first = await stack.client.post(action_url(contract, "1.1.0", "unpublish"), headers=admin)
        assert first.status_code == 200, first.text
        count = len(events(stack, sql))

        again = await stack.client.post(action_url(contract, "1.1.0", "unpublish"), headers=admin)
        assert again.status_code == 200, again.text
        assert again.json()["lifecycle"] == "unpublished"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_unpublishing_the_latest_is_409_and_a_direct_sql_attempt_is_rejected(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        count = len(events(stack, sql))

        response = await stack.client.post(
            action_url(contract, "1.0.0", "unpublish"), headers=bearer(stack.admin_token)
        )
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "urn:dc:problem:latest-version-protected"
        assert row_of(stack, sql, contract, "1.0.0")["state"] == "published"
        assert len(events(stack, sql)) == count

        with pytest.raises(psycopg.errors.CheckViolation, match="latest_is_published"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET state = 'unpublished', unpublished_at = now() "
                "WHERE contract_id = %s AND semver = '1.0.0'",
                contract,
            )

        # Once another version is the Latest, the former one may be unpublished.
        promoted = await stack.client.post(
            action_url(contract, "1.2.0", "promote"), headers=bearer(stack.admin_token)
        )
        assert promoted.status_code == 200, promoted.text
        ok = await stack.client.post(
            action_url(contract, "1.0.0", "unpublish"), headers=bearer(stack.admin_token)
        )
        assert ok.status_code == 200, ok.text
        assert invariant(stack.contracts_db_url) == []


async def test_republish_restores_the_same_bytes_as_a_preview_never_as_the_latest(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        consumer = await consumer_at(stack, chain.team)
        admin = bearer(stack.admin_token)
        original = await stack.client.get(versions_url(contract, "1.1.0"), headers=admin)
        assert original.status_code == 200
        assert (
            await stack.client.post(action_url(contract, "1.1.0", "unpublish"), headers=admin)
        ).status_code == 200
        count = len(events(stack, sql))

        response = await stack.client.post(
            action_url(contract, "1.1.0", "republish"), headers=admin
        )
        assert response.status_code == 200, response.text
        assert response.json()["lifecycle"] == "published"
        assert response.json()["status"] == "preview"

        again = await stack.client.get(versions_url(contract, "1.1.0"), headers=bearer(consumer))
        assert again.status_code == 200, again.text
        assert again.content == original.content
        assert again.headers["etag"] == original.headers["etag"]
        row = row_of(stack, sql, contract, "1.1.0")
        assert row["unpublished_at"] is None
        assert row["unpublished_by"] is None
        assert not row["is_latest"]
        assert [e["action"] for e in events(stack, sql)[count:]] == ["contract.version_republished"]

        # No-ops: republishing a published version, including one never unpublished.
        count = len(events(stack, sql))
        for ref in ("1.1.0", "1.2.0"):
            noop = await stack.client.post(action_url(contract, ref, "republish"), headers=admin)
            assert noop.status_code == 200, noop.text
            assert noop.json()["lifecycle"] == "published"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_a_version_older_than_the_latest_is_republished_as_older(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        admin = bearer(stack.admin_token)
        assert (
            await stack.client.post(action_url(contract, "1.2.0", "promote"), headers=admin)
        ).status_code == 200
        assert (
            await stack.client.post(action_url(contract, "1.0.0", "unpublish"), headers=admin)
        ).status_code == 200
        response = await stack.client.post(
            action_url(contract, "1.0.0", "republish"), headers=admin
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "older"
        assert invariant(stack.contracts_db_url) == []


async def test_an_unpublished_version_cannot_be_promoted_and_its_number_is_never_reused(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, odcs_id = await three_versions(stack, chain.team)
        admin = bearer(stack.admin_token)
        assert (
            await stack.client.post(action_url(contract, "1.1.0", "unpublish"), headers=admin)
        ).status_code == 200

        promote = await stack.client.post(action_url(contract, "1.1.0", "promote"), headers=admin)
        assert promote.status_code == 409, promote.text
        assert promote.json()["type"] == "urn:dc:problem:version-not-published"

        # A new draft declaring the unpublished number cannot be published.
        created = await stack.client.post(
            f"{base(contract)}/drafts",
            json={"from_version": "1.2.0", "bump": "none"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        draft = created.json()["draft"]
        saved = await stack.client.put(
            f"{base(contract)}/drafts/{draft['id']}",
            content=doc(odcs_id, version="1.1.0"),
            headers={**admin, "Content-Type": "application/yaml", "If-Match": draft["etag"]},
        )
        assert saved.status_code == 200, saved.text
        published = await stack.publish(contract, draft["id"], saved.headers["etag"])
        assert published.status_code == 409, published.text
        assert published.json()["type"] == "urn:dc:problem:semver-conflict"
        assert invariant(stack.contracts_db_url) == []


async def test_unpublish_and_republish_deny_a_team_reader_consumers_and_archived_folders(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        reader = await team_reader_at(stack, chain.team)
        consumer = await consumer_at(stack, chain.team)
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", chain.org)
        count = len(events(stack, sql))

        for action in ("unpublish", "republish"):
            denied = await stack.client.post(
                action_url(contract, "1.1.0", action), headers=bearer(reader)
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["type"] == "urn:dc:problem:forbidden"
            for token in (consumer, manager):
                real = await stack.client.post(
                    action_url(contract, "1.1.0", action), headers=bearer(token)
                )
                missing = await stack.client.post(
                    action_url(UNKNOWN, "1.1.0", action), headers=bearer(token)
                )
                assert real.status_code == 404, real.text
                assert stable(real) == stable(missing)
            for ref in ("latest", "9.9.9"):
                unknown = await stack.client.post(
                    action_url(contract, ref, action), headers=bearer(stack.admin_token)
                )
                assert unknown.status_code == 404, unknown.text
        assert row_of(stack, sql, contract, "1.1.0")["state"] == "published"
        assert len(events(stack, sql)) == count

        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.team}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code in (200, 204), archived.text
        for action in ("unpublish", "republish"):
            response = await stack.client.post(
                action_url(contract, "1.1.0", action), headers=bearer(stack.admin_token)
            )
            assert response.status_code == 409, response.text
            assert response.json()["type"] == "urn:dc:problem:folder-archived"
        assert invariant(stack.contracts_db_url) == []


def today() -> date:
    return datetime.now(UTC).date()


def sunset_header(day: date) -> str:
    return format_datetime(
        datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC), usegmt=True
    )


async def deprecate(
    stack: Any, contract: str, ref: str, day: date | str, *, token: str | None = None
) -> Any:
    return await stack.client.post(
        action_url(contract, ref, "deprecate"),
        json={"sunset_date": day.isoformat() if isinstance(day, date) else day},
        headers=bearer(token or stack.admin_token),
    )


async def test_deprecating_a_version_flags_it_and_adds_headers_to_the_200_and_the_304(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        consumer = await consumer_at(stack, chain.team)
        # 1.0.0 is the Latest; 1.1.0 is a Preview: deprecate 1.0.0 once 1.2.0 is the Latest.
        promoted = await stack.client.post(
            action_url(contract, "1.2.0", "promote"), headers=bearer(stack.admin_token)
        )
        assert promoted.status_code == 200, promoted.text
        plain = await stack.client.get(versions_url(contract, "1.0.0"), headers=bearer(consumer))
        assert "deprecation" not in plain.headers
        assert "sunset" not in plain.headers
        before = row_of(stack, sql, contract, "1.0.0")
        count = len(events(stack, sql))
        day = today() + timedelta(days=30)

        response = await deprecate(stack, contract, "1.0.0", day)
        assert response.status_code == 200, response.text
        out = response.json()
        assert out["deprecated"] is True
        assert out["sunset_date"] == day.isoformat()
        assert out["deprecated_at"] is not None
        assert out["status"] == "older"

        row = row_of(stack, sql, contract, "1.0.0")
        deprecation = f"@{int(row['deprecated_at'].timestamp())}"
        assert row["deprecated_by"]
        assert bytes(row["raw_document"]) == bytes(before["raw_document"])
        assert row["content_sha256"] == before["content_sha256"]

        fetched = await stack.client.get(versions_url(contract, "1.0.0"), headers=bearer(consumer))
        assert fetched.status_code == 200, fetched.text
        assert fetched.content == plain.content
        assert fetched.headers["etag"] == plain.headers["etag"]
        assert fetched.headers["deprecation"] == deprecation
        assert fetched.headers["sunset"] == sunset_header(day)

        conditional = await stack.client.get(
            versions_url(contract, "1.0.0"),
            headers={**bearer(consumer), "If-None-Match": fetched.headers["etag"]},
        )
        assert conditional.status_code == 304
        assert conditional.headers["deprecation"] == deprecation
        assert conditional.headers["sunset"] == sunset_header(day)

        metadata = await stack.client.get(
            versions_url(contract, "1.0.0", metadata=True), headers=bearer(consumer)
        )
        assert metadata.status_code == 200
        assert metadata.json()["deprecated"] is True
        assert metadata.json()["sunset_date"] == day.isoformat()
        assert metadata.headers["deprecation"] == deprecation
        assert metadata.headers["sunset"] == sunset_header(day)
        listed = await stack.client.get(versions_url(contract), headers=bearer(consumer))
        flags = {i["semver"]: (i["deprecated"], i["sunset_date"]) for i in listed.json()["items"]}
        assert flags["1.0.0"] == (True, day.isoformat())
        assert flags["1.1.0"] == (False, None)

        assert [e["action"] for e in events(stack, sql)[count:]] == ["contract.version_deprecated"]
        assert invariant(stack.contracts_db_url) == []


async def test_deprecating_again_with_the_same_date_is_a_no_op_and_a_new_date_changes_only_it(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        day = today() + timedelta(days=10)
        first = await deprecate(stack, contract, "1.1.0", day)
        assert first.status_code == 200, first.text
        stamped = row_of(stack, sql, contract, "1.1.0")["deprecated_at"]
        count = len(events(stack, sql))

        same = await deprecate(stack, contract, "1.1.0", day)
        assert same.status_code == 200, same.text
        assert len(events(stack, sql)) == count

        later = day + timedelta(days=20)
        moved = await deprecate(stack, contract, "1.1.0", later)
        assert moved.status_code == 200, moved.text
        assert moved.json()["sunset_date"] == later.isoformat()
        row = row_of(stack, sql, contract, "1.1.0")
        assert row["deprecated_at"] == stamped
        assert str(row["sunset_date"]) == later.isoformat()
        assert [e["action"] for e in events(stack, sql)[count:]] == ["contract.version_deprecated"]
        assert invariant(stack.contracts_db_url) == []


async def test_deprecate_rejects_a_past_date_unknown_fields_and_an_unpublished_version(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        admin = bearer(stack.admin_token)
        count = len(events(stack, sql))

        past = await deprecate(stack, contract, "1.1.0", today() - timedelta(days=1))
        assert past.status_code == 422, past.text
        assert past.json()["type"] == "urn:dc:problem:invalid-sunset-date"
        for body in ({"sunset_date": "2999-01-01", "extra": 1}, {}, {"sunset_date": "soon"}):
            bad = await stack.client.post(
                action_url(contract, "1.1.0", "deprecate"), json=body, headers=admin
            )
            assert bad.status_code == 422, (body, bad.text)

        assert (
            await stack.client.post(action_url(contract, "1.1.0", "unpublish"), headers=admin)
        ).status_code == 200
        count = len(events(stack, sql))
        hidden = await deprecate(stack, contract, "1.1.0", today() + timedelta(days=5))
        assert hidden.status_code == 409, hidden.text
        assert hidden.json()["type"] == "urn:dc:problem:version-not-published"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_the_current_latest_may_be_deprecated_and_promote_clears_the_deprecation(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        consumer = await consumer_at(stack, chain.team)
        admin = bearer(stack.admin_token)
        day = today() + timedelta(days=3)

        flagged = await deprecate(stack, contract, "1.0.0", day)
        assert flagged.status_code == 200, flagged.text
        assert flagged.json()["status"] == "latest"
        assert flagged.json()["deprecated"] is True

        # Moving the Latest away and promoting the deprecated version back clears the flag.
        assert (
            await stack.client.post(action_url(contract, "1.2.0", "promote"), headers=admin)
        ).status_code == 200
        back = await stack.client.post(action_url(contract, "1.0.0", "promote"), headers=admin)
        assert back.status_code == 200, back.text
        assert back.json()["deprecated"] is False
        assert back.json()["sunset_date"] is None
        assert back.json()["deprecated_at"] is None
        fetched = await stack.client.get(versions_url(contract, "latest"), headers=bearer(consumer))
        assert fetched.status_code == 200
        assert "deprecation" not in fetched.headers
        assert "sunset" not in fetched.headers
        assert invariant(stack.contracts_db_url) == []


async def test_a_passed_sunset_date_never_blocks_the_fetch(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        consumer = await consumer_at(stack, chain.team)
        assert (await deprecate(stack, contract, "1.1.0", today())).status_code == 200
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET sunset_date = current_date - 10 "
            "WHERE contract_id = %s AND semver = '1.1.0'",
            contract,
        )
        past = today() - timedelta(days=10)
        fetched = await stack.client.get(versions_url(contract, "1.1.0"), headers=bearer(consumer))
        assert fetched.status_code == 200, fetched.text
        assert fetched.headers["sunset"] == sunset_header(past)
        assert "deprecation" in fetched.headers
        assert invariant(stack.contracts_db_url) == []


async def test_deprecate_denies_a_team_reader_consumers_and_archived_folders(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        reader = await team_reader_at(stack, chain.team)
        consumer = await consumer_at(stack, chain.team)
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", chain.org)
        day = today() + timedelta(days=5)
        count = len(events(stack, sql))

        denied = await deprecate(stack, contract, "1.1.0", day, token=reader)
        assert denied.status_code == 403, denied.text
        for token in (consumer, manager):
            real = await deprecate(stack, contract, "1.1.0", day, token=token)
            missing = await deprecate(stack, UNKNOWN, "1.1.0", day, token=token)
            assert real.status_code == 404, real.text
            assert stable(real) == stable(missing)
        for ref in ("latest", "9.9.9"):
            unknown = await deprecate(stack, contract, ref, day)
            assert unknown.status_code == 404, unknown.text
        assert row_of(stack, sql, contract, "1.1.0")["deprecated_at"] is None
        assert len(events(stack, sql)) == count

        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.team}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code in (200, 204), archived.text
        response = await deprecate(stack, contract, "1.1.0", day)
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "urn:dc:problem:folder-archived"
        assert invariant(stack.contracts_db_url) == []


LIST = "/api/v1/contracts/contracts"


async def list_row(stack: Any, team: str, contract: str, token: str) -> dict[str, Any]:
    """The contract's row in the per-folder tree list (D-06: the badge reads the row itself)."""
    response = await stack.client.get(
        LIST,
        params={"folder_id": team, "include_subtree": "false", "sort": "name"},
        headers=bearer(token),
    )
    assert response.status_code == 200, response.text
    rows = [item for item in response.json()["items"] if item["id"] == contract]
    assert len(rows) == 1, response.text
    return dict(rows[0])


async def test_the_list_row_and_metadata_report_deprecated_for_a_deprecated_head(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        admin = stack.admin_token
        assert (await list_row(stack, chain.team, contract, admin))["version"][
            "deprecated"
        ] is False

        flagged = await deprecate(stack, contract, "1.0.0", today() + timedelta(days=30))
        assert flagged.status_code == 200, flagged.text
        assert flagged.json()["status"] == "latest"

        row = await list_row(stack, chain.team, contract, admin)
        assert row["version"]["deprecated"] is True
        metadata = await stack.client.get(base(contract), headers=bearer(admin))
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["version"]["deprecated"] is True


async def test_deprecating_an_older_version_leaves_the_head_row_false(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        admin = stack.admin_token
        promoted = await stack.client.post(
            action_url(contract, "1.2.0", "promote"), headers=bearer(admin)
        )
        assert promoted.status_code == 200, promoted.text

        older = await deprecate(stack, contract, "1.0.0", today() + timedelta(days=30))
        assert older.status_code == 200, older.text
        assert older.json()["status"] == "older"

        row = await list_row(stack, chain.team, contract, admin)
        assert row["version"]["deprecated"] is False
        metadata = await stack.client.get(base(contract), headers=bearer(admin))
        assert metadata.json()["version"]["deprecated"] is False


async def test_a_new_contract_reports_deprecated_false_in_create_and_list(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, doc(new_odcs_id()))
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["version"]["deprecated"] is False
        assert body["contract"]["version"]["deprecated"] is False
        row = await list_row(stack, chain.team, body["contract"]["id"], stack.admin_token)
        assert row["version"]["deprecated"] is False


async def test_a_policy_manager_row_carries_deprecated_but_no_content(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await three_versions(stack, chain.team)
        flagged = await deprecate(stack, contract, "1.0.0", today() + timedelta(days=30))
        assert flagged.status_code == 200, flagged.text
        manager = stack.policy_manager(chain.org)

        row = await list_row(stack, chain.team, contract, manager)
        assert row["version"]["deprecated"] is True
        assert row["content_redacted"] is True
        assert row["domain"] is None
        assert row["tags"] is None
