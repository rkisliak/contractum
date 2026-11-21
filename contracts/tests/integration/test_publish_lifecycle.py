"""Publish a draft as the first version and the Latest (VER-01, VER-02, VER-03, VER-08, D-47)."""

from __future__ import annotations

import asyncio
import json
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
SECRET_TEXT = "TOPSECRET-description-text"  # noqa: S105


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *, version: str = "1.0.0", quoted: bool = True) -> bytes:
    shown = f'"{version}"' if quoted else version
    return (
        f"apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: {shown}\n"
        f"status: draft\nname: Orders\n"
        f"description:\n  purpose: {SECRET_TEXT}\n"
        "customProperties:\n  - property: apiKey\n    value: TOPSECRET-custom-property\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def new_odcs_id() -> str:
    return f"urn:dc:publish:{uuid.uuid4().hex}"


def events(stack: Any, sql: Sql) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_id, before, after FROM outbox ORDER BY id",
    )


def version_row(stack: Any, sql: Sql, draft: str) -> dict[str, Any]:
    return sql(stack.contracts_db_url, "SELECT * FROM contract_version WHERE id = %s", draft)[0]


async def test_first_publish_is_201_and_the_draft_row_becomes_the_latest_version(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        body = doc(new_odcs_id())
        contract, draft, etag = await stack.create_contract(chain.team, body)
        assert etag == '"r1"'
        before = version_row(stack, sql, draft)
        count = len(events(stack, sql))

        response = await stack.publish(contract, draft, etag)
        assert response.status_code == 201, response.text
        out = response.json()
        sha = before["content_sha256"]
        fetch_url = f"/api/v1/contracts/contracts/{contract}/versions/1.0.0"
        assert out == {
            "version_id": draft,
            "contract_id": contract,
            "semver": "1.0.0",
            "status": "latest",
            "lifecycle": "published",
            "content_sha256": sha,
            "etag": f'"{sha}"',
            "fetch_url": fetch_url,
        }
        assert response.headers["location"] == fetch_url
        assert response.headers["etag"] == f'"{sha}"'

        row = version_row(stack, sql, draft)
        assert str(row["id"]) == draft
        assert bytes(row["raw_document"]) == body
        assert row["state"] == "published"
        assert row["is_latest"] is True
        assert (row["semver_major"], row["semver_minor"], row["semver_patch"]) == (1, 0, 0)
        assert row["semver_prerelease"] == ""
        assert row["published_by"] is not None
        assert row["published_at"] is not None
        pointer = sql(
            stack.contracts_db_url, "SELECT latest_version_id FROM contract WHERE id = %s", contract
        )[0]["latest_version_id"]
        assert str(pointer) == draft

        new = events(stack, sql)[count:]
        assert [event["action"] for event in new] == ["contract.version_published"]
        dumped = json.dumps([new[0]["before"], new[0]["after"]])
        for forbidden in ("schema", "customProperties", SECRET_TEXT, "TOPSECRET"):
            assert forbidden not in dumped
        assert new[0]["after"]["status"] == "latest"
        assert new[0]["after"]["semver"] == "1.0.0"
        assert invariant(stack.contracts_db_url) == []


async def test_a_published_draft_leaves_the_drafts_list(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_odcs_id()))
        assert (await stack.publish(contract, draft, etag)).status_code == 201
        listed = await stack.client.get(
            f"/api/v1/contracts/contracts/{contract}/drafts", headers=bearer(stack.admin_token)
        )
        assert listed.status_code == 200
        assert draft not in [item["id"] for item in listed.json()["items"]]
        assert invariant(stack.contracts_db_url) == []


async def test_an_unquoted_yaml_number_version_is_refused_by_governance_and_changes_nothing(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(new_odcs_id(), version="1.0", quoted=False)
        )
        count = len(events(stack, sql))
        response = await stack.publish(contract, draft, etag)
        assert response.status_code == 422, response.text
        # Under the real gate (D-129) governance refuses a non-string version first, as a
        # structural error at /version, before the SemVer rule would answer invalid-version.
        assert response.json()["type"] == "urn:dc:problem:contract-validation-failed"
        structural = response.json()["report"]["structural"]
        assert [(e["pointer"], e["keyword"]) for e in structural["errors"]] == [
            ("/version", "type")
        ]
        assert version_row(stack, sql, draft)["state"] == "draft"
        # Nothing changes; the refusal itself is audited as one publish_refused event (D-175).
        assert [e["action"] for e in events(stack, sql)[count:]] == [
            "contract.version.publish_refused"
        ]
        assert invariant(stack.contracts_db_url) == []


