"""Refusals derived from a decision's permissions are recorded denials (AUD-04, D-172, WR-01).

The route's own check is ALLOW (a Consumer holds ``contract:read-published``), and the handler
then refuses because the decision lacks another permission. Each such refusal asks identity's PDP
for that decisive permission first, so identity writes exactly one ``check.denied`` row naming it,
while the caller still gets the uniform 404.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
CONTRACTS = "/api/v1/contracts/contracts"
UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, version: str = "1.0.0") -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        "status: draft\nname: Orders\n"
    ).encode()


def denials(sql: Sql, stack: Any, principal_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.identity_db_url,
        "SELECT aggregate_id, detail FROM outbox WHERE aggregate_type = 'authz' "
        "AND action = 'check.denied' AND actor->>'id' = %s ORDER BY id",
        principal_id,
    )


def assert_uniform_404(response: httpx.Response) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["type"] == "urn:dc:problem:not-found"


def assert_one_denial(
    rows: list[dict[str, Any]], permission: str, folder_id: str, route: str
) -> None:
    assert len(rows) == 1, rows
    assert str(rows[0]["aggregate_id"]) == folder_id
    assert rows[0]["detail"] == {
        "permission": permission,
        "outcome": "not_found",
        "service": "contracts",
        "route": route,
    }


async def _draft_only(stack: Any, team: str) -> str:
    contract, _draft, _etag = await stack.create_contract(team, doc(f"urn:dc:rr:{uuid.uuid4()}"))
    return contract


async def _with_unpublished(stack: Any, team: str) -> str:
    """A contract whose Latest is 1.0.0 and whose 1.1.0 is unpublished."""
    contract, _ids = await stack.publish_versions(team, ["1.0.0", "1.1.0"])
    hidden = await stack.client.post(
        f"{CONTRACTS}/{contract}/versions/1.1.0/unpublish", headers=bearer(stack.admin_token)
    )
    assert hidden.status_code == 200, hidden.text
    return contract


async def test_a_consumer_fetching_a_draft_only_contract_records_a_contract_read_denial(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract = await _draft_only(stack, chain.team)
        token, principal = stack.token(f"cs-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "consumer", chain.team)

        fetched = await stack.client.get(
            f"{CONTRACTS}/{contract}/versions/latest", headers=bearer(token)
        )
        assert_uniform_404(fetched)
        assert_one_denial(
            denials(sql, stack, principal),
            "contract:read",
            chain.team,
            "/api/v1/contracts/contracts/{contract_id}/versions/{ref}",
        )

        listed = await stack.client.get(f"{CONTRACTS}/{contract}/versions", headers=bearer(token))
        assert_uniform_404(listed)
        assert len(denials(sql, stack, principal)) == 2

        meta = await stack.client.get(f"{CONTRACTS}/{contract}", headers=bearer(token))
        assert_uniform_404(meta)
        rows = denials(sql, stack, principal)
        assert len(rows) == 3
        assert rows[-1]["detail"]["permission"] == "contract:read-metadata"
        assert rows[-1]["detail"]["route"] == "/api/v1/contracts/contracts/{contract_id}"


async def test_an_unpublished_version_by_fetch_or_diff_records_a_contract_publish_denial(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract = await _with_unpublished(stack, chain.team)
        token, principal = stack.token(f"cs-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "consumer", chain.team)

        fetched = await stack.client.get(
            f"{CONTRACTS}/{contract}/versions/1.1.0", headers=bearer(token)
        )
        assert_uniform_404(fetched)
        assert_one_denial(
            denials(sql, stack, principal),
            "contract:publish",
            chain.team,
            "/api/v1/contracts/contracts/{contract_id}/versions/{ref}",
        )
        # A version that does not exist at all is the same 404 and is not a permission refusal.
        missing = await stack.client.get(
            f"{CONTRACTS}/{contract}/versions/9.9.9", headers=bearer(token)
        )
        assert_uniform_404(missing)
        assert len(denials(sql, stack, principal)) == 1

        diff = await stack.client.get(
            f"{CONTRACTS}/{contract}/diff",
            params={"from": "1.0.0", "to": "1.1.0"},
            headers=bearer(token),
        )
        assert_uniform_404(diff)
        rows = denials(sql, stack, principal)
        assert len(rows) == 2
        assert rows[-1]["detail"]["permission"] == "contract:publish"
        assert rows[-1]["detail"]["route"] == "/api/v1/contracts/contracts/{contract_id}/diff"

        draft_ref = await stack.client.get(
            f"{CONTRACTS}/{contract}/diff",
            params={"from": "1.0.0", "to": UNKNOWN},
            headers=bearer(token),
        )
        assert_uniform_404(draft_ref)
        rows = denials(sql, stack, principal)
        assert len(rows) == 3
        assert rows[-1]["detail"]["permission"] == "contract:read"


async def test_an_unpublished_create_from_version_source_records_a_denial_at_the_source(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        source_chain = await stack.chain("srcorg")
        target_chain = await stack.chain("dstorg")
        contract = await _with_unpublished(stack, source_chain.team)
        token, principal = stack.token(f"ed-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "team_editor", target_chain.team)
        stack.grant_sql(principal, "consumer", source_chain.team)

        copied = await stack.client.post(
            f"/api/v1/contracts/folders/{target_chain.team}/contracts/from-version",
            json={
                "source_contract_id": contract,
                "source_version": "1.1.0",
                "odcs_id": f"urn:dc:rr:copy:{uuid.uuid4()}",
            },
            headers=bearer(token),
        )
        assert_uniform_404(copied)
        assert_one_denial(
            denials(sql, stack, principal),
            "contract:publish",
            source_chain.team,
            "/api/v1/contracts/folders/{folder_id}/contracts/from-version",
        )


async def test_a_published_only_reader_on_a_never_published_template_records_a_denial(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.client.post(
            f"/api/v1/contracts/folders/{chain.org}/templates",
            params={"name": "T"},
            content=(FIXTURES / "valid-v3.2.0.yaml").read_bytes(),
            headers={**bearer(stack.admin_token), "Content-Type": "application/yaml"},
        )
        assert created.status_code == 201, created.text
        template = created.json()["template"]["id"]
        token, principal = stack.token(f"gr-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "global_reader", stack.root_id)

        read = await stack.client.get(
            f"/api/v1/contracts/templates/{template}", headers=bearer(token)
        )
        assert_uniform_404(read)
        assert_one_denial(
            denials(sql, stack, principal),
            "template:read",
            chain.org,
            "/api/v1/contracts/templates/{template_id}",
        )
