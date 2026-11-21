"""Compliance drift through contracts and the real validation app (RULE-11, D-158, D-160).

``GET /api/v1/contracts/governance/drift?folder_id=&cursor=`` pages through the published
contracts under the query folder (``governance:drift-read`` there, and inside the caller's scope),
sends their Latest documents to validation's drift route and lists the contracts that are
``drifted`` (blocking findings under an enforced version) or ``at_risk`` (only warn-mode versions
fail), each with the version's pinned rule sets next to the versions that apply now. Compliant
contracts are counted, not listed. Purely informational: no state changes and nobody is told, but
every answered call writes one counts-only audit event (``governance.drift_read``, CR-01), and
JSON Pointers are returned only where the caller holds ``contract:read``.
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

DRIFT = "/api/v1/contracts/governance/drift"
DRIFT_PATH = "/drift"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *names: str) -> bytes:
    props = "".join(f"      - name: {n}\n        logicalType: string\n" for n in names)
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
        f"status: draft\nname: Orders\nschema:\n  - name: orders\n    properties:\n{props}"
    ).encode()


def forbid(name: str) -> str:
    """A rule refusing a property called ``name``, with its pass and fail fixtures."""
    table = lambda *n: {"schema": [{"name": "orders", "properties": [{"name": x} for x in n]}]}  # noqa: E731
    return json.dumps(
        {
            "target": "$.schema[*]",
            "condition": f'this.properties.all(p, p.name != "{name}")',
            "severity": "error",
            "message": f"No {name} property.",
            "fixtures": {
                "pass": [{"document": table("id", "name")}],
                "fail": [{"document": table("id", name)}],
            },
        }
    )


async def published(stack: Any, team: str, *names: str) -> str:
    contract, draft, etag = await stack.create_contract(
        team, doc(f"urn:dc:drift:{uuid.uuid4().hex}", *names)
    )
    response = await stack.publish(contract, draft, etag)
    assert response.status_code == 201, response.text
    return str(contract)


async def world(stack: Any) -> dict[str, Any]:
    """A mandatory set at O: A published under v1 in warn, then v1 enforced; v2 in warn fails B.

    A ("legacy") fails v1, which is now enforced: drifted. B ("obsolete") passes v1 but fails the
    new v2, still in its warn window: at risk. C passes both: compliant.
    """
    org = await stack.folder(stack.root_id, f"o{uuid.uuid4().hex[:10]}")
    t1 = await stack.team_in_org(org, "one")
    t2 = await stack.team_in_org(org, "two")
    pm = stack.policy_manager(org)
    rule_set = await stack.mandatory_rule_set(
        org, {"no-legacy": forbid("legacy")}, enforce=False, name="Org naming", token=pm
    )
    a = await published(stack, t1, "id", "legacy")
    base = f"/api/v1/validation/rule-sets/{rule_set}"
    enforced = await stack.validation.post(f"{base}/versions/1/enforce-now", headers=bearer(pm))
    assert enforced.status_code == 200, enforced.text
    await stack.publish_rule_version(rule_set, pm, {"no-obsolete": forbid("obsolete")})
    b = await published(stack, t2, "id", "obsolete")
    c = await published(stack, t2, "id", "name")
    return {"org": org, "t1": t1, "t2": t2, "pm": pm, "rule_set": rule_set, "a": a, "b": b, "c": c}


def drift_calls(stack: Any) -> list[Any]:
    return [r for r in stack.validation_link.requests if r.url.path.endswith(DRIFT_PATH)]


def footprint(sql: Sql, stack: Any) -> list[dict[str, Any]]:
    """Everything a drift call must leave alone in contracts' database."""
    return sql(
        stack.contracts_db_url,
        "SELECT (SELECT count(*) FROM outbox) AS outbox,"
        " (SELECT count(*) FROM card_notice) AS notices,"
        " (SELECT max(updated_at) FROM contract) AS contracts,"
        " (SELECT max(updated_at) FROM contract_version) AS versions",
    )


