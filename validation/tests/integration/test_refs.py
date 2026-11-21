"""Reference facts in validation runs (plan 06-07; VAL-04, VAL-05, D-153, D-157).

A cel-2 rule reads ``refs``: string-keyed tri-state maps resolved before evaluation. The Plan 07
stack runs identity and validation in one process; the helpers are small copies of the other
suites.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
YAML = "application/yaml"

SLA_RESOLVES = "!(this.element in refs.elements) || refs.elements[this.element] != 'not_found'"


def contract(**extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": "urn:datacontract:test:orders",
        "version": "1.0.0",
        "status": "draft",
        "schema": [
            {
                "name": "orders",
                "properties": [
                    {"name": "id", "logicalType": "string"},
                    {"name": "amount", "logicalType": "string"},
                ],
            }
        ],
    }
    document.update(extra)
    return document


def sla(element: str) -> dict[str, Any]:
    return {
        "slaProperties": [{"property": "freshness", "value": 1, "unit": "d", "element": element}]
    }


def rule(target: str, condition: str, fixtures: dict[str, Any]) -> str:
    return json.dumps(
        {
            "target": target,
            "condition": condition,
            "severity": "error",
            "message": "A reference must resolve.",
            "fixtures": fixtures,
        }
    )


SLA_FIXTURES = {
    "pass": [
        {
            "document": contract(**sla("orders.amount")),
            "refs": {"elements": {"orders.amount": "found"}},
        }
    ],
    "fail": [
        {
            "document": contract(**sla("orders.nope")),
            "refs": {"elements": {"orders.nope": "not_found"}},
        }
    ],
}


def as_yaml(document: dict[str, Any]) -> bytes:
    return yaml.safe_dump(document, sort_keys=False).encode()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def setup(stack: Any) -> tuple[Any, str, str]:
    chain = await stack.chain()
    pam, pam_id = stack.token("pam")
    stack.grant_sql(pam_id, "policy_manager", chain.org)
    ted, ted_id = stack.token("ed")
    stack.grant_sql(ted_id, "team_editor", chain.team)
    return chain, pam, ted


async def publish_rule(stack: Any, folder: str, token: str, key: str, body: str) -> str:
    created = await stack.create_rule_set(folder, f"Refs {key}", token=token)
    assert created.status_code == 201, created.text
    rule_set_id: str = created.json()["id"]
    current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=token)
    put = await stack.put_rule(rule_set_id, key, body, current.headers["ETag"], token=token)
    assert put.status_code == 200, put.text
    published = await stack.publish(rule_set_id, put.headers["ETag"], token=token)
    assert published.status_code == 201, published.text
    return rule_set_id


async def on_demand(
    stack: Any, folder: str, token: str, document: dict[str, Any], *rule_sets: str
) -> dict[str, Any]:
    response = await stack.client.post(
        f"{BASE}/folders/{folder}/validate",
        content=as_yaml(document),
        headers={**auth(token), "Content-Type": YAML},
        params=[("rule_set", rule_set) for rule_set in rule_sets],
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def findings(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [f for f in body["report"]["findings"] if f["rule_key"] == key]


def run_url(folder_id: str) -> str:
    return f"/internal/v1/validation/folders/{folder_id}/runs"


def publish_payload(document: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "mode": "publish",
        "media_type": YAML,
        "document_b64": base64.b64encode(as_yaml(document)).decode(),
        "contract_id": str(uuid.uuid4()),
        "draft_id": str(uuid.uuid4()),
        "attachments": [],
        **extra,
    }


# -- tracer ---------------------------------------------------------------------------------------


async def test_an_sla_pointing_at_a_missing_column_fails_a_rule_exactly_there(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ed = await setup(stack)
        rule_set_id = await publish_rule(
            stack,
            chain.org,
            pam,
            "sla-resolves",
            rule("$.slaProperties[*]", SLA_RESOLVES, SLA_FIXTURES),
        )

        missing = await on_demand(
            stack, chain.team, ed, contract(**sla("orders.nope")), rule_set_id
        )
        present = await on_demand(
            stack, chain.team, ed, contract(**sla("orders.amount")), rule_set_id
        )

        (finding,) = findings(missing, "sla-resolves")
        assert finding["outcome"] == "fail"
        assert finding["location"]["pointer"] == "/slaProperties/0"
        assert findings(present, "sla-resolves") == []


# -- people, teams, links, caps and the stored summary --------------------------------------------

LINK = "https://dc.example/api/v1/contracts/contracts/0198aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
LINK_FIXTURES = {
    "pass": [{"document": contract(), "refs": {"contracts": {}}}],
    "fail": [
        {
            "document": contract(schema=[{"name": "o", "relationships": [{"to": LINK}]}]),
            "refs": {"contracts": {LINK: {"state": "not_found"}}},
        }
    ],
}
LINK_PUBLISHED = "!(this.to in refs.contracts) || refs.contracts[this.to].state != 'not_found'"
MEMBER_EXISTS = (
    "!(this.username in refs.principals) || refs.principals[this.username] != 'not_found'"
)
MEMBER_FIXTURES = {
    "pass": [
        {
            "document": contract(team={"members": [{"username": "a"}]}),
            "refs": {"principals": {"a": "found"}},
        }
    ],
    "fail": [
        {
            "document": contract(team={"members": [{"username": "a"}]}),
            "refs": {"principals": {"a": "not_found"}},
        }
    ],
}


async def test_a_member_nobody_knows_fails_and_a_local_user_passes(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, ed = await setup(stack)
        rule_set_id = await publish_rule(
            stack,
            chain.org,
            pam,
            "member-exists",
            rule("$.team.members[*]", MEMBER_EXISTS, MEMBER_FIXTURES),
        )
        known = await stack.identity.post(
            "/api/v1/identity/users",
            json={
                "username": "alice",
                "password": "a user password of 30 chars!!",
                "display_name": "Alice",
            },
            headers=auth(stack.admin_token),
        )
        assert known.status_code == 201, known.text
        document = contract(team={"members": [{"username": "alice"}, {"username": "ghost"}]})

        body = await on_demand(stack, chain.team, ed, document, rule_set_id)

        (finding,) = findings(body, "member-exists")
        assert finding["location"]["pointer"] == "/team/members/1"


async def test_publish_runs_use_the_contract_link_facts_sent_with_the_request(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ed = await setup(stack)
        rule_set_id = await publish_rule(
            stack,
            chain.org,
            pam,
            "link-live",
            rule("$.schema[*].relationships[*]", LINK_PUBLISHED, LINK_FIXTURES),
        )
        document = contract(schema=[{"name": "o", "relationships": [{"to": LINK}]}])
        base = publish_payload(
            document, attachments=[{"rule_set_id": rule_set_id, "version": "latest"}]
        )

        gone = await stack.client.post(
            run_url(chain.team),
            json={**base, "ref_facts": {"contracts": {LINK: {"state": "not_found"}}}},
            headers=auth(ed),
        )
        live = await stack.client.post(
            run_url(chain.team),
            json={**base, "ref_facts": {"contracts": {LINK: {"state": "published"}}}},
            headers=auth(ed),
        )
        unknown = await stack.client.post(run_url(chain.team), json=base, headers=auth(ed))

        assert gone.status_code == 200, gone.text
        assert len(findings(gone.json(), "link-live")) == 1
        assert findings(live.json(), "link-live") == []
        assert findings(unknown.json(), "link-live") == []  # unverifiable never fails a rule


async def test_ref_facts_over_the_cap_or_with_an_unknown_key_are_a_422(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, _pam, ed = await setup(stack)
        base = publish_payload(contract())
        many = {f"https://x/{i}": {"state": "published"} for i in range(201)}

        for bad in ({"contracts": many}, {"contracts": {}, "other": 1}):
            response = await stack.client.post(
                run_url(chain.team), json={**base, "ref_facts": bad}, headers=auth(ed)
            )
            assert response.status_code == 422, response.text
            assert response.json()["type"] == "urn:dc:problem:validation-error"
            assert "https://x/" not in response.text


async def test_the_stored_publish_run_has_counts_only(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, _pam, ed = await setup(stack)
        document = contract(
            team={"name": "mystery-team", "members": [{"username": "ghost-user"}]},
            **sla("orders.nope"),
        )

        response = await stack.client.post(
            run_url(chain.team), json=publish_payload(document), headers=auth(ed)
        )

        assert response.status_code == 200, response.text
        summary = response.json()["report"]["ref_facts"]
        assert summary["elements"] == {"found": 0, "not_found": 1, "unverifiable": 0}
        assert summary["principals"]["not_found"] == 1
        assert summary["teams"]["not_found"] == 1
        (row,) = stack.validation_sql("SELECT report FROM validation_run")
        stored = json.dumps(row["report"])
        assert row["report"]["ref_facts"] == summary
        for name in ("orders.nope", "ghost-user", "mystery-team"):
            assert name not in stored
