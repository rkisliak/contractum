"""Create rejection matrix, folder rules, fail-closed authorization, tenant-scoped uniqueness,
concurrency and the one-event-per-create adjacency (CONT-01, AUD-02, PLAT-04)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx
import pytest
from dc_auth.testing import FakeIssuer

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
MAX_BYTES = 1048576
StackFactory = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str | None = None, api_version: str = "v3.2.0", extra: str = "") -> bytes:
    text = (FIXTURES / "valid-v3.2.0.yaml").read_text(encoding="utf-8")
    if odcs_id is not None:
        text = text.replace("urn:datacontract:payments:orders", odcs_id)
    text = text.replace("apiVersion: v3.2.0", f"apiVersion: {api_version}")
    return (text + extra).encode("utf-8")


def unique_id() -> str:
    return f"urn:datacontract:matrix:{uuid.uuid4()}"


def assert_problem(response: httpx.Response, status: int, slug: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    assert body["status"] == status
    return body


def stable(body: dict[str, Any]) -> dict[str, Any]:
    """The parts of a problem that must not depend on whether the object exists."""
    return {k: body.get(k) for k in ("type", "title", "status", "detail")}


def counts(sql: Callable[..., Any], url: str) -> dict[str, int]:
    return {
        "contract": sql(url, "SELECT count(*) AS n FROM contract")[0]["n"],
        "contract_version": sql(url, "SELECT count(*) AS n FROM contract_version")[0]["n"],
        "outbox": sql(url, "SELECT count(*) AS n FROM outbox")[0]["n"],
    }


async def test_unsupported_odcs_versions_are_a_422_naming_the_supported_ones(
    make_stack: StackFactory, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        for version in ("v3.0.2", "v2.2.2", "v3.1.1"):
            response = await stack.post(chain.team, doc(unique_id(), api_version=version))
            body = assert_problem(response, 422, "unsupported-odcs-version")
            assert "v3.1.0" in body["detail"]
            assert "v3.2.0" in body["detail"]
            assert body["errors"][0]["pointer"] == "/apiVersion"
        assert counts(sql, stack.contracts_db_url) == {
            "contract": 0,
            "contract_version": 0,
            "outbox": 0,
        }


async def test_supported_versions_setting_narrows_the_allow_list(make_stack: StackFactory) -> None:
    async with make_stack(supported_odcs_versions=frozenset({"v3.2.0"})) as stack:
        chain = await stack.chain()
        legacy = (FIXTURES / "valid-v3.1.0.json").read_bytes()
        response = await stack.post(chain.team, legacy, "application/json")
        body = assert_problem(response, 422, "unsupported-odcs-version")
        assert "v3.2.0" in body["detail"]
        assert (await stack.post(chain.team, doc(unique_id()))).status_code == 201


async def test_malformed_and_wrongly_typed_requests_are_clear_4xx(
    make_stack: StackFactory, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        plain = await stack.post(chain.team, doc(unique_id()), "text/plain")
        assert_problem(plain, 415, "unsupported-media-type")

        empty = await stack.post(chain.team, b"", "application/yaml")
        assert_problem(empty, 422, "malformed-document")

        alias = await stack.post(
            chain.team, doc(unique_id(), extra="anchored: &a [1, 2]\nreused: *a\n")
        )
        assert_problem(alias, 422, "malformed-document")

        nul = await stack.post(
            chain.team,
            b'{"apiVersion": "v3.2.0", "kind": "DataContract", "id": "x", "name": "a\\u0000b"}',
            "application/json",
        )
        assert_problem(nul, 422, "malformed-document")

        # DEF-01: a lone UTF-16 surrogate escape is a 422, never a 500.
        surrogate = await stack.post(
            chain.team,
            b'{"apiVersion": "v3.2.0", "kind": "DataContract", "id": "x", "name": "a\\ud800b"}',
            "application/json",
        )
        assert_problem(surrogate, 422, "malformed-document")

        wrong_kind = await stack.post(
            chain.team, doc(unique_id()).replace(b"DataContract", b"Nope")
        )
        body = assert_problem(wrong_kind, 422, "validation-error")
        assert body["errors"][0]["pointer"] == "/kind"
        assert counts(sql, stack.contracts_db_url)["contract"] == 0


async def test_size_cap_boundary_and_streaming_413_are_problem_json(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        base = doc(unique_id())

        def padded(total: int) -> bytes:
            pad = total - len(base)
            return base + b"#" + b"x" * (pad - 2) + b"\n"

        at_limit = padded(MAX_BYTES)
        assert len(at_limit) == MAX_BYTES
        assert (await stack.post(chain.team, at_limit)).status_code == 201

        over = padded(MAX_BYTES + 1)
        too_big = await stack.post(chain.team, over)
        body = assert_problem(too_big, 413, "payload-too-large")
        assert str(MAX_BYTES) in body["detail"]

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(20):  # no Content-Length: the streaming cap must stop it
                yield b"#" + b"y" * 65535 + b"\n"

        streamed = await stack.client.post(
            f"/api/v1/contracts/folders/{chain.team}/contracts",
            content=chunks(),
            headers={**bearer(stack.admin_token), "Content-Type": "application/yaml"},
        )
        assert_problem(streamed, 413, "payload-too-large")


async def test_contracts_are_created_at_every_folder_level_and_an_archived_subtree_is_read_only(
    make_stack: StackFactory, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        expected = {
            stack.root_id: None,
            chain.org: chain.org,
            chain.project: chain.org,
            chain.team: chain.org,
        }
        for folder, organization_id in expected.items():
            response = await stack.post(folder, doc(unique_id()))
            assert response.status_code == 201, response.text
            contract = response.json()["contract"]
            assert contract["folder_id"] == folder
            assert contract["organization_id"] == organization_id

        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.project}/archive",
            headers=bearer(stack.admin_token),
        )
        assert archived.status_code == 200, archived.text
        before = counts(sql, stack.contracts_db_url)["contract"]
        for folder in (chain.project, chain.team):
            response = await stack.post(folder, doc(unique_id()))
            assert_problem(response, 409, "folder-archived")
        assert counts(sql, stack.contracts_db_url)["contract"] == before

        unknown = await stack.post(str(uuid.uuid4()), doc(unique_id()))
        assert_problem(unknown, 404, "not-found")


async def test_odcs_ids_are_unique_per_organization_and_per_root_scope(
    make_stack: StackFactory,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        shared_id = unique_id()

        assert (await stack.post(chain.org, doc(shared_id))).status_code == 201
        # Same Organization scope, any level below it.
        assert_problem(await stack.post(chain.team, doc(shared_id)), 409, "duplicate-contract-id")
        assert_problem(
            await stack.post(chain.project, doc(shared_id)), 409, "duplicate-contract-id"
        )
        # Another Organization is another scope.
        assert (await stack.post(other.team, doc(shared_id))).status_code == 201
        # ROOT is its own scope: the same id there succeeds once.
        assert (await stack.post(stack.root_id, doc(shared_id))).status_code == 201
        assert_problem(
            await stack.post(stack.root_id, doc(shared_id)), 409, "duplicate-contract-id"
        )


async def test_contract_create_follows_the_role_matrix_at_every_level(
    make_stack: StackFactory, fake_issuer: FakeIssuer
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()

        org_admin, principal = stack.token("org-admin")
        stack.grant_sql(principal, "org_admin", chain.org)
        assert (await stack.post(chain.org, doc(unique_id()), token=org_admin)).status_code == 201
        assert (await stack.post(chain.team, doc(unique_id()), token=org_admin)).status_code == 201
        # An organization admin holds nothing at ROOT: the folder is not even visible.
        assert (await stack.post(stack.root_id, doc(unique_id()), token=org_admin)).status_code in (
            403,
            404,
        )

        editor, principal = stack.token("project-editor")
        stack.grant_sql(principal, "project_editor", chain.project)
        assert (await stack.post(chain.project, doc(unique_id()), token=editor)).status_code == 201
        assert (await stack.post(chain.team, doc(unique_id()), token=editor)).status_code == 201
        assert_problem(
            await stack.post(chain.org, doc(unique_id()), token=editor), 404, "not-found"
        )

        team_editor, principal = stack.token("team-editor")
        stack.grant_sql(principal, "team_editor", chain.team)
        assert (
            await stack.post(chain.team, doc(unique_id()), token=team_editor)
        ).status_code == 201
        assert_problem(
            await stack.post(chain.project, doc(unique_id()), token=team_editor), 404, "not-found"
        )

        org_reader, principal = stack.token("org-reader")
        stack.grant_sql(principal, "org_reader", chain.org)
        assert_problem(
            await stack.post(chain.org, doc(unique_id()), token=org_reader), 403, "forbidden"
        )

        global_reader, principal = stack.token("global-reader")
        stack.grant_sql(principal, "global_reader", stack.root_id)
        assert_problem(
            await stack.post(stack.root_id, doc(unique_id()), token=global_reader),
            403,
            "forbidden",
        )


async def test_non_admin_gets_404_everywhere_with_the_body_of_an_unknown_object(
    make_stack: StackFactory, fake_issuer: FakeIssuer, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, doc(unique_id()))
        contract_id = created.json()["contract"]["id"]
        before = counts(sql, stack.contracts_db_url)
        token = fake_issuer.mint("someone-else")

        create = await stack.post(chain.team, doc(unique_id()), token=token)
        create_unknown = await stack.post(str(uuid.uuid4()), doc(unique_id()), token=token)
        assert_problem(create, 404, "not-found")
        assert stable(create.json()) == stable(create_unknown.json())

        missing = str(uuid.uuid4())
        for suffix in ("", "/document"):
            known = await stack.client.get(
                f"/api/v1/contracts/contracts/{contract_id}{suffix}", headers=bearer(token)
            )
            unknown = await stack.client.get(
                f"/api/v1/contracts/contracts/{missing}{suffix}", headers=bearer(token)
            )
            assert_problem(known, 404, "not-found")
            assert stable(known.json()) == stable(unknown.json())
        assert counts(sql, stack.contracts_db_url) == before


async def test_authentication_precedes_reading_the_body(make_stack: StackFactory) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        url = f"/api/v1/contracts/folders/{chain.team}/contracts"
        oversized = await stack.client.post(
            url, content=b"x" * (2 * MAX_BYTES), headers={"Content-Type": "application/yaml"}
        )
        assert_problem(oversized, 401, "unauthenticated")
        malformed = await stack.client.post(
            url, content=b"{not: [valid", headers={"Content-Type": "application/json"}
        )
        assert_problem(malformed, 401, "unauthenticated")


async def test_identity_down_fails_closed_with_503(
    make_stack: StackFactory, fake_issuer: FakeIssuer, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.post(chain.team, doc(unique_id()))
        contract_id = created.json()["contract"]["id"]
        before = counts(sql, stack.contracts_db_url)
        stack.identity_link.down = True
        token = fake_issuer.mint("anyone")

        create = await stack.post(chain.team, doc(unique_id()), token=token)
        assert_problem(create, 503, "service-unavailable")
        for suffix in ("", "/document"):
            read = await stack.client.get(
                f"/api/v1/contracts/contracts/{contract_id}{suffix}", headers=bearer(token)
            )
            assert_problem(read, 503, "service-unavailable")
        assert counts(sql, stack.contracts_db_url) == before


async def test_idempotency_same_document_again_is_409_and_writes_nothing_per_organization(
    make_stack: StackFactory, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        first = await stack.chain()
        second_team = await stack.team_in_org(first.org, "second")
        other = await stack.chain()
        raw = doc(unique_id())

        assert (await stack.post(first.team, raw)).status_code == 201
        after_first = counts(sql, stack.contracts_db_url)

        for team in (first.team, second_team):  # same team, and another team of the same org
            response = await stack.post(team, raw)
            assert_problem(response, 409, "duplicate-contract-id")
        assert counts(sql, stack.contracts_db_url) == after_first

        # The ODCS id is unique per Organization (tenant boundary), not globally.
        elsewhere = await stack.post(other.team, raw)
        assert elsewhere.status_code == 201, elsewhere.text
        assert counts(sql, stack.contracts_db_url)["contract"] == 2


async def test_concurrency_two_creates_with_one_id_yield_one_201_and_one_409(
    make_stack: StackFactory, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        second_team = await stack.team_in_org(chain.org, "second")
        raw = doc(unique_id())

        responses = await asyncio.gather(stack.post(chain.team, raw), stack.post(second_team, raw))
        assert sorted(r.status_code for r in responses) == [201, 409]
        loser = next(r for r in responses if r.status_code == 409)
        assert_problem(loser, 409, "duplicate-contract-id")
        assert counts(sql, stack.contracts_db_url) == {
            "contract": 1,
            "contract_version": 1,
            "outbox": 1,
        }


async def test_adjacency_a_create_writes_two_rows_but_exactly_one_event(
    make_stack: StackFactory, sql: Callable[..., Any]
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        assert (await stack.post(chain.team, doc(unique_id()))).status_code == 201
        assert counts(sql, stack.contracts_db_url) == {
            "contract": 1,
            "contract_version": 1,
            "outbox": 1,
        }
        version = sql(stack.contracts_db_url, "SELECT version_no, state FROM contract_version")[0]
        assert (version["version_no"], version["state"]) == (1, "draft")

        assert (await stack.post(chain.team, doc(unique_id()))).status_code == 201
        assert counts(sql, stack.contracts_db_url) == {
            "contract": 2,
            "contract_version": 2,
            "outbox": 2,
        }
        actions = sql(stack.contracts_db_url, "SELECT action, aggregate_version FROM outbox")
        assert {(a["action"], a["aggregate_version"]) for a in actions} == {("contract.created", 1)}