async def test_drift_lists_drifted_and_at_risk_contracts_and_counts_compliant_ones(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        before = footprint(sql, stack)

        response = await stack.client.get(
            DRIFT, params={"folder_id": w["org"]}, headers=bearer(w["pm"])
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["folder_id"] == w["org"]
        assert body["summary"] == {
            "drifted": 1,
            "at_risk": 1,
            "compliant": 1,
            "unparseable": 0,
            "hidden": 0,
        }
        assert body["total_in_scope"] == 3 and body["next_cursor"] is None
        by_id = {item["contract_id"]: item for item in body["items"]}
        assert set(by_id) == {w["a"], w["b"]}, "compliant contracts are counted, not listed"
        a, b = by_id[w["a"]], by_id[w["b"]]
        assert a["status"] == "drifted" and b["status"] == "at_risk"
        assert a["latest_semver"] == "1.0.0" and a["folder_id"] == w["t1"]
        # A was published while v1 was in warn: its pin says so; v1 is enforced now.
        [pin] = [p for p in a["pinned_rule_sets"] if p["rule_set_id"] == w["rule_set"]]
        assert (pin["version"], pin["mode"], pin["source"]) == (1, "warn", "mandatory")
        current = {(v["version_no"], v["mode"]) for v in a["current_versions"]}
        assert current == {(1, "enforce"), (2, "warn")}
        [failing_b] = b["failing_rules"]
        assert (failing_b["rule_key"], failing_b["version_no"]) == ("no-obsolete", 2)
        assert failing_b["blocking"] is False
        assert "legacy" not in json.dumps(a["counts"])
        # CR-01: the Policy Manager may not read content, so every pointer is withheld.
        for item in (a, b):
            for failing in item["failing_rules"]:
                assert failing["pointers"] == [] and failing["pointers_withheld"] is True
        # Purely informational: one audit row, no notice, no contract or version row changed.
        after = footprint(sql, stack)
        assert after[0]["outbox"] == before[0]["outbox"] + 1
        assert {k: v for k, v in after[0].items() if k != "outbox"} == {
            k: v for k, v in before[0].items() if k != "outbox"
        }
        [row] = sql(
            stack.contracts_db_url,
            "SELECT aggregate_type, aggregate_id, after FROM outbox WHERE action = %s",
            "governance.drift_read",
        )
        assert (row["aggregate_type"], row["aggregate_id"]) == ("folder", w["org"])
        assert row["after"] == {
            "continued": False,
            "listed": 2,
            "total_in_scope": 3,
            "drifted": 1,
            "at_risk": 1,
            "compliant": 1,
            "unparseable": 0,
            "hidden": 0,
        }
        [call] = drift_calls(stack)
        assert call.headers["authorization"] == f"Bearer {w['pm']}"


async def test_drift_lists_a_published_contract_that_sits_at_the_queried_organization(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        org = await stack.folder(stack.root_id, f"o{uuid.uuid4().hex[:10]}")
        pm = stack.policy_manager(org)
        await stack.mandatory_rule_set(
            org, {"no-legacy": forbid("legacy")}, enforce=False, token=pm
        )
        at_org = await published(stack, org, "id", "legacy")
        await published(stack, org, "id", "name")

        response = await stack.client.get(DRIFT, params={"folder_id": org}, headers=bearer(pm))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total_in_scope"] == 2
        assert body["summary"]["compliant"] == 1
        [item] = body["items"]
        assert item["contract_id"] == at_org
        assert item["folder_id"] == org
        assert item["status"] == "at_risk"


async def test_an_org_admin_reads_drift_and_a_consumer_or_another_org_gets_404(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        admin, admin_id = stack.token(f"oa-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(admin_id, "org_admin", w["org"])
        consumer, consumer_id = stack.token(f"cs-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(consumer_id, "consumer", w["t1"])
        other = await stack.folder(stack.root_id, f"o{uuid.uuid4().hex[:10]}")

        allowed = await stack.client.get(
            DRIFT, params={"folder_id": w["org"]}, headers=bearer(admin)
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["summary"]["drifted"] == 1
        # An Org admin may read the contracts' content, so it gets the pointers (CR-01).
        [drifted] = [i for i in allowed.json()["items"] if i["contract_id"] == w["a"]]
        assert drifted["failing_rules"]
        for failing in drifted["failing_rules"]:
            assert failing["pointers"] == ["/schema/0"] and failing["pointers_withheld"] is False

        refused = await stack.client.get(
            DRIFT, params={"folder_id": w["org"]}, headers=bearer(consumer)
        )
        assert refused.status_code == 404, refused.text
        unseen = await stack.client.get(DRIFT, params={"folder_id": other}, headers=bearer(w["pm"]))
        assert unseen.status_code == 404, unseen.text
        anonymous = await stack.client.get(DRIFT, params={"folder_id": w["org"]})
        assert anonymous.status_code == 401, anonymous.text


async def test_a_missing_or_malformed_folder_id_is_422_before_any_authorisation(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        pm = stack.policy_manager(stack.root_id)
        # Any authorisation call for the route permission would be a 503 here.
        with stack.flaky_authz(fail_permissions=frozenset({"governance:drift-read"})):
            missing = await stack.client.get(DRIFT, headers=bearer(pm))
            malformed = await stack.client.get(
                DRIFT, params={"folder_id": "not-a-uuid"}, headers=bearer(pm)
            )
        for response in (missing, malformed):
            assert response.status_code == 422, response.text
            assert response.json()["type"] == "urn:dc:problem:validation-error"
        bad_cursor = await stack.client.get(
            DRIFT, params={"folder_id": stack.root_id, "cursor": "nope"}, headers=bearer(pm)
        )
        assert bad_cursor.status_code == 422, bad_cursor.text
        assert drift_calls(stack) == []


async def test_drift_pages_walk_every_contract_once(make_stack: MakeStack) -> None:
    async with make_stack(impact_page_size=1) as stack:
        w = await world(stack)
        seen: list[str] = []
        totals = {"drifted": 0, "at_risk": 0, "compliant": 0}
        cursor = None
        pages = 0
        while True:
            params = {"folder_id": w["org"], **({"cursor": cursor} if cursor else {})}
            response = await stack.client.get(DRIFT, params=params, headers=bearer(w["pm"]))
            assert response.status_code == 200, response.text
            body = response.json()
            pages += 1
            seen += [item["contract_id"] for item in body["items"]]
            for key in totals:
                totals[key] += body["summary"][key]
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert pages == 3
        assert sorted(seen) == sorted([w["a"], w["b"]])
        assert totals == {"drifted": 1, "at_risk": 1, "compliant": 1}


async def test_validation_saturation_is_a_503_with_retry_after(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        link = stack.validation_link
        link.force(503, {"Retry-After": "3"}, b"{}")
        try:
            response = await stack.client.get(
                DRIFT, params={"folder_id": w["org"]}, headers=bearer(w["pm"])
            )
        finally:
            link.restore()
        assert response.status_code == 503, response.text
        assert response.headers["retry-after"] == "3"
        assert response.json()["type"] == "urn:dc:problem:validation-unavailable"
