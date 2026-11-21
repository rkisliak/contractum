"""The Phase 6 goal, end to end through the gateway (ROADMAP Phase 6 success criteria).

"Publishing catches breaking changes and semantic inconsistencies, not just structural errors."

One journey with real local users on the real stack. A team editor publishes ``1.0.0`` and a
consumer's subscriber card (with a webhook) pins it. The editor removes a column and declares
``1.1.0``: the draft save's embedded report already says ``required_bump: major`` and publish is
refused with 422 ``contract-validation-failed`` naming the native finding ``semver-breaking-change``
and the removed path (D-146, D-147, D-148). Declared as ``2.0.0`` the same content publishes; the
card receives ``contract.version.breaking_published`` listing the removed column for its pinned
``1.0.0`` (SUB-08, D-162 .. D-164), the consumer reads the same breaking change through the
changelog (D-156) and the stored validation report of ``2.0.0`` with its compatibility section
(D-155). A pure reorder of properties (``2.0.1``) is no change at all, and a draft with a
non-unique primary key and an unknown team member gets warn-mode findings from the seeded
``odcs-consistency`` and ``odcs-references`` sets (D-150, D-151, D-153).

Every call goes through https://traefik; only the webhook sink is read on the backend network.
Stack hygiene: per-run folder slugs, user names, ODCS ids and card names; this journey sets no
mandatory flag and writes no rule set, so it cannot affect another journey.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from conftest import WebhookSink

pytestmark = pytest.mark.e2e

FOLDERS = "/api/v1/identity/folders"
CONTRACTS = "/api/v1/contracts/contracts"
CARDS = "/api/v1/contracts/cards"
YAML = "application/yaml"
SINK_URL = "http://webhook-sink:8080/hooks"
NOTICE_WAIT_S = 60.0
BREAKING = "contract.version.breaking_published"
PUBLISHED = "contract.version.published"
REMOVED_PATH = "schema.orders.properties.amount"
Headers = dict[str, str]


def _bearer(token: str) -> Headers:
    return {"Authorization": f"Bearer {token}"}


ID_PROP = """      - name: id
        logicalType: string
        primaryKey: true
        required: true
        unique: true"""
AMOUNT_PROP = """      - name: amount
        logicalType: number"""
CURRENCY_PROP = """      - name: currency
        logicalType: string"""


def _document(odcs_id: str, version: str, *properties: str, extra: str = "") -> bytes:
    """A valid ODCS contract with the ``orders`` table holding ``properties`` in that order."""
    text = f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: Phase 6 Orders
version: "{version}"
status: active
domain: semantics
description:
  purpose: Phase 6 journey contract.
schema:
  - name: orders
    physicalType: table
    properties:
{chr(10).join(properties)}
{extra}"""
    return text.encode("utf-8")


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


async def _publish(
    gateway: httpx.AsyncClient, headers: Headers, contract_id: str, draft: dict[str, Any]
) -> httpx.Response:
    return await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts/{draft['id']}/publish",
        headers={**headers, "If-Match": draft["etag"]},
    )


