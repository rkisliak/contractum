"""Template authoring (TMPL-01): create a draft, publish immutable numbered versions, deprecate.

Tracer: a Policy Manager creates a template at an Organization and publishes version 1; the
published bytes equal the authored bytes and the database refuses to change them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
TEMPLATES = "/api/v1/contracts/templates"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def v32_yaml() -> bytes:
    return (FIXTURES / "valid-v3.2.0.yaml").read_bytes()


async def create(
    stack: Any,
    folder_id: str,
    token: str,
    body: bytes | None = None,
    *,
    name: str = "Orders template",
    media_type: str = "application/yaml",
) -> Any:
    return await stack.client.post(
        f"/api/v1/contracts/folders/{folder_id}/templates",
        params={"name": name},
        content=v32_yaml() if body is None else body,
        headers={**bearer(token), "Content-Type": media_type},
    )


async def publish(stack: Any, template_id: str, token: str) -> Any:
    return await stack.client.post(f"{TEMPLATES}/{template_id}/publish", headers=bearer(token))


def _stable(response: Any) -> dict[str, Any]:
    body = dict(response.json())
    for volatile in ("request_id", "instance", "trace_id"):
        body.pop(volatile, None)
    return body


def events(stack: Any, sql: Callable[..., Any]) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_type, aggregate_id, aggregate_version, before, after "
        "FROM outbox ORDER BY id",
    )


async def test_policy_manager_creates_a_template_and_publishes_version_1_byte_for_byte(
    make_stack: MakeStack, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        token, principal = stack.token("pm")
        stack.grant_sql(principal, "policy_manager", chain.org)
        raw = v32_yaml()

        created = await create(stack, chain.org, token, raw)
        assert created.status_code == 201, created.text
        body = created.json()
        template_id = body["template"]["id"]
        assert created.headers["location"] == f"{TEMPLATES}/{template_id}"
        assert body["template"]["publish_counter"] == 0
        assert body["template"]["name"] == "Orders template"
        assert body["template"]["folder_id"] == chain.org
        assert body["template"]["organization_id"] == chain.org
        assert body["structural_validation"]["valid"] is True

        published = await publish(stack, template_id, token)
        assert published.status_code == 201, published.text
        assert published.json()["version"]["version_no"] == 1
        assert published.json()["template"]["publish_counter"] == 1

        document = await stack.client.get(
            f"{TEMPLATES}/{template_id}/versions/1/document", headers=bearer(token)
        )
        assert document.status_code == 200
        assert document.content == raw  # comments and formatting survive
        assert document.headers["content-type"] == "application/yaml"
        assert document.headers["x-content-type-options"] == "nosniff"

        for who in (token, stack.admin_token):
            one = await stack.client.get(f"{TEMPLATES}/{template_id}", headers=bearer(who))
            assert one.status_code == 200, one.text
            assert one.json()["latest_version_no"] == 1
            assert one.json()["draft_redacted"] is False


async def test_template_events_carry_metadata_only_and_a_bounded_name(
    make_stack: MakeStack, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        token, principal = stack.token("pm")
        stack.grant_sql(principal, "policy_manager", chain.org)
        created = await create(stack, chain.org, token, name="n" * 200)
        assert created.status_code == 201, created.text
        published = await publish(stack, created.json()["template"]["id"], token)
        assert published.status_code == 201, published.text

        recorded = events(stack, sql)
        assert [e["action"] for e in recorded] == ["template.created", "template.published"]
        assert {e["aggregate_type"] for e in recorded} == {"template"}
        text = json.dumps(recorded)
        for fragment in ("Records every order", "checkout", "costCentre", "schema", "description"):
            assert fragment not in text
        assert recorded[1]["aggregate_version"] == 1
        assert recorded[1]["after"]["version"]["version_no"] == 1
        assert len(recorded[0]["after"]["name"]) == 200


async def test_name_over_the_bound_is_rejected_and_a_missing_name_is_a_validation_error(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        too_long = await create(stack, chain.org, stack.admin_token, name="n" * 201)
        assert too_long.status_code == 422
        missing = await stack.client.post(
            f"/api/v1/contracts/folders/{chain.org}/templates",
            content=v32_yaml(),
            headers={**bearer(stack.admin_token), "Content-Type": "application/yaml"},
        )
        assert missing.status_code == 422


async def test_a_template_can_be_created_at_any_folder_kind_including_root(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        for folder_id in (stack.root_id, chain.org, chain.project, chain.team):
            created = await create(stack, folder_id, stack.admin_token)
            assert created.status_code == 201, (folder_id, created.text)
        root = await create(stack, stack.root_id, stack.admin_token)
        assert root.json()["template"]["organization_id"] is None


async def test_published_versions_are_immutable_in_the_database(
    make_stack: MakeStack, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await create(stack, chain.org, stack.admin_token)
        template_id = created.json()["template"]["id"]
        assert (await publish(stack, template_id, stack.admin_token)).status_code == 201

        url = stack.contracts_db_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, autocommit=True) as conn:
            for statement in (
                "UPDATE template_version SET raw_document = 'x'::bytea",
                "UPDATE template_version SET document = '{}'::jsonb",
                "UPDATE template_version SET version_no = 9",
                "DELETE FROM template_version",
            ):
                with pytest.raises(psycopg.errors.RaiseException):
                    conn.execute(statement)  # type: ignore[call-overload]
            conn.execute("UPDATE template_version SET deprecated_at = now(), deprecated_by = 'x'")
        rows = sql(
            stack.contracts_db_url,
            "SELECT version_no, deprecated_by, size_bytes FROM template_version",
        )
        assert rows == [
            {"version_no": 1, "deprecated_by": "x", "size_bytes": len(v32_yaml())},
        ]


async def test_unknown_and_unreadable_templates_are_the_same_404(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await create(stack, chain.org, stack.admin_token)
        template_id = created.json()["template"]["id"]
        token, _ = stack.token("stranger")
        unreadable = await stack.client.get(f"{TEMPLATES}/{template_id}", headers=bearer(token))
        unknown = await stack.client.get(f"{TEMPLATES}/{uuid.uuid4()}", headers=bearer(token))
        assert unreadable.status_code == unknown.status_code == 404
        assert unreadable.json()["type"] == unknown.json()["type"] == "urn:dc:problem:not-found"
        # Identical but for the per-request correlation id.
        assert _stable(unreadable) == _stable(unknown)


async def test_requests_without_a_token_are_401(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.client.post(
            f"/api/v1/contracts/folders/{chain.org}/templates",
            params={"name": "x"},
            content=v32_yaml(),
            headers={"Content-Type": "application/yaml"},
        )
        assert created.status_code == 401


# ---- Task 2: draft editing, version list, deprecation and the publish edge cases ----------------


def changed(text_from: str = "Payments Orders", text_to: str = "Payments Orders v2") -> bytes:
    return v32_yaml().replace(text_from.encode(), text_to.encode())


async def put_draft(
    stack: Any,
    template_id: str,
    token: str,
    body: bytes,
    *,
    name: str | None = None,
    media_type: str = "application/yaml",
) -> Any:
    return await stack.client.put(
        f"{TEMPLATES}/{template_id}/draft",
        params={"name": name} if name is not None else {},
        content=body,
        headers={**bearer(token), "Content-Type": media_type},
    )


async def deprecate(stack: Any, template_id: str, version_no: int, token: str) -> Any:
    return await stack.client.post(
        f"{TEMPLATES}/{template_id}/versions/{version_no}/deprecate", headers=bearer(token)
    )


async def versions(stack: Any, template_id: str, token: str) -> list[dict[str, Any]]:
    response = await stack.client.get(f"{TEMPLATES}/{template_id}/versions", headers=bearer(token))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def assert_problem(response: Any, status: int, slug: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == f"urn:dc:problem:{slug}"


async def _pm_template(stack: Any, sub: str = "pm") -> tuple[Any, str, str]:
    chain = await stack.chain()
    token, principal = stack.token(sub)
    stack.grant_sql(principal, "policy_manager", chain.org)
    created = await create(stack, chain.org, token)
    assert created.status_code == 201, created.text
    return chain, token, created.json()["template"]["id"]


async def test_draft_update_changes_the_hash_and_records_one_event_per_real_change(
    make_stack: MakeStack, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        _, token, template_id = await _pm_template(stack)
        first = (await stack.client.get(f"{TEMPLATES}/{template_id}", headers=bearer(token))).json()

        edited = await put_draft(stack, template_id, token, changed())
        assert edited.status_code == 200, edited.text
        body = edited.json()["template"]
        assert body["draft_content_sha256"] != first["draft_content_sha256"]
        assert [e["action"] for e in events(stack, sql)] == [
            "template.created",
            "template.draft_updated",
        ]

        same = await put_draft(stack, template_id, token, changed())
        assert same.status_code == 200
        assert same.json()["template"]["draft_content_sha256"] == body["draft_content_sha256"]
        assert len(events(stack, sql)) == 2  # unchanged bytes and name: no event

        renamed = await put_draft(stack, template_id, token, changed(), name="New name")
        assert renamed.status_code == 200
        assert renamed.json()["template"]["name"] == "New name"
        assert len(events(stack, sql)) == 3
        recorded = events(stack, sql)[-1]
        assert recorded["before"]["name"] == "Orders template"
        assert recorded["after"]["name"] == "New name"
        assert "Payments" not in json.dumps(recorded)


async def test_draft_document_returns_the_draft_bytes_and_converts_on_accept(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        _, token, template_id = await _pm_template(stack)
        edited = changed()
        assert (await put_draft(stack, template_id, token, edited)).status_code == 200

        draft = await stack.client.get(f"{TEMPLATES}/{template_id}/document", headers=bearer(token))
        assert draft.status_code == 200
        assert draft.content == edited
        assert draft.headers["content-type"] == "application/yaml"

        converted = await stack.client.get(
            f"{TEMPLATES}/{template_id}/document",
            headers={**bearer(token), "Accept": "application/json"},
        )
        assert converted.status_code == 200
        assert converted.headers["content-type"] == "application/json"
        assert converted.headers["vary"] == "Accept"
        assert converted.headers["x-content-type-options"] == "nosniff"
        assert json.loads(converted.content)["name"] == "Payments Orders v2"


async def test_publish_after_a_change_is_version_2_and_versions_list_ascending(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        _, token, template_id = await _pm_template(stack)
        assert (await publish(stack, template_id, token)).status_code == 201
        assert (await put_draft(stack, template_id, token, changed())).status_code == 200
        second = await publish(stack, template_id, token)
        assert second.status_code == 201
        assert second.json()["version"]["version_no"] == 2

        listed = await versions(stack, template_id, token)
        assert [v["version_no"] for v in listed] == [1, 2]
        assert [v["deprecated"] for v in listed] == [False, False]
        v1 = await stack.client.get(
            f"{TEMPLATES}/{template_id}/versions/1/document", headers=bearer(token)
        )
        v2 = await stack.client.get(
            f"{TEMPLATES}/{template_id}/versions/2/document", headers=bearer(token)
        )
        assert v1.content == v32_yaml()
        assert v2.content == changed()


async def test_deprecate_marks_the_version_once_and_keeps_it_readable(
    make_stack: MakeStack, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        _, token, template_id = await _pm_template(stack)
        assert (await publish(stack, template_id, token)).status_code == 201
        _, principal = stack.token("pm")

        first = await deprecate(stack, template_id, 1, token)
        assert first.status_code == 200, first.text
        assert first.json()["deprecated"] is True
        assert first.json()["deprecated_by"] == principal
        assert [e["action"] for e in events(stack, sql)][-1] == "template.version_deprecated"
        count = len(events(stack, sql))

        again = await deprecate(stack, template_id, 1, token)
        assert again.status_code == 200
        assert again.json()["deprecated"] is True
        assert again.json()["deprecated_at"] == first.json()["deprecated_at"]
        assert len(events(stack, sql)) == count  # already deprecated: no event

        assert_problem(await deprecate(stack, template_id, 9, token), 404, "not-found")
        still = await stack.client.get(
            f"{TEMPLATES}/{template_id}/versions/1/document", headers=bearer(token)
        )
        assert still.status_code == 200
        assert still.content == v32_yaml()


async def test_idempotency_publishing_an_unchanged_draft_returns_the_existing_version(
    make_stack: MakeStack, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        _, token, template_id = await _pm_template(stack)
        assert (await publish(stack, template_id, token)).status_code == 201
        assert (await put_draft(stack, template_id, token, changed())).status_code == 200
        assert (await publish(stack, template_id, token)).status_code == 201
        count = len(events(stack, sql))

        again = await publish(stack, template_id, token)
        assert again.status_code == 200, again.text
        assert again.json()["version"]["version_no"] == 2
        assert again.json()["template"]["publish_counter"] == 2
        assert len(events(stack, sql)) == count
        assert [v["version_no"] for v in await versions(stack, template_id, token)] == [1, 2]


async def test_concurrency_two_publishes_of_a_changed_draft_make_exactly_one_version(
    make_stack: MakeStack, sql: Callable[..., Any]
) -> None:
    import asyncio

    async with make_stack() as stack:
        _, token, template_id = await _pm_template(stack)
        assert (await publish(stack, template_id, token)).status_code == 201
        assert (await put_draft(stack, template_id, token, changed())).status_code == 200
        assert (await publish(stack, template_id, token)).status_code == 201
        assert (
            await put_draft(stack, template_id, token, changed("Payments Orders", "Orders three"))
        ).status_code == 200

        left, right = await asyncio.gather(
            publish(stack, template_id, token), publish(stack, template_id, token)
        )
        assert sorted([left.status_code, right.status_code]) == [200, 201]
        for response in (left, right):
            assert response.json()["version"]["version_no"] == 3
            assert response.json()["template"]["publish_counter"] == 3
        assert [v["version_no"] for v in await versions(stack, template_id, token)] == [1, 2, 3]
        published = [e for e in events(stack, sql) if e["action"] == "template.published"]
        assert len(published) == 3


async def test_identical_draft_is_republished_after_the_latest_version_is_deprecated(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        _, token, template_id = await _pm_template(stack)
        assert (await publish(stack, template_id, token)).status_code == 201
        assert (await put_draft(stack, template_id, token, changed())).status_code == 200
        assert (await publish(stack, template_id, token)).status_code == 201
        assert (await deprecate(stack, template_id, 2, token)).status_code == 200

        again = await publish(stack, template_id, token)
        assert again.status_code == 201, again.text
        assert again.json()["version"]["version_no"] == 3
        listed = await versions(stack, template_id, token)
        assert [(v["version_no"], v["deprecated"]) for v in listed] == [
            (1, False),
            (2, True),
            (3, False),
        ]


async def test_draft_documents_are_gated_like_contracts(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        _, token, template_id = await _pm_template(stack)
        unsupported = await put_draft(
            stack, template_id, token, v32_yaml().replace(b"v3.2.0", b"v3.0.2")
        )
        assert_problem(unsupported, 422, "unsupported-odcs-version")
        no_kind = await put_draft(
            stack, template_id, token, v32_yaml().replace(b"kind: DataContract\n", b"")
        )
        assert no_kind.status_code == 422, no_kind.text
        assert_problem(
            await put_draft(stack, template_id, token, b"a: [unclosed"), 422, "malformed-document"
        )
        huge = await put_draft(stack, template_id, token, v32_yaml() + b"#" + b"x" * 1_048_576)
        assert_problem(huge, 413, "payload-too-large")
        wrong_type = await put_draft(stack, template_id, token, v32_yaml(), media_type="text/plain")
        assert_problem(wrong_type, 415, "unsupported-media-type")
        # nothing above changed the stored draft
        draft = await stack.client.get(f"{TEMPLATES}/{template_id}/document", headers=bearer(token))
        assert draft.content == v32_yaml()


async def test_template_writes_in_an_archived_folder_are_409(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain, token, template_id = await _pm_template(stack)
        assert (await publish(stack, template_id, token)).status_code == 201
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.org}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code == 200, archived.text

        assert_problem(
            await put_draft(stack, template_id, token, changed()), 409, "folder-archived"
        )
        assert_problem(await publish(stack, template_id, token), 409, "folder-archived")
        assert_problem(await deprecate(stack, template_id, 1, token), 409, "folder-archived")
        assert_problem(await create(stack, chain.org, token), 409, "folder-archived")
        # reads stay possible
        assert (
            await stack.client.get(f"{TEMPLATES}/{template_id}", headers=bearer(token))
        ).status_code == 200


async def test_an_out_of_range_version_number_is_a_422_not_a_500(make_stack: MakeStack) -> None:
    """WR-01: the column is a 32-bit integer, so every route bounds the path value."""
    async with make_stack() as stack:
        _chain, token, template_id = await _pm_template(stack)
        assert (await publish(stack, template_id, token)).status_code == 201
        for bad in ("2147483648", "0", "-1", "99999999999999999999"):
            document = await stack.client.get(
                f"{TEMPLATES}/{template_id}/versions/{bad}/document", headers=bearer(token)
            )
            assert document.status_code == 422, (bad, document.text)
            assert (await deprecate(stack, template_id, int(bad), token)).status_code == 422
        ok = await stack.client.get(
            f"{TEMPLATES}/{template_id}/versions/2147483647/document", headers=bearer(token)
        )
        assert ok.status_code == 404
