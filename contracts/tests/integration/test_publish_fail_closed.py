"""The publish gate fails closed in every branch (plan 05-17; VAL-07, D-131, T-5-41, T-5-42).

contracts reaches validation over a ``ValidationLink`` that can be cut, slowed, answered with a
canned status or have its real answer rewritten. Whatever validation does, the draft stays a draft,
no version row is published, no pins are written and no ``contract.version_published`` event is
written; a clean answer afterwards publishes the same draft.
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


def doc(odcs_id: str) -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
        "status: draft\nname: Orders\nschema:\n  - name: orders\n    properties:\n"
        "      - name: id\n        logicalType: string\n"
    ).encode()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _editor_draft(stack: Any) -> tuple[str, str, str, str, str]:
    """A conformant draft in a fresh team, owned by an editor of that team."""
    chain = await stack.chain()
    token, principal_id = stack.token(f"ed-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal_id, "team_editor", chain.team)
    contract, draft, etag = await stack.create_contract(
        chain.team, doc(f"urn:dc:fc:{uuid.uuid4().hex}"), token=str(token)
    )
    return contract, draft, etag, str(token), chain.team


def _events(stack: Any, sql: Sql) -> int:
    rows = sql(
        stack.contracts_db_url,
        "SELECT count(*) AS n FROM outbox WHERE action = 'contract.version_published'",
    )
    return int(rows[0]["n"])


def _row(stack: Any, sql: Sql, draft: str) -> dict[str, Any]:
    return sql(stack.contracts_db_url, "SELECT * FROM contract_version WHERE id = %s", draft)[0]


def _latest(stack: Any, sql: Sql, contract: str) -> Any:
    return sql(
        stack.contracts_db_url, "SELECT latest_version_id FROM contract WHERE id = %s", contract
    )[0]["latest_version_id"]


class Baseline:
    """What an unpublished draft looks like, captured before a refused publish."""

    def __init__(self, stack: Any, sql: Sql, contract: str, draft: str) -> None:
        self.stack, self.sql, self.contract, self.draft = stack, sql, contract, draft
        self.events = _events(stack, sql)
        self.revision = _row(stack, sql, draft)["revision"]
        self.latest = _latest(stack, sql, contract)

    def assert_unchanged(self) -> None:
        row = _row(self.stack, self.sql, self.draft)
        assert row["state"] == "draft"
        assert row["revision"] == self.revision
        assert row["validation_run_id"] is None
        assert row["pinned_rule_sets"] is None
        assert row["engine_version"] is None
        assert row["schema_sha"] is None
        assert _events(self.stack, self.sql) == self.events
        assert _latest(self.stack, self.sql, self.contract) == self.latest


# -- tracer: validation unreachable --------------------------------------------------------


async def test_with_validation_unreachable_a_publish_is_503_and_nothing_is_published(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, draft, etag, ed, _ = await _editor_draft(stack)
        before = Baseline(stack, sql, contract, draft)
        stack.validation_link.down = True

        refused = await stack.publish(contract, draft, etag, token=ed)

        assert refused.status_code == 503, refused.text
        assert refused.json()["type"] == "urn:dc:problem:validation-unavailable"
        before.assert_unchanged()

        stack.validation_link.restore()
        published = await stack.publish(contract, draft, etag, token=ed)
        assert published.status_code == 201, published.text
        assert _row(stack, sql, draft)["state"] == "published"


# -- every other branch ------------------------------------------------------------


async def _refused_and_untouched(
    make_stack: MakeStack, sql: Sql, arm: Callable[[Any], None], **settings: Any
) -> tuple[Any, Any]:
    """Arm a failure, publish once, prove nothing was published; returns (response, stack)."""
    async with make_stack(**settings) as stack:
        contract, draft, etag, ed, _ = await _editor_draft(stack)
        before = Baseline(stack, sql, contract, draft)
        arm(stack.validation_link)

        response = await stack.publish(contract, draft, etag, token=ed)

        before.assert_unchanged()
        stack.validation_link.restore()
        again = await stack.publish(contract, draft, etag, token=ed)
        assert again.status_code == 201, again.text
        return response, stack


async def test_a_validation_answer_slower_than_the_publish_timeout_is_503(
    make_stack: MakeStack, sql: Sql
) -> None:
    def slow(link: Any) -> None:
        link.delay_s = 2.0

    response, _ = await _refused_and_untouched(
        make_stack, sql, slow, validation_publish_timeout_s=0.5
    )

    assert response.status_code == 503, response.text
    assert response.json()["type"] == "urn:dc:problem:validation-unavailable"


async def test_a_validation_500_is_503_and_the_body_is_not_echoed(
    make_stack: MakeStack, sql: Sql
) -> None:
    def broken(link: Any) -> None:
        link.force(500, body=b"Traceback secret-internal-detail")

    response, _ = await _refused_and_untouched(make_stack, sql, broken)

    assert response.status_code == 503, response.text
    assert response.json()["type"] == "urn:dc:problem:validation-unavailable"
    assert "secret-internal-detail" not in response.text


async def test_a_validation_busy_answer_is_503_and_copies_retry_after(
    make_stack: MakeStack, sql: Sql
) -> None:
    def busy(link: Any) -> None:
        link.force(503, {"Retry-After": "7"})

    response, _ = await _refused_and_untouched(make_stack, sql, busy)

    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "7"
    assert response.json()["type"] == "urn:dc:problem:validation-unavailable"


@pytest.mark.parametrize("status", [408, 429])
async def test_a_slow_or_saturated_validation_is_503_not_a_refusal(
    make_stack: MakeStack, sql: Sql, status: int
) -> None:
    def arm(link: Any) -> None:
        link.force(status)

    response, _ = await _refused_and_untouched(make_stack, sql, arm)

    assert response.status_code == 503, response.text
    assert response.json()["type"] == "urn:dc:problem:validation-unavailable"


async def test_an_unreadable_validation_answer_is_503(make_stack: MakeStack, sql: Sql) -> None:
    def garbage(link: Any) -> None:
        link.force(200, {"Content-Type": "application/json"}, b"<html>not json</html>")

    response, _ = await _refused_and_untouched(make_stack, sql, garbage)

    assert response.status_code == 503, response.text
    assert response.json()["type"] == "urn:dc:problem:validation-unavailable"


async def test_an_answer_for_different_bytes_is_503(make_stack: MakeStack, sql: Sql) -> None:
    def other_bytes(link: Any) -> None:
        def change(answer: dict[str, Any]) -> dict[str, Any]:
            answer["content_sha256"] = "0" * 64
            return answer

        link.rewrite = change

    response, _ = await _refused_and_untouched(make_stack, sql, other_bytes)

    assert response.status_code == 503, response.text
    assert response.json()["type"] == "urn:dc:problem:validation-unavailable"


async def test_a_validation_401_is_passed_through_as_401(make_stack: MakeStack, sql: Sql) -> None:
    def unauth(link: Any) -> None:
        link.force(401)

    response, _ = await _refused_and_untouched(make_stack, sql, unauth)

    assert response.status_code == 401, response.text
    assert response.json()["type"] == "urn:dc:problem:unauthenticated"


@pytest.mark.parametrize("slug", ["principal-not-registered", "interactive-session-required"])
async def test_identitys_known_403_problems_are_passed_through_unchanged(
    make_stack: MakeStack, sql: Sql, slug: str
) -> None:
    def refuse(link: Any) -> None:
        body = json.dumps({"type": f"urn:dc:problem:{slug}", "status": 403}).encode()
        link.force(403, {"Content-Type": "application/problem+json"}, body)

    response, _ = await _refused_and_untouched(make_stack, sql, refuse)

    assert response.status_code == 403, response.text
    assert response.json()["type"] == f"urn:dc:problem:{slug}"


async def test_a_plain_validation_403_is_403_forbidden(make_stack: MakeStack, sql: Sql) -> None:
    def refuse(link: Any) -> None:
        link.force(403, body=b'{"detail": "internal-validation-detail"}')

    response, _ = await _refused_and_untouched(make_stack, sql, refuse)

    assert response.status_code == 403, response.text
    assert response.json()["type"] == "urn:dc:problem:forbidden"
    assert "internal-validation-detail" not in response.text


@pytest.mark.parametrize("status", [400, 404, 413, 415, 422])
async def test_any_other_validation_4xx_is_503_not_a_permission_error_wr07(
    make_stack: MakeStack, sql: Sql, status: int
) -> None:
    def refuse(link: Any) -> None:
        link.force(status, body=b'{"detail": "internal-validation-detail"}')

    response, _ = await _refused_and_untouched(make_stack, sql, refuse)

    assert response.status_code == 503, response.text
    assert response.json()["type"] == "urn:dc:problem:validation-unavailable"
    assert "internal-validation-detail" not in response.text
