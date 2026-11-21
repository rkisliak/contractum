"""How a draft starts: the generated ODCS starter (D-43) and a copy of another contract (D-44b).

Tracer: an editor starts a contract from the ODCS starter, edits nothing and publishes 0.1.0; the
starter is a server-built dictionary serialised with ``yaml.safe_dump`` so caller text cannot add
or override a key.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg
import pytest
from dc_odcs import load_yaml

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"
UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
STARTER_KEYS = ["apiVersion", "kind", "id", "name", "version", "status"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_odcs_id() -> str:
    return f"urn:dc:ref:{uuid.uuid4().hex}"


def starter_url(folder: str) -> str:
    return f"/api/v1/contracts/folders/{folder}/contracts/starter"


async def starter(stack: Any, folder: str, token: str, payload: dict[str, Any]) -> Any:
    return await stack.client.post(starter_url(folder), json=payload, headers=bearer(token))


async def draft_document(stack: Any, token: str, contract: str, draft: str) -> Any:
    return await stack.client.get(
        f"{CONTRACTS}/{contract}/drafts/{draft}/document", headers=bearer(token)
    )


async def editor_at(stack: Any, folder: str, sub: str = "editor") -> str:
    token, principal = stack.token(sub)
    stack.grant_sql(principal, "team_editor", folder)
    return token


def assert_problem(response: Any, status: int, slug: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    return body


def counts(stack: Any, sql: Sql) -> tuple[int, int]:
    url = stack.contracts_db_url
    return (
        sql(url, "SELECT count(*) AS n FROM contract")[0]["n"],
        sql(url, "SELECT count(*) AS n FROM outbox")[0]["n"],
    )


async def test_an_editor_starts_a_contract_from_the_starter_and_publishes_0_1_0(
    make_stack: MakeStack, sql: Sql, invariant: Callable[[str], list[dict[str, Any]]]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor_at(stack, chain.team)
        odcs_id = new_odcs_id()

        created = await starter(stack, chain.team, ed, {"odcs_id": odcs_id, "name": "Orders"})
        assert created.status_code == 201, created.text
        body = created.json()
        contract = body["contract"]["id"]
        draft = body["draft"]
        assert created.headers["location"] == f"{CONTRACTS}/{contract}"
        assert draft["etag"] == '"r1"'
        assert body["contract"]["odcs_id"] == odcs_id
        assert body["contract"]["media_type"] == "application/yaml"
        assert body["contract"]["folder_id"] == chain.team
        assert "structural_validation" in body

        document = await draft_document(stack, ed, contract, draft["id"])
        assert document.status_code == 200
        loaded = load_yaml(document.content.decode())
        assert loaded == {
            "apiVersion": "v3.2.0",
            "kind": "DataContract",
            "id": odcs_id,
            "name": "Orders",
            "version": "0.1.0",
            "status": "draft",
        }
        assert list(loaded) == STARTER_KEYS
        assert isinstance(loaded["version"], str)

        published = await stack.publish(contract, draft["id"], draft["etag"], token=ed)
        assert published.status_code == 201, published.text
        assert published.json()["semver"] == "0.1.0"
        assert published.json()["status"] == "latest"
        assert invariant(stack.contracts_db_url) == []


async def test_a_name_carrying_yaml_syntax_stays_one_string_value(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor_at(stack, chain.team)
        # A newline is refused by the name rule (quick 261120-lbz, D-01): YAML syntax that the
        # rule allows (colon, hash) still has to stay one string value.
        refused = await starter(
            stack, chain.team, ed, {"odcs_id": new_odcs_id(), "name": "Orders\nversion: 9.9.9"}
        )
        assert refused.status_code == 422
        name = "Orders version: 9.9.9 # x: y"
        created = await starter(stack, chain.team, ed, {"odcs_id": new_odcs_id(), "name": name})
        assert created.status_code == 201, created.text
        body = created.json()
        document = await draft_document(stack, ed, body["contract"]["id"], body["draft"]["id"])
        loaded = load_yaml(document.content.decode())
        assert loaded["name"] == name
        assert loaded["version"] == "0.1.0"
        assert list(loaded) == STARTER_KEYS


async def test_the_starter_is_refused_for_bad_bodies_folders_and_callers(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor_at(stack, chain.team)
        odcs_id = new_odcs_id()
        assert (
            await starter(stack, chain.team, ed, {"odcs_id": odcs_id, "name": "Orders"})
        ).status_code == 201
        before = counts(stack, sql)

        # a taken id in the Organization
        duplicate = await starter(stack, chain.team, ed, {"odcs_id": odcs_id, "name": "Again"})
        assert_problem(duplicate, 409, "duplicate-contract-id")

        # invalid bodies
        for payload in (
            {"odcs_id": new_odcs_id(), "name": ""},
            {"odcs_id": new_odcs_id(), "name": "bad\u0000name"},
            {"odcs_id": "", "name": "Orders"},
            {"odcs_id": new_odcs_id(), "name": "x" * 201},
            {"odcs_id": new_odcs_id(), "name": "Orders", "notation": "avro"},
            {"odcs_id": new_odcs_id(), "name": "Orders", "folder_id": chain.org},
            {"name": "Orders"},
        ):
            response = await starter(stack, chain.team, ed, payload)
            assert response.status_code == 422, (payload, response.text)

        # notation defaults to odcs and may be named
        named = await starter(
            stack, chain.team, ed, {"notation": "odcs", "odcs_id": new_odcs_id(), "name": "N"}
        )
        assert named.status_code == 201, named.text
        before = (before[0] + 1, before[1] + 1)

        # a folder of any kind holds contracts
        project = await starter(
            stack, chain.project, stack.admin_token, {"odcs_id": new_odcs_id(), "name": "P"}
        )
        assert project.status_code == 201, project.text
        before = (before[0] + 1, before[1] + 1)

        # a reader of the folder may not create; a caller without access sees nothing
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        denied = await starter(stack, chain.team, reader, {"odcs_id": new_odcs_id(), "name": "R"})
        assert denied.status_code == 403, denied.text
        stranger, _ = stack.token("stranger")
        hidden = await starter(stack, chain.team, stranger, {"odcs_id": new_odcs_id(), "name": "S"})
        assert_problem(hidden, 404, "not-found")
        unknown = await starter(stack, UNKNOWN, ed, {"odcs_id": new_odcs_id(), "name": "U"})
        assert_problem(unknown, 404, "not-found")
        assert counts(stack, sql) == before

        # an archived subtree is read-only
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.project}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code == 200, archived.text
        frozen = await starter(stack, chain.team, ed, {"odcs_id": new_odcs_id(), "name": "A"})
        assert_problem(frozen, 409, "folder-archived")
        assert counts(stack, sql) == before


async def test_the_starter_records_one_created_event_without_document_content(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor_at(stack, chain.team)
        odcs_id = new_odcs_id()
        created = await starter(stack, chain.team, ed, {"odcs_id": odcs_id, "name": "Orders"})
        assert created.status_code == 201, created.text
        events = sql(stack.contracts_db_url, "SELECT action, after FROM outbox")
        assert [event["action"] for event in events] == ["contract.created"]
        after = events[0]["after"]
        assert after["odcs_id"] == odcs_id
        assert after["state"] == "draft"
        assert "raw_document" not in after
        assert "document" not in after


# --- create from a version or draft of another contract (D-44b) -------------------------------

SOURCE_ID = "urn:dc:s"
JSON_SOURCE = (
    b'{\n  "apiVersion": "v3.2.0",\n  "kind": "DataContract",\n  "id"  :  "urn:dc:j",\n'
    b'  "version": "1.0.0",\n  "name": "J"\n}\n'
)
BLOCK_SOURCE = (
    b'apiVersion: v3.2.0\nkind: DataContract\nid: |-\n  urn:dc:blk\nversion: "1.0.0"\nname: Blk\n'
)


def yaml_doc(version: str, odcs_id: str = SOURCE_ID) -> bytes:
    return (
        "# orders contract, owned by the sales team\n"
        f"apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}  # stable id\n"
        f"version: {version}  # release\nstatus: draft\nname: Orders\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def from_version_url(folder: str) -> str:
    return f"/api/v1/contracts/folders/{folder}/contracts/from-version"


async def copy_from(stack: Any, folder: str, token: str, payload: dict[str, Any]) -> Any:
    return await stack.client.post(from_version_url(folder), json=payload, headers=bearer(token))


async def new_draft(
    stack: Any, contract: str, document: bytes, *, from_version: str
) -> tuple[str, str]:
    """An open draft of ``contract`` holding ``document``; returns (draft id, its ETag)."""
    created = await stack.client.post(
        f"{CONTRACTS}/{contract}/drafts",
        json={"from_version": from_version, "bump": "none"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        f"{CONTRACTS}/{contract}/drafts/{draft['id']}",
        content=document,
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": draft["etag"],
        },
    )
    assert saved.status_code == 200, saved.text
    return str(draft["id"]), str(saved.headers["etag"])


async def publish_next(stack: Any, contract: str, version: str, *, from_version: str) -> str:
    """Publish ``version`` as a new version; returns its version id."""
    draft, etag = await new_draft(stack, contract, yaml_doc(version), from_version=from_version)
    published = await stack.publish(contract, draft, etag)
    assert published.status_code == 201, published.text
    return str(published.json()["version_id"])


async def source_contract(stack: Any, team: str) -> dict[str, Any]:
    """S: 1.0.0 (Latest) and 1.1.0 published, plus an open draft declaring 1.2.0."""
    contract, draft, etag = await stack.create_contract(team, yaml_doc("1.0.0"))
    first = await stack.publish(contract, draft, etag)
    assert first.status_code == 201, first.text
    v110 = await publish_next(stack, contract, "1.1.0", from_version="1.0.0")
    open_draft, open_etag = await new_draft(
        stack, contract, yaml_doc("1.2.0"), from_version="1.1.0"
    )
    return {
        "contract": contract,
        "v100": str(first.json()["version_id"]),
        "v110": v110,
        "draft": open_draft,
        "draft_etag": open_etag,
    }


async def fetch_bytes(stack: Any, contract: str, ref: str) -> bytes:
    response = await stack.client.get(
        f"{CONTRACTS}/{contract}/versions/{ref}", headers=bearer(stack.admin_token)
    )
    assert response.status_code == 200, response.text
    return bytes(response.content)


def created_events(stack: Any, sql: Sql) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT after FROM outbox WHERE action = 'contract.created' ORDER BY id",
    )


def stable(response: Any) -> tuple[int, str | None, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, response.headers.get("content-type"), sorted(body.items())


async def test_copy_of_a_published_version_keeps_the_bytes_and_replaces_only_the_id_token(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        source = await source_contract(stack, chain.team)
        ed, principal = stack.token("ed")
        stack.grant_sql(principal, "team_editor", t2)
        stack.grant_sql(principal, "consumer", chain.team)

        created = await copy_from(
            stack,
            t2,
            ed,
            {
                "source_contract_id": source["contract"],
                "source_version": "1.1.0",
                "odcs_id": "urn:dc:copy",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        new = body["contract"]["id"]
        assert created.headers["location"] == f"{CONTRACTS}/{new}"
        assert body["contract"]["odcs_id"] == "urn:dc:copy"
        assert body["contract"]["folder_id"] == t2
        assert body["contract"]["template"] is None
        assert body["draft"]["etag"] == '"r1"'

        source_bytes = await fetch_bytes(stack, source["contract"], "1.1.0")
        assert b"id: urn:dc:s  # stable id" in source_bytes
        document = await draft_document(stack, ed, new, body["draft"]["id"])
        assert document.status_code == 200
        expected = source_bytes.replace(
            b"id: urn:dc:s  # stable id", b'id: "urn:dc:copy"  # stable id'
        )
        assert document.content == expected
        assert b"version: 1.1.0  # release" in document.content
        assert document.headers["content-type"] == "application/yaml"

        metadata = await stack.client.get(f"{CONTRACTS}/{new}", headers=bearer(ed))
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["origin"] == {
            "contract_id": source["contract"],
            "version_id": source["v110"],
        }
        assert metadata.json()["template"] is None
        # the source is untouched
        assert await fetch_bytes(stack, source["contract"], "1.1.0") == source_bytes

        events = created_events(stack, sql)
        after = events[-1]["after"]
        assert after["origin_contract_id"] == source["contract"]
        assert after["origin_version_id"] == source["v110"]
        assert after["origin_version_state"] == "published"
        assert "raw_document" not in after and "document" not in after
        assert len(events) == 2  # the source contract's create and the copy's


async def test_a_json_source_copies_with_only_its_id_string_replaced(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        contract, draft, etag = await stack.create_contract(
            chain.team, JSON_SOURCE, "application/json"
        )
        assert (await stack.publish(contract, draft, etag)).status_code == 201

        created = await copy_from(
            stack,
            t2,
            stack.admin_token,
            {"source_contract_id": contract, "source_version": "1.0.0", "odcs_id": "urn:dc:jc"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["contract"]["media_type"] == "application/json"
        document = await draft_document(
            stack, stack.admin_token, body["contract"]["id"], body["draft"]["id"]
        )
        assert document.content == JSON_SOURCE.replace(b'"urn:dc:j"', b'"urn:dc:jc"')
        assert json.loads(document.content)["version"] == "1.0.0"


async def test_a_source_whose_id_is_not_a_single_line_scalar_is_refused_and_nothing_is_created(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        contract, draft, etag = await stack.create_contract(chain.team, BLOCK_SOURCE)
        assert (await stack.publish(contract, draft, etag)).status_code == 201
        before = counts(stack, sql)

        refused = await copy_from(
            stack,
            t2,
            stack.admin_token,
            {"source_contract_id": contract, "source_version": "1.0.0", "odcs_id": "urn:dc:blkc"},
        )
        assert_problem(refused, 422, "copy-field-not-editable")
        assert counts(stack, sql) == before


async def test_a_draft_source_needs_contract_read_and_the_copy_keeps_the_draft_bytes(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        source = await source_contract(stack, chain.team)
        payload = {
            "source_contract_id": source["contract"],
            "source_draft_id": source["draft"],
            "odcs_id": "urn:dc:copy-d",
        }

        # a Consumer cannot read drafts: the same 404 as an unknown contract
        consumer, c_principal = stack.token("consumer")
        stack.grant_sql(c_principal, "team_editor", t2)
        stack.grant_sql(c_principal, "consumer", chain.team)
        refused = await copy_from(stack, t2, consumer, payload)
        unknown = await copy_from(stack, t2, consumer, {**payload, "source_contract_id": UNKNOWN})
        assert refused.status_code == 404
        assert stable(refused) == stable(unknown)

        # a Team reader of the source folder may read the draft
        reader, r_principal = stack.token("reader")
        stack.grant_sql(r_principal, "team_editor", t2)
        stack.grant_sql(r_principal, "team_reader", chain.team)
        created = await copy_from(stack, t2, reader, payload)
        assert created.status_code == 201, created.text
        body = created.json()
        new = body["contract"]["id"]
        draft_bytes = (
            await draft_document(stack, stack.admin_token, source["contract"], source["draft"])
        ).content
        assert b"version: 1.2.0  # release" in draft_bytes
        document = await draft_document(stack, reader, new, body["draft"]["id"])
        assert document.content == draft_bytes.replace(
            b"id: urn:dc:s  # stable id", b'id: "urn:dc:copy-d"  # stable id'
        )
        metadata = await stack.client.get(f"{CONTRACTS}/{new}", headers=bearer(reader))
        assert metadata.json()["origin"] == {
            "contract_id": source["contract"],
            "version_id": source["draft"],
        }
        after = created_events(stack, sql)[-1]["after"]
        assert after["origin_version_state"] == "draft"
        assert after["origin_version_id"] == source["draft"]


async def test_the_origin_survives_publishing_or_discarding_the_source_draft(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        source = await source_contract(stack, chain.team)
        admin = stack.admin_token
        first = await copy_from(
            stack,
            t2,
            admin,
            {
                "source_contract_id": source["contract"],
                "source_draft_id": source["draft"],
                "odcs_id": "urn:dc:c1",
            },
        )
        assert first.status_code == 201, first.text

        # publishing the source draft keeps its id, so the origin now names a published version
        published = await stack.publish(source["contract"], source["draft"], source["draft_etag"])
        assert published.status_code == 201, published.text
        assert published.json()["version_id"] == source["draft"]
        metadata = await stack.client.get(
            f"{CONTRACTS}/{first.json()['contract']['id']}", headers=bearer(admin)
        )
        assert metadata.json()["origin"]["version_id"] == source["draft"]

        # a second draft is copied, then discarded
        second_draft, second_etag = await new_draft(
            stack, source["contract"], yaml_doc("1.3.0"), from_version="1.2.0"
        )
        second = await copy_from(
            stack,
            t2,
            admin,
            {
                "source_contract_id": source["contract"],
                "source_draft_id": second_draft,
                "odcs_id": "urn:dc:c2",
            },
        )
        assert second.status_code == 201, second.text
        discarded = await stack.client.delete(
            f"{CONTRACTS}/{source['contract']}/drafts/{second_draft}",
            headers={**bearer(admin), "If-Match": second_etag},
        )
        assert discarded.status_code == 204, discarded.text
        metadata = await stack.client.get(
            f"{CONTRACTS}/{second.json()['contract']['id']}", headers=bearer(admin)
        )
        assert metadata.json()["origin"] == {
            "contract_id": source["contract"],
            "version_id": second_draft,
        }


async def test_bad_references_and_bodies_are_one_404_or_a_422(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        source = await source_contract(stack, chain.team)
        other, other_draft, _ = await stack.create_contract(
            chain.team, yaml_doc("1.0.0", "urn:dc:other")
        )
        admin = stack.admin_token
        before = counts(stack, sql)
        base = {"source_contract_id": source["contract"], "odcs_id": "urn:dc:never"}

        not_found = [
            {**base, "source_draft_id": other_draft},  # a draft of another contract
            {**base, "source_draft_id": source["v110"]},  # a published version id
            {**base, "source_draft_id": UNKNOWN},
            {**base, "source_version": "1.2.0"},  # declared only by a draft
            {**base, "source_version": "9.9.9"},
            {**base, "source_version": "not-a-semver"},
            {**base, "source_version": "1.1.0", "source_contract_id": UNKNOWN},
        ]
        bodies = []
        for payload in not_found:
            response = await copy_from(stack, t2, admin, payload)
            assert_problem(response, 404, "not-found")
            bodies.append(stable(response))
        assert len({str(item) for item in bodies}) == 1

        for payload in (
            {**base},  # neither
            {**base, "source_version": "1.1.0", "source_draft_id": source["draft"]},  # both
            {**base, "source_version": "1.1.0", "folder_id": chain.org},  # extra field
            {**base, "source_version": "1.1.0", "odcs_id": ""},
            {**base, "source_version": "1.1.0", "odcs_id": "bad\u0000id"},
            {**base, "source_version": ""},
        ):
            response = await copy_from(stack, t2, admin, payload)
            assert response.status_code == 422, (payload, response.text)
        assert counts(stack, sql) == before


async def test_a_source_the_caller_cannot_read_is_the_unknown_contract_404(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        source = await source_contract(stack, chain.team)
        in_other_org = await source_contract(stack, other.team)
        ed, principal = stack.token("ed")
        stack.grant_sql(principal, "team_editor", t2)  # no access at all to the source folders
        payload = {
            "source_contract_id": source["contract"],
            "source_version": "1.1.0",
            "odcs_id": "urn:dc:nope",
        }
        refused = await copy_from(stack, t2, ed, payload)
        unknown = await copy_from(stack, t2, ed, {**payload, "source_contract_id": UNKNOWN})
        assert refused.status_code == 404
        assert stable(refused) == stable(unknown)
        foreign = await copy_from(
            stack, t2, ed, {**payload, "source_contract_id": in_other_org["contract"]}
        )
        assert stable(foreign) == stable(unknown)
        assert counts(stack, sql)[0] == 2  # the two sources and nothing else


async def test_an_unpublished_version_needs_contract_publish_at_the_source(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        source = await source_contract(stack, chain.team)
        unpublished = await stack.client.post(
            f"{CONTRACTS}/{source['contract']}/versions/1.1.0/unpublish",
            headers=bearer(stack.admin_token),
        )
        assert unpublished.status_code == 200, unpublished.text
        payload = {
            "source_contract_id": source["contract"],
            "source_version": "1.1.0",
            "odcs_id": "urn:dc:from-unpublished",
        }

        consumer, c_principal = stack.token("consumer")
        stack.grant_sql(c_principal, "team_editor", t2)
        stack.grant_sql(c_principal, "consumer", chain.team)
        refused = await copy_from(stack, t2, consumer, payload)
        unknown = await copy_from(stack, t2, consumer, {**payload, "source_contract_id": UNKNOWN})
        assert refused.status_code == 404
        assert stable(refused) == stable(unknown)

        owner, o_principal = stack.token("owner")
        stack.grant_sql(o_principal, "team_editor", t2)
        stack.grant_sql(o_principal, "team_editor", chain.team)
        created = await copy_from(stack, t2, owner, payload)
        assert created.status_code == 201, created.text
        assert created_events(stack, sql)[-1]["after"]["origin_version_state"] == "unpublished"


async def test_the_copy_may_not_take_the_source_odcs_id_and_needs_create_at_the_target(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        source = await source_contract(stack, chain.team)
        payload = {
            "source_contract_id": source["contract"],
            "source_version": "1.1.0",
            "odcs_id": SOURCE_ID,
        }
        before = counts(stack, sql)
        assert_problem(
            await copy_from(stack, t2, stack.admin_token, payload), 409, "duplicate-contract-id"
        )
        assert counts(stack, sql) == before

        ok = {**payload, "odcs_id": "urn:dc:fine"}
        reader, r_principal = stack.token("reader")
        stack.grant_sql(r_principal, "team_reader", t2)
        stack.grant_sql(r_principal, "team_reader", chain.team)
        assert (await copy_from(stack, t2, reader, ok)).status_code == 403
        stranger, _ = stack.token("stranger")
        assert_problem(await copy_from(stack, t2, stranger, ok), 404, "not-found")
        into_project = await copy_from(
            stack, chain.project, stack.admin_token, {**ok, "odcs_id": "urn:dc:fine-project"}
        )
        assert into_project.status_code == 201, into_project.text
        before = counts(stack, sql)
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.project}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code == 200, archived.text
        assert_problem(
            await copy_from(stack, chain.team, stack.admin_token, ok), 409, "folder-archived"
        )
        assert counts(stack, sql) == before


async def test_the_database_keeps_the_origin_pair_and_the_origin_contract(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        t2 = await stack.team_in_org(chain.org, "t2")
        source = await source_contract(stack, chain.team)
        created = await copy_from(
            stack,
            t2,
            stack.admin_token,
            {
                "source_contract_id": source["contract"],
                "source_version": "1.0.0",
                "odcs_id": "urn:dc:copy-db",
            },
        )
        assert created.status_code == 201, created.text
        row = sql(
            stack.contracts_db_url,
            "SELECT origin_contract_id::text AS c, origin_version_id::text AS v "
            "FROM contract WHERE id = %s",
            created.json()["contract"]["id"],
        )[0]
        assert (row["c"], row["v"]) == (source["contract"], source["v100"])

        def insert(odcs_id: str, contract: str | None, version: str | None) -> None:
            sql(
                stack.contracts_db_url,
                "INSERT INTO contract (folder_id, ancestor_ids, organization_id, odcs_id, "
                "origin_contract_id, origin_version_id, created_by, updated_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'system:test', 'system:test')",
                t2,
                [chain.org, t2],
                chain.org,
                odcs_id,
                contract,
                version,
            )

        with pytest.raises(psycopg.errors.CheckViolation):
            insert("urn:dc:only-contract", source["contract"], None)
        with pytest.raises(psycopg.errors.CheckViolation):
            insert("urn:dc:only-version", None, str(uuid.uuid4()))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            insert("urn:dc:ghost-contract", str(uuid.uuid4()), str(uuid.uuid4()))
        # a version id is provenance only: it is not a foreign key
        insert("urn:dc:ghost-version", source["contract"], str(uuid.uuid4()))


async def test_the_odcs_id_bound_is_255_on_the_starter_and_from_version(
    make_stack: MakeStack,
) -> None:
    """Quick 261120-lbz, D-02: the API models carry the ingest gate's bound, not 1000."""
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor_at(stack, chain.team)
        too_long = "x" * 256

        refused = await starter(stack, chain.team, ed, {"odcs_id": too_long, "name": "Orders"})
        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:validation-error"
        assert refused.json()["errors"][0]["pointer"] == "/body/odcs_id"

        first = await starter(stack, chain.team, ed, {"odcs_id": "y" * 255, "name": "Orders"})
        assert first.status_code == 201, first.text
        body = first.json()

        source = {
            "source_contract_id": body["contract"]["id"],
            "source_draft_id": body["draft"]["id"],
        }
        refused = await copy_from(stack, chain.team, ed, {**source, "odcs_id": too_long})
        assert refused.status_code == 422, refused.text
        assert refused.json()["errors"][0]["pointer"] == "/body/odcs_id"
        copied = await copy_from(stack, chain.team, ed, {**source, "odcs_id": "z" * 255})
        assert copied.status_code == 201, copied.text
