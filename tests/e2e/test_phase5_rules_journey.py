"""The Phase 5 goal, end to end through the gateway (ROADMAP Phase 5 success criteria).

"Policy Managers author versioned, declarative rule sets and make them mandatory down the
hierarchy. No contract can be published while it fails an enforced mandatory rule or an attached
error-severity rule."

One journey with real local users on the real stack: a Global Policy Manager (a Policy Manager at
ROOT) publishes a rule set at ROOT, makes it mandatory and later enforces it at once; a team
editor publishes while the set only warns, is blocked once it is enforced (422
``contract-validation-failed`` naming rule, rule-set version, folder and JSON Pointer), fixes the
contract and publishes. The editor then attaches a second ROOT rule set with an error rule, is
blocked by it and publishes after the fix. A reader validates an uploaded document on demand and a
Policy Manager of the organization is refused the stored-draft validate route. Every governance
write is audited. Everything goes through https://traefik; only the audit read uses the internal
audit service.

Stack hygiene (T-5-54): the ROOT rule sets are shared by every journey on this stack and published
rule sets are immutable, so every condition keys on this run's own marker (``FORBIDDEN-<marker>``,
``tmp_<marker>``) and can fail no other journey's document, and the mandatory flag is cleared in a
``finally`` block.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

FOLDERS = "/api/v1/identity/folders"
CONTRACTS = "/api/v1/contracts/contracts"
VALIDATION = "/api/v1/validation"
YAML = "application/yaml"
JSON = "application/json"
Headers = dict[str, str]


def _bearer(token: str) -> Headers:
    return {"Authorization": f"Bearer {token}"}


def _document(odcs_id: str, version: str, *property_lines: str) -> bytes:
    """A valid ODCS contract with one table; ``property_lines`` are extra YAML keys of its first
    property (indented as given by the caller) or whole extra properties."""
    first = "\n".join(property_lines) if property_lines else ""
    text = f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: Phase 5 Orders
version: "{version}"
status: active
domain: governance
description:
  purpose: Phase 5 journey contract.
schema:
  - name: orders
    physicalType: table
    properties:
{first}
"""
    return text.encode("utf-8")


def _prop(name: str, description: str | None = None) -> str:
    lines = [f"      - name: {name}", "        logicalType: string"]
    if description is not None:
        lines.append(f'        description: "{description}"')
    return "\n".join(lines)


def _rule(
    key: str, condition: str, message: str, pass_document: Any, fail_document: Any
) -> dict[str, Any]:
    return {
        "key": key,
        "target": "$.schema[*].properties[*]",
        "condition": condition,
        "severity": "error",
        "message": message,
        "fixtures": {
            "pass": [{"document": pass_document}],
            "fail": [{"document": fail_document}],
        },
    }


def _table(*properties: dict[str, Any]) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": list(properties)}]}


async def _folder(gateway: httpx.AsyncClient, headers: Headers, parent_id: str, slug: str) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Phase 5 {slug}"},
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


