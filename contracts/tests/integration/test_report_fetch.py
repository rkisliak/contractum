"""The stored validation report of a published version (VAL-09, D-155, D-36).

``GET /api/v1/contracts/contracts/{contract_id}/versions/{ref}/validation-report`` answers anyone
who can fetch that version (``contract:read-published`` at the contract folder plus the version
visibility rules) with the report the version was published with. Contracts reads the version's
``validation_run_id`` and asks validation, which authorises the read again with the caller's token.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from dc_contracts.versions import PassThroughPublishGate

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *names: str, version: str = "1.0.0") -> bytes:
    """A conformant ODCS v3.2.0 document with one table holding the named properties."""
    props = "".join(f"      - name: {n}\n        logicalType: string\n" for n in names)
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: Orders\nschema:\n  - name: orders\n    properties:\n{props}"
    ).encode()


def new_id() -> str:
    return f"urn:dc:report-fetch:{uuid.uuid4().hex}"


def report_url(contract: str, ref: str) -> str:
    return f"{CONTRACTS}/{contract}/versions/{ref}/validation-report"


def stable(response: Any) -> tuple[int, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, sorted(body.items())


async def editor_at(stack: Any, team: str, sub: str = "ed") -> str:
    token, principal = stack.token(f"{sub}-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "team_editor", team)
    return str(token)


async def consumer_at(stack: Any, folder: str, sub: str = "cs") -> str:
    token, principal = stack.token(f"{sub}-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "consumer", folder)
    return str(token)


async def publish_first(stack: Any, team: str, token: str, odcs_id: str) -> str:
    contract, draft, etag = await stack.create_contract(
        team, doc(odcs_id, "id", "amount"), token=token
    )
    published = await stack.publish(contract, draft, etag, token=token)
    assert published.status_code == 201, published.text
    return str(contract)


async def publish_next(
    stack: Any, contract: str, token: str, odcs_id: str, version: str, *names: str
) -> Any:
    created = await stack.client.post(
        f"{CONTRACTS}/{contract}/drafts",
        json={"from_version": "1.0.0", "bump": "none"},
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        f"{CONTRACTS}/{contract}/drafts/{draft['id']}",
        content=doc(odcs_id, *names, version=version),
        headers={**bearer(token), "Content-Type": "application/yaml", "If-Match": draft["etag"]},
    )
    assert saved.status_code == 200, saved.text
    return await stack.publish(contract, draft["id"], saved.headers["etag"], token=token)


# -- tracer ---------------------------------------------------------------------------------------


async def test_a_consumer_fetches_the_stored_validation_report_of_a_published_version(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor = await editor_at(stack, chain.team)
        contract = await publish_first(stack, chain.team, editor, new_id())
        consumer = await consumer_at(stack, chain.team)
        stack.validation_link.requests.clear()

        response = await stack.client.get(report_url(contract, "1.0.0"), headers=bearer(consumer))

        assert response.status_code == 200, response.text
        body = response.json()
        stored = sql(
            stack.contracts_db_url,
            "SELECT id, validation_run_id, content_sha256 FROM contract_version"
            " WHERE contract_id = %s AND state = 'published'",
            contract,
        )[0]
        assert body["contract_id"] == contract
        assert body["version_id"] == str(stored["id"])
        assert body["semver"] == "1.0.0"
        assert body["run_id"] == str(stored["validation_run_id"])
        assert body["content_sha256"] == stored["content_sha256"]
        report = body["report"]
        assert report["summary"]["publishable"] is True
        assert isinstance(report["findings"], list)
        assert report["compatibility"]["status"] == "no-baseline"
        assert "ref_facts" in report
        assert isinstance(body["pins"], list)
        assert response.headers["etag"] == f'"run-{stored["validation_run_id"]}"'
        assert "private" in response.headers["cache-control"]
        # Contracts forwarded the consumer's own token to validation's self-authorising read.
        sent = stack.validation_link.requests[-1]
        assert sent.url.path == f"/internal/v1/validation/runs/{stored['validation_run_id']}"
        assert sent.url.params["contract_id"] == contract
        assert sent.headers["authorization"] == f"Bearer {consumer}"

        latest = await stack.client.get(report_url(contract, "latest"), headers=bearer(consumer))
        assert latest.status_code == 200, latest.text
        assert latest.json() == body


# -- edge cases (Task 2) --------------------------------------------------------------------------


async def test_a_version_published_before_the_gate_recorded_runs_is_not_recorded(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor = await editor_at(stack, chain.team)
        # Versions published before the Phase 5 gate have no run: the pass-through test double
        # publishes exactly like that (validation_run_id NULL), Pitfall 5.
        stack.contracts_app.state.publish_gate = PassThroughPublishGate()
        contract = await publish_first(stack, chain.team, editor, new_id())
        stored = sql(
            stack.contracts_db_url,
            "SELECT validation_run_id FROM contract_version WHERE contract_id = %s",
            contract,
        )
        assert stored[0]["validation_run_id"] is None
        consumer = await consumer_at(stack, chain.team)
        stack.validation_link.requests.clear()

        for ref in ("1.0.0", "latest"):
            response = await stack.client.get(report_url(contract, ref), headers=bearer(consumer))
            assert response.status_code == 404, response.text
            assert response.json()["type"] == "urn:dc:problem:validation-report-not-recorded"
        assert stack.validation_link.requests == []


async def test_a_policy_manager_or_another_teams_editor_gets_the_unknown_contract_404(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor = await editor_at(stack, chain.team)
        contract = await publish_first(stack, chain.team, editor, new_id())
        manager = stack.policy_manager(chain.org)
        other_team = await stack.team_in_org(chain.org, "other")
        other_editor = await editor_at(stack, other_team, "other")
        reference = await stack.client.get(
            report_url(str(uuid.uuid4()), "1.0.0"), headers=bearer(editor)
        )
        assert reference.status_code == 404

        for token in (manager, other_editor):
            response = await stack.client.get(report_url(contract, "1.0.0"), headers=bearer(token))
            assert stable(response) == stable(reference)
            assert "report" not in response.json()


async def test_if_none_match_revalidates_only_after_authorisation(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor = await editor_at(stack, chain.team)
        contract = await publish_first(stack, chain.team, editor, new_id())
        consumer = await consumer_at(stack, chain.team)
        first = await stack.client.get(report_url(contract, "latest"), headers=bearer(consumer))
        assert first.status_code == 200, first.text
        etag = first.headers["etag"]

        again = await stack.client.get(
            report_url(contract, "1.0.0"), headers={**bearer(consumer), "If-None-Match": etag}
        )
        assert again.status_code == 304, again.text
        assert again.content == b""
        assert again.headers["etag"] == etag
        assert "private" in again.headers["cache-control"]

        # The same header from a caller who may not read the version is still the 404 (T-6-28).
        manager = stack.policy_manager(chain.org)
        denied = await stack.client.get(
            report_url(contract, "1.0.0"), headers={**bearer(manager), "If-None-Match": etag}
        )
        assert denied.status_code == 404
        assert denied.json()["type"] == "urn:dc:problem:not-found"


async def test_a_breaking_major_reports_its_compatibility_against_the_baseline(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor = await editor_at(stack, chain.team)
        odcs_id = new_id()
        contract = await publish_first(stack, chain.team, editor, odcs_id)
        published = await publish_next(stack, contract, editor, odcs_id, "2.0.0", "id")
        assert published.status_code == 201, published.text
        consumer = await consumer_at(stack, chain.team)

        response = await stack.client.get(report_url(contract, "2.0.0"), headers=bearer(consumer))

        assert response.status_code == 200, response.text
        compatibility = response.json()["report"]["compatibility"]
        assert compatibility["counts"]["breaking"] >= 1
        assert compatibility["baseline"]["semver"] == "1.0.0"
        assert compatibility["required_bump"] == "major"
        older = await stack.client.get(report_url(contract, "1.0.0"), headers=bearer(consumer))
        assert older.json()["report"]["compatibility"]["status"] == "no-baseline"
        assert older.headers["etag"] != response.headers["etag"]


async def test_an_unreachable_or_failing_validation_service_is_503_never_an_empty_report(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor = await editor_at(stack, chain.team)
        contract = await publish_first(stack, chain.team, editor, new_id())
        consumer = await consumer_at(stack, chain.team)

        stack.validation_link.down = True
        down = await stack.client.get(report_url(contract, "1.0.0"), headers=bearer(consumer))
        stack.validation_link.restore()
        stack.validation_link.force(500)
        failing = await stack.client.get(report_url(contract, "1.0.0"), headers=bearer(consumer))
        stack.validation_link.restore()

        for response in (down, failing):
            assert response.status_code == 503, response.text
            assert response.json()["type"] == "urn:dc:problem:validation-unavailable"
            assert "report" not in response.json()
