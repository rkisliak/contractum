"""Edge refusals of the contract routes: every doubt is a problem, never a partial answer.

* An identity outage while create-from-version checks the source, or while delete reads which
  referrers it may name, is 503 ``service-unavailable`` and nothing changes.
* The stored validation report is never served on a doubt: validation not configured (503), the
  run not readable by the caller (404), or a run for other bytes (503).
* A dry-run without a validation client is 503 before any contract is read.
* An unknown draft id is the uniform 404 on the draft document route.
* A draft-only contract can be deleted and restored (no published links to re-check), and a
  purge of a folder with nothing left answers zero counts with one counts-only event.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"
SERVICE_UNAVAILABLE = "urn:dc:problem:service-unavailable"
VALIDATION_UNAVAILABLE = "urn:dc:problem:validation-unavailable"
NOT_FOUND = "urn:dc:problem:not-found"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_problem(response: Any, status: int, problem_type: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["type"] == problem_type


def new_odcs_id() -> str:
    return f"urn:dc:edges:{uuid.uuid4().hex}"


async def test_identity_outages_on_source_check_and_referrer_scope_are_503(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        source, _ = await stack.publish_versions(chain.team, ["1.0.0"])
        admin = bearer(stack.admin_token)

        with stack.flaky_authz(fail_permissions=frozenset({"contract:read-published"})):
            copied = await stack.client.post(
                f"/api/v1/contracts/folders/{chain.team}/contracts/from-version",
                json={
                    "source_contract_id": source,
                    "source_version": "1.0.0",
                    "odcs_id": new_odcs_id(),
                },
                headers=admin,
            )
        assert_problem(copied, 503, SERVICE_UNAVAILABLE)

        with stack.flaky_authz(fail_permissions=frozenset({"contract:read-metadata"})):
            deleted = await stack.client.delete(f"{CONTRACTS}/{source}", headers=admin)
        assert_problem(deleted, 503, SERVICE_UNAVAILABLE)

        rows = sql(
            stack.contracts_db_url, "SELECT count(*) AS n, count(deleted_at) AS gone FROM contract"
        )
        assert rows == [{"n": 1, "gone": 0}], "no copy was made and nothing was deleted"


async def test_the_validation_report_is_never_served_on_a_doubt(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await stack.publish_versions(chain.team, ["1.0.0"])
        url = f"{CONTRACTS}/{contract}/versions/1.0.0/validation-report"
        admin = bearer(stack.admin_token)
        link = stack.validation_link
        state = stack.contracts_app.state

        served = await stack.client.get(url, headers=admin)
        assert served.status_code == 200, served.text

        client = state.validation_client
        del state.validation_client
        try:
            unconfigured = await stack.client.get(url, headers=admin)
        finally:
            state.validation_client = client
        assert_problem(unconfigured, 503, VALIDATION_UNAVAILABLE)

        link.force(404, {"Content-Type": "application/problem+json"}, b"{}")
        try:
            not_readable = await stack.client.get(url, headers=admin)
        finally:
            link.restore()
        assert_problem(not_readable, 404, NOT_FOUND)

        link.rewrite = lambda body: {**body, "content_sha256": "0" * 64}
        try:
            other_bytes = await stack.client.get(url, headers=admin)
        finally:
            link.restore()
        assert_problem(other_bytes, 503, VALIDATION_UNAVAILABLE)
        assert other_bytes.json()["detail"] == "Contract validation answered for different bytes."


async def test_dry_run_without_validation_and_an_unknown_draft_document(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _draft, _etag = await stack.create_contract(chain.team, stack_doc(new_odcs_id()))
        admin = bearer(stack.admin_token)
        state = stack.contracts_app.state

        client = state.validation_client
        del state.validation_client
        try:
            dry_run = await stack.client.post(
                "/api/v1/contracts/governance/rule-set-dry-runs",
                content=json.dumps({"rule_set_id": str(uuid.uuid4()), "version": "draft"}),
                headers={**admin, "Content-Type": "application/json"},
            )
        finally:
            state.validation_client = client
        assert_problem(dry_run, 503, VALIDATION_UNAVAILABLE)

        unknown = await stack.client.get(
            f"{CONTRACTS}/{contract}/drafts/{uuid.uuid4()}/document", headers=admin
        )
        assert_problem(unknown, 404, NOT_FOUND)


async def test_a_draft_only_contract_restores_and_an_empty_folder_purges_to_zero(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _draft, _etag = await stack.create_contract(chain.team, stack_doc(new_odcs_id()))
        admin = bearer(stack.admin_token)

        deleted = await stack.client.delete(f"{CONTRACTS}/{contract}", headers=admin)
        assert deleted.status_code == 204, deleted.text
        restored = await stack.client.post(f"{CONTRACTS}/{contract}/restore", headers=admin)
        assert restored.status_code == 200, restored.text
        assert restored.json()["id"] == contract
        events = sql(
            stack.contracts_db_url,
            "SELECT action FROM outbox WHERE aggregate_id = %s ORDER BY id",
            contract,
        )
        assert [e["action"] for e in events][-2:] == ["contract.deleted", "contract.restored"]

        empty = await stack.folder(chain.project, f"empty-{uuid.uuid4().hex[:6]}")
        purged = await stack.client.post(
            f"/internal/v1/contracts/folders/{empty}/purge", headers=admin
        )
        assert purged.status_code == 200, purged.text
        zero = {
            "deleted_contracts": 0,
            "contract_versions": 0,
            "archived_subscriber_cards": 0,
            "card_subscriptions": 0,
            "origin_cleared": 0,
        }
        assert purged.json() == {"folder_id": empty, **zero}
        [event] = sql(
            stack.contracts_db_url,
            "SELECT after FROM outbox WHERE action = 'folder.purged' AND aggregate_id = %s",
            empty,
        )
        assert event["after"] == zero


def stack_doc(odcs_id: str) -> bytes:
    """A minimal valid draft document (the stack's own fixture shape)."""
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
        "status: draft\nname: Edges\n"
    ).encode()


async def test_publish_refuses_bytes_that_changed_during_the_gate_at_the_same_revision(
    make_stack: MakeStack, sql: Sql
) -> None:
    """The hash re-check under the lock catches a change that did not move the revision."""
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(chain.team, stack_doc(new_odcs_id()))

        async def change_bytes_behind_the_revision(request: Any) -> None:
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET content_sha256 = %s WHERE id = %s",
                "0" * 64,
                draft,
            )

        stack.validation_link.on_request = change_bytes_behind_the_revision
        try:
            refused = await stack.publish(contract, draft, etag)
        finally:
            stack.validation_link.restore()

        assert_problem(refused, 412, "urn:dc:problem:precondition-failed")
        [row] = sql(
            stack.contracts_db_url,
            "SELECT state, revision FROM contract_version WHERE id = %s",
            draft,
        )
        assert row == {"state": "draft", "revision": 1}
