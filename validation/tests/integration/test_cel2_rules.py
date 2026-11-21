"""cel-2 rule sets through the validation service (plan 06-05; VAL-04, VAL-05, D-139, D-152).

A Policy Manager authors, checks and publishes a rule set that reads ``changes`` and ``previous``;
new drafts compile under cel-2, fixtures with a baseline get their changes from the
``dc_odcs.diff`` classifier, and a run evaluates cel-1 and cel-2 versions side by side. Until a
baseline exists (Plans 06 and 07) a run binds ``previous = null`` and ``changes = []``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
YAML = "application/yaml"
NO_BREAKING = "changes.all(c, c['class'] != 'breaking')"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def contract(*names: str) -> dict[str, Any]:
    return {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": "urn:datacontract:test:orders",
        "version": "1.0.0",
        "status": "draft",
        "schema": [
            {
                "name": "orders",
                "properties": [{"name": n, "logicalType": "string"} for n in names],
            }
        ],
    }


def cel2_rule(condition: str, fixtures: dict[str, Any]) -> str:
    return json.dumps(
        {
            "target": "$",
            "condition": condition,
            "severity": "error",
            "message": "A published contract must not break its consumers.",
            "fixtures": fixtures,
        }
    )


NO_BREAKING_FIXTURES = {
    "pass": [{"document": table("id", "name")}],
    "fail": [{"document": table("id"), "previous": table("id", "name")}],
}


async def setup(stack: Any) -> tuple[Any, str]:
    chain = await stack.chain()
    pam, pam_id = stack.token("pam")
    stack.grant_sql(pam_id, "policy_manager", chain.org)
    return chain, pam


async def draft_with_rule(stack: Any, folder: str, token: str, body: str) -> tuple[str, str]:
    created = await stack.create_rule_set(folder, "Compatibility", token=token)
    assert created.status_code == 201, created.text
    rule_set_id: str = created.json()["id"]
    current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=token)
    put = await stack.put_rule(
        rule_set_id, "no-breaking", body, current.headers["ETag"], token=token
    )
    assert put.status_code == 200, put.text
    return rule_set_id, put.headers["ETag"]


async def test_a_policy_manager_publishes_a_cel2_rule_and_an_on_demand_run_evaluates_it(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam = await setup(stack)
        rule_set_id, etag = await draft_with_rule(
            stack, chain.org, pam, cel2_rule(NO_BREAKING, NO_BREAKING_FIXTURES)
        )

        checked = await stack.check(rule_set_id, token=pam)
        assert checked.status_code == 200, checked.text
        assert checked.json()["check"]["ok"] is True

        published = await stack.publish(rule_set_id, etag, token=pam)
        assert published.status_code == 201, published.text
        assert published.json()["dsl_version"] == "cel-2"
        stored = stack.validation_sql(
            "SELECT dsl_version FROM rule_set_version WHERE rule_set_id = %s", rule_set_id
        )
        assert [row["dsl_version"] for row in stored] == ["cel-2"]

        ted, ted_id = stack.token("ed")
        stack.grant_sql(ted_id, "team_editor", chain.team)
        response = await stack.client.post(
            f"{BASE}/folders/{chain.team}/validate",
            content=yaml.safe_dump(contract("id", "name"), sort_keys=False).encode(),
            headers={"Authorization": f"Bearer {ted}", "Content-Type": YAML},
            params=[("rule_set", rule_set_id)],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dsl_version"] == "cel-2"
        found = [f for f in body["report"]["findings"] if f["rule_key"] == "no-breaking"]
        assert found == []  # changes is [] without a baseline, so the rule passes
        assert body["report"]["summary"]["publishable"] is True


async def test_an_unguarded_previous_rule_is_refused_at_publish_with_an_error_not_a_pass(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam = await setup(stack)
        fixtures = {
            "pass": [{"document": table("id")}],
            "fail": [{"document": table("id", "id")}],
        }
        rule_set_id, etag = await draft_with_rule(
            stack, chain.org, pam, cel2_rule("has(previous.version)", fixtures)
        )

        refused = await stack.publish(rule_set_id, etag, token=pam)

        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:ruleset-fixtures-failed"
        problems = refused.json()["check"]["rules"][0]["problems"]
        assert {p["code"] for p in problems} == {"fixture_misbehaved"}
        assert all(p["detail"].endswith("evaluated to error") for p in problems)
        versions = stack.validation_sql(
            "SELECT count(*) AS n FROM rule_set_version WHERE rule_set_id = %s", rule_set_id
        )
        assert versions[0]["n"] == 0


async def test_a_run_evaluates_a_cel1_seed_and_a_cel2_set_side_by_side(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, pam = await setup(stack)
        rule_set_id, etag = await draft_with_rule(
            stack, chain.org, pam, cel2_rule(NO_BREAKING, NO_BREAKING_FIXTURES)
        )
        assert (await stack.publish(rule_set_id, etag, token=pam)).status_code == 201
        ted, ted_id = stack.token("ed")
        stack.grant_sql(ted_id, "team_editor", chain.team)
        seeded = stack.validation_sql(
            "SELECT dsl_version FROM rule_set_version v JOIN rule_set s ON s.id = v.rule_set_id "
            "WHERE s.name = 'odcs-structural'"
        )
        assert [row["dsl_version"] for row in seeded] == ["cel-1"]

        response = await stack.client.post(
            f"{BASE}/folders/{chain.team}/validate",
            content=yaml.safe_dump(contract("id", "name"), sort_keys=False).encode(),
            headers={"Authorization": f"Bearer {ted}", "Content-Type": YAML},
            params=[("rule_set", rule_set_id)],
        )

        assert response.status_code == 200, response.text
        pins = {p["name"]: p for p in response.json()["pins"]}
        assert {"odcs-structural", "Compatibility"} <= set(pins)
        assert response.json()["report"]["summary"]["publishable"] is True