async def _publish_rule_set(
    gateway: httpx.AsyncClient,
    headers: Headers,
    folder_id: str,
    name: str,
    rules: list[dict[str, Any]],
) -> str:
    """Create a rule set, save its draft and publish version 1; returns the rule set id."""
    created = await gateway.post(
        f"{VALIDATION}/folders/{folder_id}/rule-sets", json={"name": name}, headers=headers
    )
    assert created.status_code == 201, created.text
    rule_set_id = str(created.json()["id"])
    saved = await gateway.put(
        f"{VALIDATION}/rule-sets/{rule_set_id}/draft",
        content=json.dumps({"description": f"{name} (e2e)", "rules": rules}),
        headers={**headers, "Content-Type": JSON, "If-Match": created.headers["etag"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["check"]["ok"] is True, saved.text
    published = await gateway.post(
        f"{VALIDATION}/rule-sets/{rule_set_id}/publish",
        headers={**headers, "If-Match": saved.headers["etag"]},
    )
    assert published.status_code == 201, published.text
    assert published.json()["version_no"] == 1, published.text
    return rule_set_id


async def _set_mandatory(
    gateway: httpx.AsyncClient, headers: Headers, rule_set_id: str, mandatory: bool
) -> httpx.Response:
    return await gateway.put(
        f"{VALIDATION}/rule-sets/{rule_set_id}/mandatory",
        json={"mandatory": mandatory},
        headers=headers,
    )


async def _publish(
    gateway: httpx.AsyncClient, headers: Headers, contract_id: str, draft: dict[str, Any]
) -> httpx.Response:
    return await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts/{draft['id']}/publish",
        headers={**headers, "If-Match": draft["etag"]},
    )


async def _new_draft(
    gateway: httpx.AsyncClient, headers: Headers, contract_id: str, from_version: str, expect: str
) -> dict[str, Any]:
    started = await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts", json={"from_version": from_version}, headers=headers
    )
    assert started.status_code == 201, started.text
    draft: dict[str, Any] = started.json()["draft"]
    assert draft["declared_version"] == expect, draft
    return draft


async def _save(
    gateway: httpx.AsyncClient,
    headers: Headers,
    contract_id: str,
    draft: dict[str, Any],
    content: bytes,
) -> httpx.Response:
    return await gateway.put(
        f"{CONTRACTS}/{contract_id}/drafts/{draft['id']}",
        content=content,
        headers={**headers, "Content-Type": YAML, "If-Match": draft["etag"]},
    )


def _findings(report: dict[str, Any], rule_key: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = [f for f in report["findings"] if f["rule_key"] == rule_key]
    return found


def _blocked(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json"), response.text
    body: dict[str, Any] = response.json()
    assert body["type"] == "urn:dc:problem:contract-validation-failed", body
    assert body["run_id"], body
    report: dict[str, Any] = body["report"]
    assert report["summary"]["publishable"] is False, report
    return report


async def _metadata(
    gateway: httpx.AsyncClient, headers: Headers, contract_id: str, version: str
) -> dict[str, Any]:
    response = await gateway.get(
        f"{CONTRACTS}/{contract_id}/versions/{version}/metadata", headers=headers
    )
    assert response.status_code == 200, response.text
    metadata: dict[str, Any] = response.json()
    return metadata


def _pin(metadata: dict[str, Any], name: str) -> dict[str, Any]:
    pins = [p for p in metadata["pinned_rule_sets"] if p["name"] == name]
    assert len(pins) == 1, (name, metadata["pinned_rule_sets"])
    pin: dict[str, Any] = pins[0]
    return pin


async def _audit_events(
    audit: httpx.AsyncClient, headers: Headers, aggregate_id: str, action: str
) -> list[dict[str, Any]] | None:
    response = await audit.get(
        "/internal/v1/events", params={"aggregate_id": aggregate_id, "limit": 200}, headers=headers
    )
    response.raise_for_status()
    events: list[dict[str, Any]] = [e for e in response.json() if e["action"] == action]
    return events or None


async def test_the_phase_goal_mandatory_and_attached_rules_gate_publishing_through_the_gateway(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    admin_token: str,
    users: Any,
    wait_for: Callable[..., Any],
) -> None:
    admin = _bearer(admin_token)
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    root_id = str(root["id"])
    run = uuid.uuid4().hex[:10]
    marker = uuid.uuid4().hex[:12]  # M: no document of another journey or run carries it

    org = await _folder(gateway, admin, root_id, f"p5j-{run}")
    project = await _folder(gateway, admin, org, "data")
    team = await _folder(gateway, admin, project, "team")

    gpm_user = await users.create("p5gpm")
    editor = await users.create("p5ed")
    reader = await users.create("p5rd")
    org_pm = await users.create("p5opm")
    await _grant(gateway, admin, root_id, gpm_user.principal_id, "policy_manager")
    await _grant(gateway, admin, team, editor.principal_id, "team_editor")
    await _grant(gateway, admin, team, reader.principal_id, "team_reader")
    await _grant(gateway, admin, org, org_pm.principal_id, "policy_manager")
    gpm = _bearer(await users.token(gpm_user))
    ed = _bearer(await users.token(editor))
    rd = _bearer(await users.token(reader))
    opm = _bearer(await users.token(org_pm))

    forbidden = f"FORBIDDEN-{marker}"
    tmp_prefix = f"tmp_{marker}"
    naming_name = f"e2e-{marker}-naming"
    extras_name = f"e2e-{marker}-extras"
    naming_id: str | None = None
    mandatory_on = False
    try:
        # --- Global Policy Manager: publish a ROOT rule set and make it mandatory -------------
        naming_id = await _publish_rule_set(
            gateway,
            gpm,
            root_id,
            naming_name,
            [
                _rule(
                    "no-marker-props",
                    f'!has(this.description) || !this.description.startsWith("{forbidden}")',
                    "A property description must not start with the forbidden marker.",
                    _table({"name": "a"}, {"name": "b", "description": "fine"}),
                    _table({"name": "a", "description": f"{forbidden} nope"}),
                )
            ],
        )
        mandatory_on = True
        flagged = await _set_mandatory(gateway, gpm, naming_id, True)
        assert flagged.status_code == 200, flagged.text
        assert flagged.json()["mandatory"] is True, flagged.text

        # --- editor: a violation inside the warning window is reported, never blocking --------
        odcs_id = f"urn:datacontract:phase5:{uuid.uuid4()}"
        first = _document(odcs_id, "1.0.0", _prop("order_id", f"{forbidden} first draft"))
        created = await gateway.post(
            f"/api/v1/contracts/folders/{team}/contracts",
            content=first,
            headers={**ed, "Content-Type": YAML},
        )
        assert created.status_code == 201, created.text
        contract_id = str(created.json()["contract"]["id"])
        draft = created.json()["draft"]
        advisory = created.json()["validation"]
        assert advisory["status"] == "completed", advisory
        warned = _findings(advisory, "no-marker-props")
        assert len(warned) == 1, advisory
        assert warned[0]["mode"] == "warn" and warned[0]["blocking"] is False, warned
        assert warned[0]["rule_set"]["name"] == naming_name, warned
        assert advisory["summary"]["publishable"] is True, advisory

        published = await _publish(gateway, ed, contract_id, draft)
        assert published.status_code == 201, published.text  # a warning does not block
        assert published.json()["semver"] == "1.0.0"
        meta_1 = await _metadata(gateway, ed, contract_id, "1.0.0")
        assert meta_1["validation_run_id"], meta_1
        warn_pin = _pin(meta_1, naming_name)
        assert warn_pin["version"] == 1 and warn_pin["mode"] == "warn", warn_pin
        assert warn_pin["source"] == "mandatory", warn_pin

        # --- the Policy Manager moves the warn date (any date, audited), then enforces now ---
        postponed_to = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)
        rescheduled = await gateway.put(
            f"{VALIDATION}/rule-sets/{naming_id}/versions/1/enforce-at",
            json={"enforce_at": postponed_to.isoformat().replace("+00:00", "Z")},
            headers=gpm,
        )
        assert rescheduled.status_code == 200, rescheduled.text
        assert datetime.fromisoformat(rescheduled.json()["enforce_at"]) == postponed_to

        # --- the Policy Manager enforces version 1 now; the same kind of publish is blocked ---
        enforced = await gateway.post(
            f"{VALIDATION}/rule-sets/{naming_id}/versions/1/enforce-now", headers=gpm
        )
        assert enforced.status_code == 200, enforced.text

        draft_2 = await _new_draft(gateway, ed, contract_id, "1.0.0", "1.1.0")
        saved_2 = await _save(
            gateway,
            ed,
            contract_id,
            draft_2,
            _document(odcs_id, "1.1.0", _prop("order_id", f"{forbidden} still here")),
        )
        assert saved_2.status_code == 200, saved_2.text
        draft_2 = saved_2.json()["draft"]
        blocking_now = _findings(saved_2.json()["validation"], "no-marker-props")
        assert blocking_now[0]["mode"] == "enforce" and blocking_now[0]["blocking"] is True
        refused = await _publish(gateway, ed, contract_id, draft_2)
        report = _blocked(refused)  # contract-validation-failed
        finding = _findings(report, "no-marker-props")[0]
        assert finding["blocking"] is True, finding
        assert finding["rule_set"]["name"] == naming_name, finding
        assert finding["rule_set"]["version"] == 1, finding
        assert finding["rule_set"]["folder_kind"] == "root", finding
        assert finding["rule_set"]["folder_id"] == root_id, finding
        assert finding["location"]["pointer"] == "/schema/0/properties/0", finding
        assert finding["source"] == "mandatory" and finding["mode"] == "enforce", finding

        # --- the editor fixes the contract; publish succeeds with the pinned versions ---------
        fixed_2 = await _save(
            gateway, ed, contract_id, draft_2, _document(odcs_id, "1.1.0", _prop("order_id"))
        )
        assert fixed_2.status_code == 200, fixed_2.text
        assert fixed_2.json()["validation"]["summary"]["publishable"] is True
        published_2 = await _publish(gateway, ed, contract_id, fixed_2.json()["draft"])
        assert published_2.status_code == 201, published_2.text
        assert published_2.json()["semver"] == "1.1.0"
        meta_2 = await _metadata(gateway, ed, contract_id, "1.1.0")
        enforce_pin = _pin(meta_2, naming_name)
        assert enforce_pin["version"] == 1 and enforce_pin["mode"] == "enforce", enforce_pin
        platform_pin = _pin(meta_2, "odcs-structural")
        assert platform_pin["version"] == 1 and platform_pin["scope"] == "platform", platform_pin
        assert platform_pin["mode"] == "enforce", platform_pin

        # --- the editor attaches a ROOT rule set with an error rule ---------------------------
        extras_id = await _publish_rule_set(
            gateway,
            gpm,
            root_id,
            extras_name,
            [
                _rule(
                    f"no-tmp-{marker}",
                    f'!has(this.name) || !this.name.startsWith("{tmp_prefix}")',
                    "A property name must not start with the temporary marker.",
                    _table({"name": "a"}, {"name": "b"}),
                    _table({"name": f"{tmp_prefix}_x"}),
                )
            ],
        )
        attached = await gateway.put(f"{CONTRACTS}/{contract_id}/rule-sets/{extras_id}", headers=ed)
        assert attached.status_code == 201, attached.text
        assert attached.json()["status"] == "created", attached.text
        listed = await gateway.get(f"{CONTRACTS}/{contract_id}/rule-sets", headers=ed)
        assert listed.status_code == 200, listed.text
        assert [i["rule_set_id"] for i in listed.json()["items"]] == [extras_id]

        draft_3 = await _new_draft(gateway, ed, contract_id, "1.1.0", "1.2.0")
        saved_3 = await _save(
            gateway,
            ed,
            contract_id,
            draft_3,
            _document(odcs_id, "1.2.0", _prop(f"{tmp_prefix}_x")),
        )
        assert saved_3.status_code == 200, saved_3.text
        draft_3 = saved_3.json()["draft"]
        report_3 = _blocked(await _publish(gateway, ed, contract_id, draft_3))
        explicit = [f for f in report_3["findings"] if f["source"] == "explicit"]
        assert len(explicit) == 1, report_3
        assert explicit[0]["blocking"] is True, explicit
        assert explicit[0]["rule_set"]["name"] == extras_name, explicit
        assert explicit[0]["location"]["pointer"] == "/schema/0/properties/0", explicit

        fixed_3 = await _save(
            gateway, ed, contract_id, draft_3, _document(odcs_id, "1.2.0", _prop("order_id"))
        )
        assert fixed_3.status_code == 200, fixed_3.text
        published_3 = await _publish(gateway, ed, contract_id, fixed_3.json()["draft"])
        assert published_3.status_code == 201, published_3.text
        meta_3 = await _metadata(gateway, ed, contract_id, "1.2.0")
        extras_pin = _pin(meta_3, extras_name)
        assert extras_pin["source"] == "explicit" and extras_pin["version"] == 1, extras_pin
        assert _pin(meta_3, naming_name)["mode"] == "enforce"

        # --- on-demand validation: an upload (reader) and the stored draft (Policy Manager) ---
        upload = _document(odcs_id, "9.9.9", _prop("order_id", f"{forbidden} upload"))
        validated = await gateway.post(
            f"{VALIDATION}/folders/{team}/validate",
            content=upload,
            headers={**rd, "Content-Type": YAML},
        )
        assert validated.status_code == 200, validated.text
        upload_report = validated.json()["report"]
        assert upload_report["summary"]["publishable"] is False, upload_report
        assert _findings(upload_report, "no-marker-props")[0]["blocking"] is True
        assert validated.json()["pins"], validated.text
        versions = await gateway.get(f"{CONTRACTS}/{contract_id}/versions", headers=ed)
        assert versions.status_code == 200, versions.text
        assert "9.9.9" not in {v["semver"] for v in versions.json()["items"]}  # nothing stored

        draft_4 = await _new_draft(gateway, ed, contract_id, "1.2.0", "1.3.0")
        denied = await gateway.post(
            f"{CONTRACTS}/{contract_id}/drafts/{draft_4['id']}/validate", headers=opm
        )
        assert denied.status_code in (403, 404), denied.text
        allowed = await gateway.post(
            f"{CONTRACTS}/{contract_id}/drafts/{draft_4['id']}/validate", headers=ed
        )
        assert allowed.status_code == 200, allowed.text

        # --- audit: every governance write left a record -------------------------------------
        audit = internal("audit")
        for action in (
            "ruleset.version_published",
            "ruleset.mandatory_set",
            "ruleset.enforced_early",
            "ruleset.enforcement_rescheduled",
        ):
            await wait_for(
                lambda action=action: _audit_events(audit, admin, naming_id, action),
                30.0,
                description=f"the {action} audit event for {naming_id}",
            )
        attached_events = await wait_for(
            lambda: _audit_events(audit, admin, contract_id, "contract.ruleset_attached"),
            30.0,
            description="the contract.ruleset_attached audit event",
        )
        assert attached_events[0]["after"]["rule_set_id"] == extras_id, attached_events
        version_events = await wait_for(
            lambda: _audit_events(audit, admin, contract_id, "contract.version_published"),
            30.0,
            description="the contract.version_published audit events",
        )
        assert version_events, version_events
        for event in version_events:
            assert event["after"]["validation_run_id"], event
            assert event["after"]["pinned_rule_sets"], event
    finally:
        # Never leave a mandatory ROOT set behind (T-5-54), even when an assertion failed.
        if mandatory_on and naming_id is not None:
            cleared = await _set_mandatory(gateway, gpm, naming_id, False)
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["mandatory"] is False, cleared.text

    assert naming_id is not None
    cleared_events = await wait_for(
        lambda: _audit_events(internal("audit"), admin, naming_id, "ruleset.mandatory_cleared"),
        30.0,
        description=f"the ruleset.mandatory_cleared audit event for {naming_id}",
    )
    assert cleared_events[0]["after"], cleared_events