async def test_a_team_reader_is_403_and_consumers_and_policy_managers_get_the_unknown_404(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_odcs_id()))
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        consumer, consumer_principal = stack.token("cs")
        stack.grant_sql(consumer_principal, "consumer", chain.team)
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", chain.org)
        count = len(events(stack, sql))

        denied = await stack.publish(contract, draft, etag, token=reader)
        assert denied.status_code == 403, denied.text
        assert denied.json()["type"] == "urn:dc:problem:forbidden"

        def stable(response: Any) -> tuple[int, str | None, list[tuple[str, Any]]]:
            body = dict(response.json())
            for volatile in ("request_id", "instance"):
                body.pop(volatile, None)
            return response.status_code, response.headers.get("content-type"), sorted(body.items())

        for token in (consumer, manager):
            real = await stack.publish(contract, draft, etag, token=token)
            missing = await stack.publish(UNKNOWN, draft, etag, token=token)
            assert real.status_code == 404, real.text
            assert stable(real) == stable(missing)

        assert version_row(stack, sql, draft)["state"] == "draft"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_a_missing_if_match_is_428_and_a_stale_one_is_412_with_no_change_or_event(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, _etag = await stack.create_contract(chain.team, doc(new_odcs_id()))
        count = len(events(stack, sql))

        missing = await stack.publish(contract, draft, None)
        assert missing.status_code == 428, missing.text
        assert missing.json()["type"] == "urn:dc:problem:precondition-required"

        stale = await stack.publish(contract, draft, '"r9"')
        assert stale.status_code == 412, stale.text
        assert stale.json()["type"] == "urn:dc:problem:precondition-failed"
        assert '"r9"' not in stale.text

        assert version_row(stack, sql, draft)["state"] == "draft"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_publish_in_an_archived_folder_is_409_folder_archived(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_odcs_id()))
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.team}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code in (200, 204), archived.text
        response = await stack.publish(contract, draft, etag)
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "urn:dc:problem:folder-archived"
        assert version_row(stack, sql, draft)["state"] == "draft"
        assert invariant(stack.contracts_db_url) == []


async def test_a_draft_changed_after_the_etag_was_read_is_412_not_published(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_odcs_id()
        contract, draft, etag = await stack.create_contract(chain.team, doc(odcs_id))
        saved = await stack.client.put(
            f"/api/v1/contracts/contracts/{contract}/drafts/{draft}",
            content=doc(odcs_id, version="1.1.0"),
            headers={
                **bearer(stack.admin_token),
                "Content-Type": "application/yaml",
                "If-Match": etag,
            },
        )
        assert saved.status_code == 200, saved.text
        stale = await stack.publish(contract, draft, etag)
        assert stale.status_code == 412, stale.text
        assert version_row(stack, sql, draft)["state"] == "draft"
        fresh = await stack.publish(contract, draft, saved.headers["etag"])
        assert fresh.status_code == 201, fresh.text
        assert fresh.json()["semver"] == "1.1.0"
        assert invariant(stack.contracts_db_url) == []


async def test_the_gate_runs_on_the_stored_bytes_and_a_gate_failure_publishes_nothing(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    class RefusingGate:
        async def check(self, raw: bytes, media_type: str, settings: Any, ctx: Any) -> Any:
            from dc_contracts.ingest import parse_upload
            from dc_core.problems import ProblemError

            await parse_upload(raw, media_type, settings)
            raise ProblemError(422, "validation-error", "Unprocessable Content", "gate refused")

    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_odcs_id()))
        real_gate = stack.contracts_app.state.publish_gate
        stack.contracts_app.state.publish_gate = RefusingGate()
        count = len(events(stack, sql))
        response = await stack.publish(contract, draft, etag)
        stack.contracts_app.state.publish_gate = real_gate
        assert response.status_code == 422, response.text
        assert version_row(stack, sql, draft)["state"] == "draft"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


# ---- fetch of published versions (API-01, API-03, D-48) ----

