"""Rule impact before and after enforcement, end to end through the gateway (Phase 6, SC5).

"Policy Managers can see a rule's impact before it enforces and after it has."

One journey with real local users on the real stack. A Policy Manager of a per-run Organization
drafts a rule set there and dry-runs the draft over the published contracts in scope (POST
``/api/v1/contracts/governance/rule-set-dry-runs``, D-159): contract A would be blocked, contract B
passes. The Policy Manager publishes version 1, makes the set mandatory and enforces it early; the
compliance-drift report (GET ``/api/v1/contracts/governance/drift?folder_id=``, D-160) then lists A
as drifted, with the rule-set versions A was published against next to the version now enforced.
At no point does any response to the Policy Manager carry A's document content: no property name
and no description text (D-36, D-158).

Every call goes through https://traefik. Stack hygiene (T-6-39): the rule set lives at the per-run
Organization, its condition keys on this run's own marker, and the mandatory flag is cleared in a
``finally`` block, so no other journey's document can be affected.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

FOLDERS = "/api/v1/identity/folders"
CONTRACTS = "/api/v1/contracts"
VALIDATION = "/api/v1/validation"
DRY_RUNS = f"{CONTRACTS}/governance/rule-set-dry-runs"
DRIFT = f"{CONTRACTS}/governance/drift"
YAML = "application/yaml"
JSON = "application/json"
Headers = dict[str, str]


def _bearer(token: str) -> Headers:
    return {"Authorization": f"Bearer {token}"}


def _document(odcs_id: str, name: str, column: str, description: str | None = None) -> bytes:
    described = f'\n        description: "{description}"' if description is not None else ""
    text = f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: {name}
version: "1.0.0"
status: active
domain: impact
description:
  purpose: Phase 6 rule impact journey.
schema:
  - name: events
    physicalType: table
    properties:
      - name: {column}
        logicalType: string{described}
"""
    return text.encode("utf-8")


def _table(*properties: dict[str, Any]) -> dict[str, Any]:
    return {"schema": [{"name": "events", "properties": list(properties)}]}


async def _folder(gateway: httpx.AsyncClient, headers: Headers, parent_id: str, slug: str) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Phase 6 {slug}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _grant(
    gateway: httpx.AsyncClient, headers: Headers, folder_id: str, principal_id: str, role: str
) -> None:
    response = await gateway.post(
        f"{FOLDERS}/{folder_id}/grants",
        json={"principal_type": "user", "principal_id": principal_id, "role": role},
        headers=headers,
    )
    assert response.status_code == 201, response.text


async def _publish_new(
    gateway: httpx.AsyncClient, headers: Headers, team_id: str, content: bytes
) -> str:
    created = await gateway.post(
        f"{CONTRACTS}/folders/{team_id}/contracts",
        content=content,
        headers={**headers, "Content-Type": YAML},
    )
    assert created.status_code == 201, created.text
    contract_id = str(created.json()["contract"]["id"])
    draft = created.json()["draft"]
    published = await gateway.post(
        f"{CONTRACTS}/contracts/{contract_id}/drafts/{draft['id']}/publish",
        headers={**headers, "If-Match": draft["etag"]},
    )
    assert published.status_code == 201, published.text
    assert published.json()["semver"] == "1.0.0"
    return contract_id