async def _new_draft(
    gateway: httpx.AsyncClient,
    headers: Headers,
    contract_id: str,
    from_version: str,
    bump: str,
    expect: str,
) -> dict[str, Any]:
    started = await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts",
        json={"from_version": from_version, "bump": bump},
        headers=headers,
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
) -> dict[str, Any]:
    """Save the draft; returns the response body (``draft`` and the embedded ``validation``)."""
    response = await gateway.put(
        f"{CONTRACTS}/{contract_id}/drafts/{draft['id']}",
        content=content,
        headers={**headers, "Content-Type": YAML, "If-Match": draft["etag"]},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["validation"]["status"] == "completed", body["validation"]
    return body


def _findings(report: dict[str, Any], rule_key: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = [f for f in report["findings"] if f["rule_key"] == rule_key]
    return found


def _change_codes(changes: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {(c["code"], c["class"], c["path"]) for c in changes}


def _sink_bodies(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []
    for request in requests:
        if request.get("path") != "/hooks":
            continue
        bodies.append(json.loads(base64.b64decode(request["body_b64"])))
    return bodies


async def _webhook_of(
    sink: WebhookSink, event_type: str, card_id: str, semver: str
) -> dict[str, Any] | None:
    """The webhook body the sink received for ``event_type`` about version ``semver``, if any."""
    for body in _sink_bodies(await sink.requests()):
        data = body.get("data", {})
        if (
            body.get("type") == event_type
            and data.get("card", {}).get("id") == card_id
            and data.get("version", {}).get("semver") == semver
        ):
            return body
    return None


async def test_the_phase_goal_breaking_changes_and_semantic_checks_gate_publishing(
    gateway: httpx.AsyncClient,
    admin_token: str,
    users: Any,
    webhook_sink: WebhookSink,
    wait_for: Callable[..., Any],
    ensure_webhook_hosts: Callable[..., Awaitable[None]],
) -> None:
    admin = _bearer(admin_token)
    # The allow-list is mandatory: the sink's host is listed before a card is pointed at it.
    await ensure_webhook_hosts("webhook-sink")
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    root_id = str(root["id"])
    run = uuid.uuid4().hex[:10]
    marker = uuid.uuid4().hex[:12]

    org = await _folder(gateway, admin, root_id, f"p6s-{run}")
    project = await _folder(gateway, admin, org, "data")
    team = await _folder(gateway, admin, project, "team")
    consumers = await _folder(gateway, admin, project, "consumers")

    editor = await users.create("p6ed")
    consumer = await users.create("p6co")
    await _grant(gateway, admin, team, editor.principal_id, "team_editor")
    await _grant(gateway, admin, org, consumer.principal_id, "consumer")
    ed = _bearer(await users.token(editor))
    co = _bearer(await users.token(consumer))

    # --- the editor publishes 1.0.0: orders(id PK required unique, amount, currency) ---------
    odcs_id = f"urn:datacontract:phase6:{uuid.uuid4()}"
    created = await gateway.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=_document(odcs_id, "1.0.0", ID_PROP, AMOUNT_PROP, CURRENCY_PROP),
        headers={**ed, "Content-Type": YAML},
    )
    assert created.status_code == 201, created.text
    contract_id = str(created.json()["contract"]["id"])
    first_report = created.json()["validation"]
    assert first_report["status"] == "completed", first_report
    assert first_report["compatibility"]["status"] == "no-baseline", first_report
    published_1 = await _publish(gateway, ed, contract_id, created.json()["draft"])
    assert published_1.status_code == 201, published_1.text
    assert published_1.json()["semver"] == "1.0.0"

    # --- the consumer's card, with a webhook, pins 1.0.0 -------------------------------------
    card = await gateway.post(
        f"/api/v1/contracts/folders/{consumers}/cards",
        json={
            "name": f"orders-consumers-{run}",
            "description": "Consumes the Phase 6 journey contract.",
            "contacts": [{"name": "Contact", "kind": "email", "value": f"p6-{run}@example.test"}],
            "email_enabled": False,
        },
        headers=co,
    )
    assert card.status_code == 201, card.text
    card_id = str(card.json()["id"])
    enabled = await gateway.patch(
        f"{CARDS}/{card_id}",
        json={"webhook_url": SINK_URL, "webhook_enabled": True},
        headers=co,
    )
    assert enabled.status_code == 200, enabled.text
    pinned = await gateway.post(
        f"{CARDS}/{card_id}/subscriptions",
        json={"contract_id": contract_id, "version": "1.0.0"},
        headers=co,
    )
    assert pinned.status_code == 201, pinned.text

    # --- a removed column declared as a minor: the save report already says "major" ----------
    draft = await _new_draft(gateway, ed, contract_id, "1.0.0", "minor", "1.1.0")
    saved = await _save(
        gateway, ed, contract_id, draft, _document(odcs_id, "1.1.0", ID_PROP, CURRENCY_PROP)
    )
    draft = saved["draft"]
    compatibility = saved["validation"]["compatibility"]
    assert compatibility["status"] == "compared", compatibility
    assert compatibility["baseline"]["semver"] == "1.0.0", compatibility
    assert compatibility["declared_version"] == "1.1.0", compatibility
    assert compatibility["required_bump"] == "major", compatibility
    assert compatibility["bump_ok"] is False, compatibility
    assert ("property-removed", "breaking", REMOVED_PATH) in _change_codes(
        compatibility["changes"]
    ), compatibility
    advisory = _findings(saved["validation"], "semver-breaking-change")
    assert len(advisory) == 1 and advisory[0]["blocking"] is True, saved["validation"]
    assert saved["validation"]["summary"]["publishable"] is False, saved["validation"]

    refused = await _publish(gateway, ed, contract_id, draft)
    assert refused.status_code == 422, refused.text
    assert refused.headers["content-type"].startswith("application/problem+json"), refused.text
    problem = refused.json()
    assert problem["type"] == "urn:dc:problem:contract-validation-failed", problem
    report = problem["report"]
    native = _findings(report, "semver-breaking-change")
    assert len(native) == 1, report
    assert native[0]["source"] == "native" and native[0]["blocking"] is True, native
    assert native[0]["mode"] == "enforce" and native[0]["severity"] == "error", native
    assert native[0]["location"]["pointer"] == "/version", native
    assert "2.0.0" in native[0]["message"], native  # names the smallest passing version
    assert set(native[0]["rule_set"].values()) == {None}, native
    assert report["findings"][0]["source"] == "native", report  # listed first
    assert ("property-removed", "breaking", REMOVED_PATH) in _change_codes(
        report["compatibility"]["changes"]
    ), report
    versions = await gateway.get(f"{CONTRACTS}/{contract_id}/versions", headers=ed)
    assert versions.status_code == 200, versions.text
    assert "1.1.0" not in {v["semver"] for v in versions.json()["items"]}  # nothing published

    # --- the same content declared as 2.0.0 publishes ----------------------------------------
    resaved = await _save(
        gateway, ed, contract_id, draft, _document(odcs_id, "2.0.0", ID_PROP, CURRENCY_PROP)
    )
    assert resaved["draft"]["declared_version"] == "2.0.0", resaved["draft"]
    assert resaved["validation"]["compatibility"]["bump_ok"] is True, resaved["validation"]
    assert _findings(resaved["validation"], "semver-breaking-change") == []
    published_2 = await _publish(gateway, ed, contract_id, resaved["draft"])
    assert published_2.status_code == 201, published_2.text
    assert published_2.json()["semver"] == "2.0.0"

    # --- the card pinned to 1.0.0 hears that 2.0.0 breaks it, through its webhook ------------
    breaking = await wait_for(
        lambda: _webhook_of(webhook_sink, BREAKING, card_id, "2.0.0"),
        NOTICE_WAIT_S,
        description="the breaking_published webhook for 2.0.0",
    )
    data = breaking["data"]
    assert data["contract"]["id"] == contract_id, data
    (section,) = data["pinned_versions"]
    assert section["semver"] == "1.0.0", section
    assert section["breaking_count"] == 1, section
    assert [(c["code"], c["path"]) for c in section["changes"]] == [
        ("property-removed", REMOVED_PATH)
    ], section
    assert section["diff_url"] == f"{CONTRACTS}/{contract_id}/diff?from=1.0.0&to=2.0.0", section
    await wait_for(
        lambda: _webhook_of(webhook_sink, PUBLISHED, card_id, "2.0.0"),
        NOTICE_WAIT_S,
        description="the plain published webhook for 2.0.0",
    )

    # --- the consumer reads the same breaking change and the stored report -------------------
    diff = await gateway.get(
        f"{CONTRACTS}/{contract_id}/diff", params={"from": "1.0.0", "to": "2.0.0"}, headers=co
    )
    assert diff.status_code == 200, diff.text
    changelog = diff.json()
    assert changelog["required_bump"] == "major", changelog
    assert changelog["counts"]["breaking"] == 1, changelog
    assert ("property-removed", "breaking", REMOVED_PATH) in _change_codes(changelog["changes"]), (
        changelog
    )
    assert changelog["from"]["semver"] == "1.0.0" and changelog["to"]["semver"] == "2.0.0"

    stored = await gateway.get(
        f"{CONTRACTS}/{contract_id}/versions/2.0.0/validation-report", headers=co
    )
    assert stored.status_code == 200, stored.text
    stored_body = stored.json()
    assert stored_body["semver"] == "2.0.0" and stored_body["run_id"], stored_body
    stored_compat = stored_body["report"]["compatibility"]
    assert stored_compat["baseline"]["semver"] == "1.0.0", stored_compat
    assert stored_compat["required_bump"] == "major" and stored_compat["bump_ok"] is True
    assert stored.headers["etag"] == f'"run-{stored_body["run_id"]}"', stored.headers

    # --- a pure reorder (2.0.1) is no change at all ------------------------------------------
    draft_3 = await _new_draft(gateway, ed, contract_id, "2.0.0", "patch", "2.0.1")
    saved_3 = await _save(
        gateway, ed, contract_id, draft_3, _document(odcs_id, "2.0.1", CURRENCY_PROP, ID_PROP)
    )
    assert saved_3["validation"]["compatibility"]["changes_total"] == 0, saved_3["validation"]
    assert saved_3["validation"]["compatibility"]["required_bump"] == "none"
    published_3 = await _publish(gateway, ed, contract_id, saved_3["draft"])
    assert published_3.status_code == 201, published_3.text
    reorder = await gateway.get(
        f"{CONTRACTS}/{contract_id}/diff", params={"from": "2.0.0", "to": "2.0.1"}, headers=co
    )
    assert reorder.status_code == 200, reorder.text
    assert reorder.json()["changes"] == [], reorder.json()
    assert reorder.json()["changes_total"] == 0, reorder.json()
    assert reorder.json()["required_bump"] == "none", reorder.json()

    # --- semantic checks: the seeded sets warn about a non-unique key and a ghost member ------
    ghost = f"ghost-{marker}"
    draft_4 = await _new_draft(gateway, ed, contract_id, "2.0.1", "patch", "2.0.2")
    refunds = f"""  - name: refunds
    physicalType: table
    properties:
      - name: refund_id
        logicalType: string
        primaryKey: true
        required: true
team:
  members:
    - username: {ghost}
"""
    saved_4 = await _save(
        gateway,
        ed,
        contract_id,
        draft_4,
        _document(odcs_id, "2.0.2", CURRENCY_PROP, ID_PROP, extra=refunds),
    )
    semantic = saved_4["validation"]
    pk = _findings(semantic, "primary-key-required-unique")
    assert len(pk) == 1, semantic
    assert pk[0]["rule_set"]["name"] == "odcs-consistency", pk
    assert pk[0]["rule_set"]["scope"] == "platform", pk
    assert pk[0]["mode"] == "warn" and pk[0]["blocking"] is False, pk
    assert pk[0]["location"]["pointer"] == "/schema/1", pk
    member = _findings(semantic, "team-member-exists")
    assert len(member) == 1, semantic
    assert member[0]["rule_set"]["name"] == "odcs-references", member
    assert member[0]["mode"] == "warn" and member[0]["blocking"] is False, member
    assert ghost not in json.dumps(semantic)  # the report names locations, never the value
    assert semantic["summary"]["publishable"] is True, semantic  # warn grace never blocks