YAML_WITH_COMMENT = (
    "# keep this comment byte-for-byte\n"
    'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
    "status: draft\nname: Orders\n"
)


def versions_url(contract: str, ref: str) -> str:
    return f"/api/v1/contracts/contracts/{contract}/versions/{ref}"


def stable(response: Any) -> tuple[int, str | None, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, response.headers.get("content-type"), sorted(body.items())


async def test_latest_and_semver_serve_the_stored_bytes_with_a_strong_etag(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        body = YAML_WITH_COMMENT.format(odcs_id=new_odcs_id()).encode()
        contract, draft, etag = await stack.create_contract(chain.team, body)
        published = await stack.publish(contract, draft, etag)
        assert published.status_code == 201, published.text
        sha = published.json()["content_sha256"]

        urls = [
            versions_url(contract, "latest"),
            versions_url(contract, "1.0.0"),
            published.json()["fetch_url"],
        ]
        for url in urls:
            response = await stack.client.get(url, headers=bearer(stack.admin_token))
            assert response.status_code == 200, response.text
            assert response.content == body
            assert response.headers["content-type"] == "application/yaml"
            assert response.headers["etag"] == f'"{sha}"'
            assert response.headers["vary"] == "Accept"
            assert response.headers["cache-control"] == "private, no-cache"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["content-disposition"] == "inline"
        assert invariant(stack.contracts_db_url) == []


async def test_accept_for_the_other_format_converts_with_its_own_etag_and_html_is_406(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        body = YAML_WITH_COMMENT.format(odcs_id=new_odcs_id()).encode()
        contract, draft, etag = await stack.create_contract(chain.team, body)
        sha = (await stack.publish(contract, draft, etag)).json()["content_sha256"]

        converted = await stack.client.get(
            versions_url(contract, "latest"),
            headers={**bearer(stack.admin_token), "Accept": "application/json"},
        )
        assert converted.status_code == 200, converted.text
        assert converted.headers["content-type"] == "application/json"
        assert converted.headers["etag"] == f'"{sha}-json"'
        assert json.loads(converted.content)["version"] == "1.0.0"

        refused = await stack.client.get(
            versions_url(contract, "latest"),
            headers={**bearer(stack.admin_token), "Accept": "text/html"},
        )
        assert refused.status_code == 406, refused.text
        assert invariant(stack.contracts_db_url) == []


async def test_a_consumer_and_a_team_reader_fetch_but_a_policy_manager_gets_the_unknown_404(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        body = doc(new_odcs_id())
        contract, draft, etag = await stack.create_contract(chain.team, body)
        assert (await stack.publish(contract, draft, etag)).status_code == 201

        consumer, consumer_principal = stack.token("cs")
        stack.grant_sql(consumer_principal, "consumer", chain.team)
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", chain.org)

        for token in (consumer, reader):
            response = await stack.client.get(
                versions_url(contract, "latest"), headers=bearer(token)
            )
            assert response.status_code == 200, response.text
            assert response.content == body

        real = await stack.client.get(versions_url(contract, "latest"), headers=bearer(manager))
        missing = await stack.client.get(versions_url(UNKNOWN, "latest"), headers=bearer(manager))
        assert real.status_code == 404
        assert stable(real) == stable(missing)

        anonymous = await stack.client.get(versions_url(contract, "latest"))
        assert anonymous.status_code == 401
        assert invariant(stack.contracts_db_url) == []


async def test_a_draft_only_contract_has_no_latest_and_no_fetchable_draft_semver(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_odcs_id(), version="0.1.0")
        )
        unknown = await stack.client.get(
            versions_url(UNKNOWN, "latest"), headers=bearer(stack.admin_token)
        )
        assert unknown.status_code == 404
        for ref in ("latest", "0.1.0", "not-a-semver", "1.0", "v0.1.0"):
            response = await stack.client.get(
                versions_url(contract, ref), headers=bearer(stack.admin_token)
            )
            assert response.status_code == 404, (ref, response.text)
            assert stable(response) == stable(unknown), ref
        assert invariant(stack.contracts_db_url) == []


async def test_a_ref_with_build_metadata_resolves_the_same_version(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        body = doc(new_odcs_id(), version="1.2.3-rc.1+build.5")
        contract, draft, etag = await stack.create_contract(chain.team, body)
        assert (await stack.publish(contract, draft, etag)).status_code == 201
        for ref in ("1.2.3-rc.1+build.5", "1.2.3-rc.1+other", "1.2.3-rc.1"):
            response = await stack.client.get(
                versions_url(contract, ref), headers=bearer(stack.admin_token)
            )
            assert response.status_code == 200, (ref, response.text)
            assert response.content == body
        assert invariant(stack.contracts_db_url) == []


# ---- publish refusals (D-39, D-46, D-50, D-51) ----


def json_doc(odcs_id: str, **fields: Any) -> bytes:
    body: dict[str, Any] = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": odcs_id,
        "status": "draft",
        "name": "Orders",
    }
    body.update(fields)
    return json.dumps(body).encode()


# Under the real gate (D-129) governance runs before the SemVer rule. A document whose ``version``
# is not a string, or is missing, breaks the ODCS JSON Schema, so it is refused first with
# 422 contract-validation-failed and a blocking structural error; an empty string and a two-part
# version are schema-valid and still reach the SemVer rule (422 invalid-version).
STRUCTURAL = {
    "json-number": ("/version", "type"),
    "missing": ("", "required"),
    "null": ("/version", "type"),
}


@pytest.mark.parametrize(
    "fields",
    [{"version": 1}, {"version": ""}, {}, {"version": None}, {"version": "1.0"}],
    ids=["json-number", "empty-string", "missing", "null", "two-part"],
)
async def test_a_draft_without_a_strict_semver_string_is_422_invalid_version_and_unpublished(
    make_stack: MakeStack,
    sql: Sql,
    invariant: Invariant,
    fields: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    case = request.node.callspec.id
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(
            chain.team, json_doc(new_odcs_id(), **fields), "application/json"
        )
        count = len(events(stack, sql))
        response = await stack.publish(contract, draft, etag)
        assert response.status_code == 422, response.text
        if case in STRUCTURAL:
            body = response.json()
            assert body["type"] == "urn:dc:problem:contract-validation-failed"
            assert body["report"]["summary"]["publishable"] is False
            errors = body["report"]["structural"]["errors"]
            assert [(e["pointer"], e["keyword"]) for e in errors] == [STRUCTURAL[case]]
        else:
            assert response.json()["type"] == "urn:dc:problem:invalid-version"
        assert version_row(stack, sql, draft)["state"] == "draft"
        # A gate refusal is audited as one value-free publish_refused event (D-175); the local
        # invalid-version refusal is not a gate verdict and records nothing.
        added = [e["action"] for e in events(stack, sql)[count:]]
        assert added == (["contract.version.publish_refused"] if case in STRUCTURAL else [])
        assert invariant(stack.contracts_db_url) == []


async def test_a_document_id_that_no_longer_matches_the_contract_is_422_odcs_id_mismatch(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    import hashlib

    async with make_stack() as stack:
        chain = await stack.chain()
        original_id = new_odcs_id()
        contract, draft, etag = await stack.create_contract(chain.team, doc(original_id))
        other_id = new_odcs_id()
        other = doc(other_id)
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET raw_document = %s, content_sha256 = %s, size_bytes = %s "
            "WHERE id = %s",
            other,
            hashlib.sha256(other).hexdigest(),
            len(other),
            draft,
        )
        count = len(events(stack, sql))
        response = await stack.publish(contract, draft, etag)
        assert response.status_code == 422, response.text
        assert response.json()["type"] == "urn:dc:problem:odcs-id-mismatch"
        assert response.json()["expected_odcs_id"] == original_id
        assert response.json()["found_odcs_id"] == other_id
        assert version_row(stack, sql, draft)["state"] == "draft"
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


async def test_a_published_draft_id_is_read_only_409_never_412(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_odcs_id()
        contract, draft, etag = await stack.create_contract(chain.team, doc(odcs_id))
        assert (await stack.publish(contract, draft, etag)).status_code == 201
        count = len(events(stack, sql))
        before = version_row(stack, sql, draft)

        for header in (etag, '"r9"', "*"):
            again = await stack.publish(contract, draft, header)
            assert again.status_code == 409, (header, again.text)
            body = again.json()
            assert body["type"] == "urn:dc:problem:draft-already-published"
            assert body["version_id"] == draft
            assert header not in again.text or header == etag

        saved = await stack.client.put(
            f"/api/v1/contracts/contracts/{contract}/drafts/{draft}",
            content=doc(odcs_id, version="1.1.0"),
            headers={
                **bearer(stack.admin_token),
                "Content-Type": "application/yaml",
                "If-Match": etag,
            },
        )
        assert saved.status_code == 409, saved.text
        assert saved.json()["type"] == "urn:dc:problem:draft-already-published"
        assert saved.json()["version_id"] == draft

        # No If-Match at all keeps the 428 that every draft write answers first.
        assert (await stack.publish(contract, draft, None)).status_code == 428
        no_header = await stack.client.put(
            f"/api/v1/contracts/contracts/{contract}/drafts/{draft}",
            content=doc(odcs_id),
            headers={**bearer(stack.admin_token), "Content-Type": "application/yaml"},
        )
        assert no_header.status_code == 428

        # Reading the draft routes still treats a published row as not a draft.
        gone = await stack.client.get(
            f"/api/v1/contracts/contracts/{contract}/drafts/{draft}",
            headers=bearer(stack.admin_token),
        )
        assert gone.status_code == 404

        assert version_row(stack, sql, draft) == before
        assert len(events(stack, sql)) == count
        assert invariant(stack.contracts_db_url) == []


# --- Plan 03-04: new drafts from a version, bumped in place (D-45, VER-04, CONT-06) ---

SOURCE_YAML = (
    "# header comment\r\n"
    "apiVersion: v3.2.0\r\nkind: DataContract\r\nid: {odcs_id}\r\n"
    "version: 1.0.0  # current release\r\n"
    "name: Orders über\r\n"
    "schema:\r\n  - name: orders\r\n    properties:\r\n"
    "      - name: id\r\n        logicalType: string\r\n"
    # A nested ``version`` key that a bump must leave alone; ``value`` is free-form in ODCS, so the
    # document stays schema-valid under the real publish gate.
    "customProperties:\r\n  - property: release\r\n    value:\r\n      version: 1.0.0\r\n"
)


def drafts_url(contract: str, draft: str | None = None) -> str:
    base = f"/api/v1/contracts/contracts/{contract}/drafts"
    return base if draft is None else f"{base}/{draft}"


async def new_draft(
    stack: Any, contract: str, body: dict[str, Any], *, token: str | None = None
) -> Any:
    return await stack.client.post(
        drafts_url(contract), json=body, headers=bearer(token or stack.admin_token)
    )


async def draft_bytes(stack: Any, contract: str, draft: str) -> bytes:
    response = await stack.client.get(
        drafts_url(contract, draft) + "/document", headers=bearer(stack.admin_token)
    )
    assert response.status_code == 200, response.text
    return bytes(response.content)


async def published_source(stack: Any, source: bytes) -> tuple[str, str, str]:
    chain = await stack.chain()
    contract, draft, etag = await stack.create_contract(chain.team, source)
    assert (await stack.publish(contract, draft, etag)).status_code == 201
    return chain.team, contract, draft


async def test_an_editor_branches_a_bumped_draft_and_publishes_it_as_a_preview(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        source = SOURCE_YAML.format(odcs_id=new_odcs_id()).encode()
        _team, contract, first = await published_source(stack, source)
        count = len(events(stack, sql))

        created = await new_draft(stack, contract, {"from_version": "1.0.0"})
        assert created.status_code == 201, created.text
        out = created.json()["draft"]
        assert out["declared_version"] == "1.1.0"
        assert out["base_version_id"] == first
        assert out["media_type"] == "application/yaml"
        assert out["revision"] == 1
        assert created.headers["etag"] == '"r1"'
        assert created.headers["location"] == drafts_url(contract, out["id"])

        # Fidelity: the source bytes with the one top-level token replaced.
        expected = source.replace(b"version: 1.0.0  # current", b"version: 1.1.0  # current")
        assert await draft_bytes(stack, contract, out["id"]) == expected
        assert expected.count(b"1.0.0") == 1  # the nested version is still there

        row = version_row(stack, sql, out["id"])
        assert row["version_no"] == 2
        assert str(row["base_version_id"]) == first
        counter = sql(
            stack.contracts_db_url,
            "SELECT version_counter FROM contract WHERE id = %s",
            contract,
        )[0]["version_counter"]
        assert counter == 2
        new = events(stack, sql)[count:]
        assert [event["action"] for event in new] == ["contract.draft_created"]
        assert new[0]["before"] is None
        assert new[0]["after"]["base_version_id"] == first
        assert "schema" not in json.dumps(new[0]["after"])

        published = await stack.publish(contract, out["id"], created.headers["etag"])
        assert published.status_code == 201, published.text
        assert published.json()["status"] == "preview"
        latest = await stack.client.get(
            versions_url(contract, "latest"), headers=bearer(stack.admin_token)
        )
        assert latest.content == source
        preview = await stack.client.get(
            versions_url(contract, "1.1.0"), headers=bearer(stack.admin_token)
        )
        assert preview.content == expected
        assert invariant(stack.contracts_db_url) == []


async def test_major_patch_none_and_a_draft_source_each_copy_as_specified(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        source = SOURCE_YAML.format(odcs_id=new_odcs_id()).encode()
        _team, contract, _first = await published_source(stack, source)

        for bump, declared in (("major", "2.0.0"), ("patch", "1.0.1")):
            created = await new_draft(stack, contract, {"from_version": "1.0.0", "bump": bump})
            assert created.status_code == 201, created.text
            assert created.json()["draft"]["declared_version"] == declared

        same = await new_draft(stack, contract, {"from_version": "1.0.0", "bump": "none"})
        assert same.status_code == 201, same.text
        assert same.json()["draft"]["declared_version"] == "1.0.0"
        assert await draft_bytes(stack, contract, same.json()["draft"]["id"]) == source

        from_draft = await new_draft(
            stack, contract, {"from_draft_id": same.json()["draft"]["id"], "bump": "minor"}
        )
        assert from_draft.status_code == 201, from_draft.text
        assert from_draft.json()["draft"]["declared_version"] == "1.1.0"
        assert from_draft.json()["draft"]["base_version_id"] == same.json()["draft"]["id"]
        assert invariant(stack.contracts_db_url) == []


async def test_a_json_source_is_copied_as_json_with_only_the_version_string_changed(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        odcs_id = new_odcs_id()
        source = (
            '{\n\t"apiVersion": "v3.2.0",\n\t"kind": "DataContract",\n\t"id": "' + odcs_id + '",\n'
            '\t"version": "1.0.0",\n\t"schema": [{"name": "t"}],\n'
            '\t"customProperties": [{"property": "release", "value": {"version": "1.0.0"}}]\n}\n'
        ).encode()
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(chain.team, source, "application/json")
        assert (await stack.publish(contract, draft, etag)).status_code == 201
        created = await new_draft(stack, contract, {"from_version": "1.0.0"})
        assert created.status_code == 201, created.text
        assert created.json()["draft"]["media_type"] == "application/json"
        assert await draft_bytes(stack, contract, created.json()["draft"]["id"]) == source.replace(
            b'"version": "1.0.0",\n\t"schema"', b'"version": "1.1.0",\n\t"schema"'
        )


async def test_a_block_scalar_version_is_422_for_a_bump_and_copies_verbatim_with_none(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        source = (
            f"apiVersion: v3.2.0\nkind: DataContract\nid: {new_odcs_id()}\n"
            "version: |-\n  1.0.0\nname: Orders\n"
        ).encode()
        _team, contract, _first = await published_source(stack, source)
        count = len(events(stack, sql))
        rows = len(sql(stack.contracts_db_url, "SELECT id FROM contract_version"))

        refused = await new_draft(stack, contract, {"from_version": "1.0.0", "bump": "minor"})
        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:copy-field-not-editable"
        assert len(events(stack, sql)) == count
        assert len(sql(stack.contracts_db_url, "SELECT id FROM contract_version")) == rows

        verbatim = await new_draft(stack, contract, {"from_version": "1.0.0", "bump": "none"})
        assert verbatim.status_code == 201, verbatim.text
        assert await draft_bytes(stack, contract, verbatim.json()["draft"]["id"]) == source
        assert invariant(stack.contracts_db_url) == []


async def test_bad_sources_and_bodies_are_404_or_422_and_create_nothing(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        source = SOURCE_YAML.format(odcs_id=new_odcs_id()).encode()
        _team, contract, first = await published_source(stack, source)
        rows = len(sql(stack.contracts_db_url, "SELECT id FROM contract_version"))

        for missing in ({"from_version": "9.9.9"}, {"from_version": "latest"}):
            absent = await new_draft(stack, contract, missing)
            assert absent.status_code == 404, absent.text
        unknown_draft = await new_draft(stack, contract, {"from_draft_id": UNKNOWN})
        assert unknown_draft.status_code == 404
        published_as_draft = await new_draft(stack, contract, {"from_draft_id": first})
        assert published_as_draft.status_code == 404
        for invalid in (
            {},
            {"from_version": "1.0.0", "from_draft_id": first},
            {"from_version": "1.0.0", "bump": "huge"},
            {"from_version": "1.0.0", "extra": 1},
            {"from_version": "x" * 301},
        ):
            response = await new_draft(stack, contract, invalid)
            assert response.status_code == 422, (invalid, response.text)
            assert response.json()["type"] == "urn:dc:problem:validation-error"
        assert len(sql(stack.contracts_db_url, "SELECT id FROM contract_version")) == rows


async def test_consumers_get_the_unknown_404_and_a_team_reader_is_403_on_create_draft(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(
            chain.team, SOURCE_YAML.format(odcs_id=new_odcs_id()).encode()
        )
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        consumer, consumer_principal = stack.token("cs")
        stack.grant_sql(consumer_principal, "consumer", chain.team)
        count = len(events(stack, sql))

        denied = await new_draft(stack, contract, {"from_draft_id": draft}, token=reader)
        assert denied.status_code == 403, denied.text
        real = await new_draft(stack, contract, {"from_draft_id": draft}, token=consumer)
        missing = await new_draft(stack, UNKNOWN, {"from_draft_id": draft}, token=consumer)
        assert real.status_code == 404, real.text
        assert stable(real) == stable(missing)
        assert len(events(stack, sql)) == count
        assert etag == '"r1"'


# --- Plan 03-04 Task 2: parallel drafts, first publish wins, concurrency (D-46, VER-04) ---


async def set_version(
    stack: Any, contract: str, draft: str, odcs_id: str, version: str, etag: str
) -> str:
    """Rewrite the draft so it declares ``version`` (PUT under If-Match); returns the new ETag."""
    saved = await stack.client.put(
        drafts_url(contract, draft),
        content=doc(odcs_id, version=version),
        headers={**bearer(stack.admin_token), "Content-Type": "application/yaml", "If-Match": etag},
    )
    assert saved.status_code == 200, saved.text
    return str(saved.headers["etag"])


async def contract_with_latest(stack: Any) -> tuple[str, str, str]:
    """A contract whose 1.0.0 is published; returns (contract id, odcs id, first version id)."""
    odcs_id = new_odcs_id()
    chain = await stack.chain()
    contract, draft, etag = await stack.create_contract(chain.team, doc(odcs_id))
    assert (await stack.publish(contract, draft, etag)).status_code == 201
    return contract, odcs_id, draft


async def test_first_publish_wins_and_the_second_draft_with_the_same_semver_is_409(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        contract, _odcs, _first = await contract_with_latest(stack)
        a = (await new_draft(stack, contract, {"from_version": "1.0.0"})).json()["draft"]
        b = (await new_draft(stack, contract, {"from_version": "1.0.0"})).json()["draft"]
        assert a["declared_version"] == b["declared_version"] == "1.1.0"

        won = await stack.publish(contract, a["id"], a["etag"])
        assert won.status_code == 201, won.text
        assert won.json()["status"] == "preview"
        count = len(events(stack, sql))
        lost = await stack.publish(contract, b["id"], b["etag"])
        assert lost.status_code == 409, lost.text
        assert lost.json()["type"] == "urn:dc:problem:semver-conflict"
        assert lost.json()["existing_semver"] == "1.1.0"
        assert lost.json()["highest_semver"] == "1.1.0"
        assert version_row(stack, sql, b["id"])["state"] == "draft"
        assert len(events(stack, sql)) == count
        listed = await stack.client.get(drafts_url(contract), headers=bearer(stack.admin_token))
        assert b["id"] in [item["id"] for item in listed.json()["items"]]
        assert invariant(stack.contracts_db_url) == []


async def test_first_publish_wins_across_build_metadata_prerelease_and_lower_versions(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        contract, odcs_id, _first = await contract_with_latest(stack)

        async def draft_declaring(version: str) -> tuple[str, str]:
            made = (
                await new_draft(stack, contract, {"from_version": "1.0.0", "bump": "none"})
            ).json()["draft"]
            return made["id"], await set_version(
                stack, contract, made["id"], odcs_id, version, made["etag"]
            )

        a, a_etag = await draft_declaring("1.1.0+a")
        assert (await stack.publish(contract, a, a_etag)).status_code == 201
        b, b_etag = await draft_declaring("1.1.0+b")
        same = await stack.publish(contract, b, b_etag)
        assert same.status_code == 409, same.text
        assert same.json()["type"] == "urn:dc:problem:semver-conflict"

        rc, rc_etag = await draft_declaring("1.1.0-rc.1")
        lower_pre = await stack.publish(contract, rc, rc_etag)
        assert lower_pre.status_code == 409, lower_pre.text
        assert lower_pre.json()["highest_semver"] == "1.1.0+a"

        low, low_etag = await draft_declaring("0.9.0")
        lower = await stack.publish(contract, low, low_etag)
        assert lower.status_code == 409, lower.text
        assert invariant(stack.contracts_db_url) == []


async def test_several_previews_coexist_while_latest_stays_put(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        contract, odcs_id, first = await contract_with_latest(stack)
        minor = (await new_draft(stack, contract, {"from_version": "1.0.0"})).json()["draft"]
        major = (
            await new_draft(stack, contract, {"from_version": "1.0.0", "bump": "major"})
        ).json()["draft"]
        spare = (
            await new_draft(stack, contract, {"from_version": "1.0.0", "bump": "patch"})
        ).json()["draft"]
        for made, semver in ((minor, "1.1.0"), (major, "2.0.0")):
            published = await stack.publish(contract, made["id"], made["etag"])
            assert published.status_code == 201, published.text
            assert published.json()["status"] == "preview"
            assert published.json()["semver"] == semver

        latest = await stack.client.get(
            versions_url(contract, "latest"), headers=bearer(stack.admin_token)
        )
        assert latest.status_code == 200
        assert b'version: "1.0.0"' in latest.content
        for semver in ("1.1.0", "2.0.0"):
            fetched = await stack.client.get(
                versions_url(contract, semver), headers=bearer(stack.admin_token)
            )
            assert fetched.status_code == 200, fetched.text
        listed = await stack.client.get(drafts_url(contract), headers=bearer(stack.admin_token))
        items = listed.json()["items"]
        assert [(item["id"], item["declared_version"]) for item in items] == [
            (spare["id"], "1.0.1")
        ]
        pointer = (
            await stack.client.get(
                versions_url(contract, "1.0.0"), headers=bearer(stack.admin_token)
            )
        ).status_code
        assert pointer == 200
        assert first not in [item["id"] for item in items]
        assert odcs_id
        assert invariant(stack.contracts_db_url) == []


async def test_concurrency_six_create_drafts_get_distinct_row_numbers(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        contract, _odcs, _first = await contract_with_latest(stack)
        responses = await asyncio.gather(
            *(new_draft(stack, contract, {"from_version": "1.0.0"}) for _ in range(6))
        )
        assert [response.status_code for response in responses] == [201] * 6
        numbers = sorted(
            row["version_no"]
            for row in sql(
                stack.contracts_db_url,
                "SELECT version_no FROM contract_version WHERE contract_id = %s",
                contract,
            )
        )
        assert numbers == list(range(1, 8))
        counter = sql(
            stack.contracts_db_url, "SELECT version_counter FROM contract WHERE id = %s", contract
        )[0]["version_counter"]
        assert counter == 7
        assert invariant(stack.contracts_db_url) == []


async def test_concurrency_four_publishes_of_the_same_semver_have_exactly_one_winner(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        contract, _odcs, _first = await contract_with_latest(stack)
        drafts = [
            (await new_draft(stack, contract, {"from_version": "1.0.0"})).json()["draft"]
            for _ in range(4)
        ]
        responses = await asyncio.gather(
            *(stack.publish(contract, made["id"], made["etag"]) for made in drafts)
        )
        codes = sorted(response.status_code for response in responses)
        assert codes == [201, 409, 409, 409], [response.text for response in responses]
        for response in responses:
            if response.status_code == 409:
                assert response.json()["type"] == "urn:dc:problem:semver-conflict"
        assert invariant(stack.contracts_db_url) == []