async def test_a_policy_manager_sees_rule_impact_before_and_after_enforcement(
    gateway: httpx.AsyncClient,
    admin_token: str,
    users: Any,
) -> None:
    admin = _bearer(admin_token)
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    run = uuid.uuid4().hex[:10]
    marker = uuid.uuid4().hex[:12]  # no document of another journey or run carries it

    org = await _folder(gateway, admin, str(root["id"]), f"p6i-{run}")
    project = await _folder(gateway, admin, org, "data")
    team_a = await _folder(gateway, admin, project, "alpha")
    team_b = await _folder(gateway, admin, project, "beta")

    pm_user = await users.create("p6pm")
    editor = await users.create("p6ied")
    await _grant(gateway, admin, org, pm_user.principal_id, "policy_manager")
    await _grant(gateway, admin, team_a, editor.principal_id, "team_editor")
    await _grant(gateway, admin, team_b, editor.principal_id, "team_editor")
    pm = _bearer(await users.token(pm_user))
    ed = _bearer(await users.token(editor))

    # --- two published contracts: A breaks the marker rule, B does not -----------------------
    forbidden = f"FORBIDDEN-{marker}"
    secret_column = f"col_{uuid.uuid4().hex[:12]}"
    secret_text = f"secret-{uuid.uuid4().hex}"
    contract_a = await _publish_new(
        gateway,
        ed,
        team_a,
        _document(
            f"urn:datacontract:phase6i:{uuid.uuid4()}",
            f"Impact A {run}",
            secret_column,
            f"{forbidden} {secret_text}",
        ),
    )
    contract_b = await _publish_new(
        gateway,
        ed,
        team_b,
        _document(f"urn:datacontract:phase6i:{uuid.uuid4()}", f"Impact B {run}", "event_id"),
    )

    pm_bodies: list[str] = []  # every response body the Policy Manager receives

    def seen(response: httpx.Response) -> httpx.Response:
        pm_bodies.append(response.text)
        return response

    rule_key = f"no-marker-{marker}"
    rule = {
        "key": rule_key,
        "target": "$.schema[*].properties[*]",
        "condition": f'!has(this.description) || !this.description.startsWith("{forbidden}")',
        "severity": "error",
        "message": "A property description must not start with the forbidden marker.",
        "fixtures": {
            "pass": [{"document": _table({"name": "a"}, {"name": "b", "description": "fine"})}],
            "fail": [{"document": _table({"name": "a", "description": f"{forbidden} no"})}],
        },
    }
    rule_set_id: str | None = None
    mandatory_on = False
    try:
        # --- the Policy Manager drafts a rule set at the Organization ------------------------
        created = seen(
            await gateway.post(
                f"{VALIDATION}/folders/{org}/rule-sets",
                json={"name": f"e2e-{marker}-impact"},
                headers=pm,
            )
        )
        assert created.status_code == 201, created.text
        rule_set_id = str(created.json()["id"])
        saved = seen(
            await gateway.put(
                f"{VALIDATION}/rule-sets/{rule_set_id}/draft",
                content=json.dumps({"description": "Phase 6 impact (e2e)", "rules": [rule]}),
                headers={**pm, "Content-Type": JSON, "If-Match": created.headers["etag"]},
            )
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["check"]["ok"] is True, saved.text

        # --- before: a dry-run of the draft shows A would be blocked and B passes ------------
        dry = seen(
            await gateway.post(
                DRY_RUNS, json={"rule_set_id": rule_set_id, "version": "draft"}, headers=pm
            )
        )
        assert dry.status_code == 200, dry.text
        dry_body = dry.json()
        assert dry_body["rule_set_id"] == rule_set_id and dry_body["version"] == "draft"
        assert dry_body["scope_folder_id"] == org, dry_body
        assert dry_body["next_cursor"] is None, dry_body
        assert dry_body["total_in_scope"] == 2, dry_body
        assert dry_body["summary"] == {"would_fail": 1, "would_error": 0, "pass": 1, "hidden": 0}
        by_id = {item["contract_id"]: item for item in dry_body["items"]}
        assert set(by_id) == {contract_a, contract_b}, dry_body
        a_dry, b_dry = by_id[contract_a], by_id[contract_b]
        assert a_dry["would_block"] is True and a_dry["status"] == "evaluated", a_dry
        assert a_dry["latest_semver"] == "1.0.0" and a_dry["name"] == f"Impact A {run}", a_dry
        assert a_dry["counts"]["fail"] == 1, a_dry
        (failing,) = a_dry["failing_rules"]
        assert failing["rule_key"] == rule_key and failing["blocking"] is True, failing
        # CR-01: the Policy Manager holds no contract:read, so where the rule matched is withheld.
        assert failing["pointers"] == [] and failing["pointers_withheld"] is True, failing
        assert b_dry["would_block"] is False and b_dry["failing_rules"] == [], b_dry

        # --- the Policy Manager publishes v1, makes it mandatory and enforces it early --------
        published = seen(
            await gateway.post(
                f"{VALIDATION}/rule-sets/{rule_set_id}/publish",
                headers={**pm, "If-Match": saved.headers["etag"]},
            )
        )
        assert published.status_code == 201, published.text
        assert published.json()["version_no"] == 1, published.text
        mandatory_on = True
        flagged = seen(
            await gateway.put(
                f"{VALIDATION}/rule-sets/{rule_set_id}/mandatory",
                json={"mandatory": True},
                headers=pm,
            )
        )
        assert flagged.status_code == 200, flagged.text
        enforced = seen(
            await gateway.post(
                f"{VALIDATION}/rule-sets/{rule_set_id}/versions/1/enforce-now", headers=pm
            )
        )
        assert enforced.status_code == 200, enforced.text

        # --- after: compliance drift lists A as drifted, B is counted as compliant -----------
        drift = seen(await gateway.get(DRIFT, params={"folder_id": org}, headers=pm))
        assert drift.status_code == 200, drift.text
        drift_body = drift.json()
        assert drift_body["folder_id"] == org and drift_body["total_in_scope"] == 2, drift_body
        assert drift_body["summary"]["drifted"] == 1, drift_body
        assert drift_body["summary"]["compliant"] == 1, drift_body
        assert drift_body["summary"]["hidden"] == 0, drift_body
        (a_drift,) = drift_body["items"]
        assert a_drift["contract_id"] == contract_a and a_drift["status"] == "drifted", a_drift
        assert rule_key in {r["rule_key"] for r in a_drift["failing_rules"]}, a_drift
        pinned_ids = {p["rule_set_id"] for p in a_drift["pinned_rule_sets"]}
        assert "odcs-structural" in {p["name"] for p in a_drift["pinned_rule_sets"]}, a_drift
        assert rule_set_id not in pinned_ids, a_drift  # published before the set existed
        current = [v for v in a_drift["current_versions"] if v["rule_set_id"] == rule_set_id]
        assert current == [
            {
                "rule_set_id": rule_set_id,
                "name": f"e2e-{marker}-impact",
                "version_no": 1,
                "mode": "enforce",
            }
        ], a_drift

        # --- no Policy Manager response ever carried A's document content --------------------
        for body in pm_bodies:
            assert secret_column not in body, body
            assert secret_text not in body, body
    finally:
        # Never leave a mandatory set behind (T-6-39), even when an assertion failed.
        if mandatory_on and rule_set_id is not None:
            cleared = await gateway.put(
                f"{VALIDATION}/rule-sets/{rule_set_id}/mandatory",
                json={"mandatory": False},
                headers=pm,
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["mandatory"] is False, cleared.text
