"""E2E-01: the milestone's engineer journey, with its audit trail, through the gateway (D-182).

ROADMAP Phase 7 goal: "Authorized users can answer 'who changed or accessed what, and when' through
the API. The whole product surface is documented and usable by every principal type, and the full
engineer journey is proven end to end."

The journey runs twice, once with a local author and once with an Entra author (alice). Every call
goes through https://traefik:

1. setup as the Global admin: a per-run Organization O, a Project P and Teams A and B, a published
   template at O whose property description fails this run's rule, and a rule set at O with one
   rule keyed on the run marker, made mandatory and enforced now;
2. the author (team editor at A) creates a contract from the template; publish is refused with 422
   ``contract-validation-failed`` naming the rule; the author fixes the draft and publishes 1.0.0,
   then 1.1.0;
3. the Team B principal (team admin at B, consumer at A, of the other IdP) and a service-account
   key (consumer at A) fetch Latest and 1.0.0; the Team B principal's draft on Team A's contract is
   refused as 404 (``contract:update`` is visible only through ``contract:read``);
4. an Org admin of O reads the trail through ``GET /api/v1/audit/events``: the contract's change
   events in occurrence order with ``contract.version.publish_refused`` naming the rule, one access
   event per content read, the refused write as a denial at Team A, the setup events by their
   aggregate ids, and "what did this pipeline read" as one call;
5. isolation: the Team B admin (``audit:query`` at Team B only) gets an empty page for the contract
   and for the Team A denial;
6. no returned event contains ``dcp_``, ``whsec_`` or the document text written into the
   contract's descriptions.

Personas: for the local author the Team B principal is the Entra user alice (admitted through the
registered "Data Eng" group, registration reused on 409, granted directly); for the Entra author
(alice) the Team B principal is a local user. The persona the Phase 4 journey asserts is
unregistered is never used, and the fake Graph is never mutated.

Stack hygiene (T-7-36): names are per run, the rule keys on this run's marker so it can fail no
other journey's document, and the mandatory flag is cleared in a ``finally`` block.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

# Mirrors conftest.ENTRA_TENANT / ENTRA_DATA_ENG_GROUP / ENTRA_PERSONAS (a test module does not
# import its conftest under --import-mode=importlib); both mirror the Plan 04-07 seeds.
ENTRA_TENANT = "11111111-2222-4333-8444-555555555555"
ENTRA_DATA_ENG_GROUP = "9a0f0000-0000-4000-8000-0000000000a1"
ALICE = "alice"
ALICE_OID = "a11ce000-0000-4000-8000-000000000001"
ALICE_PRINCIPAL = f"entra:{ENTRA_TENANT}:{ALICE_OID}"

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
CONTRACTS_API = "/api/v1/contracts"
CONTRACTS = f"{CONTRACTS_API}/contracts"
VALIDATION = "/api/v1/validation"
AUDIT_EVENTS = "/api/v1/audit/events"
YAML = "application/yaml"
JSON = "application/json"
DRAFTS_ROUTE = "/api/v1/contracts/contracts/{contract_id}/drafts"
TRAIL_TIMEOUT_S = 60.0
Headers = dict[str, str]


def _bearer(token: str) -> Headers:
    return {"Authorization": f"Bearer {token}"}


def _expiry(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _contract_yaml(
    odcs_id: str,
    name: str,
    version: str = "1.0.0",
    description: str = "Phase 7 journey contract.",
    property_description: str = "The order identifier.",
    extra_property: bool = False,
) -> bytes:
    """A conformant ODCS contract; the descriptions carry the caller's (per-run) text."""
    extra = (
        """
      - name: amount
        logicalType: number
        description: "The order amount."
"""
        if extra_property
        else "\n"
    )
    text = f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: {name}
version: "{version}"
status: draft
domain: phase7
tags: [phase7, journey]
description:
  purpose: "{description}"
schema:
  - name: orders
    physicalType: table
    properties:
      - name: order_id
        logicalType: string
        description: "{property_description}"{extra}"""
    return text.encode("utf-8")


def _problem_type(response: httpx.Response) -> str:
    assert response.headers["content-type"].startswith("application/problem+json"), (
        response.headers,
        response.text,
    )
    return str(response.json()["type"])


async def _folder(gateway: httpx.AsyncClient, headers: Headers, parent_id: str, slug: str) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Phase 7 {slug}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _grant(
    gateway: httpx.AsyncClient,
    headers: Headers,
    folder_id: str,
    principal_type: str,
    principal_id: str,
    role: str,
) -> None:
    response = await gateway.post(
        f"{FOLDERS}/{folder_id}/grants",
        json={"principal_type": principal_type, "principal_id": principal_id, "role": role},
        headers=headers,
    )
    assert response.status_code == 201, response.text


async def _register(
    gateway: httpx.AsyncClient, headers: Headers, path: str, owner_folder_id: str, object_id: str
) -> str:
    """Register an Entra group; on 409 (an earlier run or journey) reuse the registration."""
    created = await gateway.post(
        path, json={"owner_folder_id": owner_folder_id, "object_id": object_id}, headers=headers
    )
    if created.status_code in (200, 201):
        return str(created.json()["id"])
    assert created.status_code == 409, created.text
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        page = await gateway.get(path, params=params, headers=headers)
        assert page.status_code == 200, page.text
        body = page.json()
        for item in body["items"]:
            if item["object_id"] == object_id:
                return str(item["id"])
        cursor = body["next_cursor"]
        assert cursor is not None, f"{object_id} is registered (409) but not listed"


def _rule(key: str, forbidden: str) -> dict[str, Any]:
    def table(*properties: dict[str, Any]) -> dict[str, Any]:
        return {"schema": [{"name": "orders", "properties": list(properties)}]}

    return {
        "key": key,
        "target": "$.schema[*].properties[*]",
        "condition": f'!has(this.description) || !this.description.startsWith("{forbidden}")',
        "severity": "error",
        "message": "A property description must not start with this run's forbidden marker.",
        "fixtures": {
            "pass": [{"document": table({"name": "a"}, {"name": "b", "description": "fine"})}],
            "fail": [{"document": table({"name": "a", "description": f"{forbidden} nope"})}],
        },
    }


async def _publish_rule_set(
    gateway: httpx.AsyncClient,
    headers: Headers,
    folder_id: str,
    name: str,
    rules: list[dict[str, Any]],
) -> str:
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


async def _save(
    gateway: httpx.AsyncClient,
    headers: Headers,
    contract_id: str,
    draft: dict[str, Any],
    content: bytes,
) -> dict[str, Any]:
    response = await gateway.put(
        f"{CONTRACTS}/{contract_id}/drafts/{draft['id']}",
        content=content,
        headers={**headers, "Content-Type": YAML, "If-Match": draft["etag"]},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _events(gateway: httpx.AsyncClient, headers: Headers, **params: str) -> dict[str, Any]:
    response = await gateway.get(AUDIT_EVENTS, params={"limit": 200, **params}, headers=headers)
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


def _occurrence_order(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda e: (e["occurred_at"], e["event_id"]))


def _actions(events: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    return [e for e in events if e["action"] == action]


async def test_the_engineer_journey_with_its_audit_trail(
    gateway: httpx.AsyncClient,
    admin_token: str,
    users: Any,
    entra_token: Callable[[str], Awaitable[str]],
    wait_for: Callable[..., Any],
    author: str,
) -> None:
    admin = _bearer(admin_token)
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root", root
    root_id = str(root["id"])
    run = uuid.uuid4().hex[:10]
    marker = uuid.uuid4().hex[:12]  # the rule keys on it: no other journey's document has it
    doc_text = f"doctext-{uuid.uuid4().hex[:16]}"  # document content that audit must never hold
    forbidden = f"FORBIDDEN-{marker}"
    rule_key = f"no-marker-{marker}"

    # --- 1. setup as the Global admin --------------------------------------------------------
    org = await _folder(gateway, admin, root_id, f"p7j-{author}-{run}")
    project = await _folder(gateway, admin, org, "data")
    team_a = await _folder(gateway, admin, project, "team-a")
    team_b = await _folder(gateway, admin, project, "team-b")

    org_admin_user = await users.create("p7jorg")
    await _grant(gateway, admin, org, "user", org_admin_user.principal_id, "org_admin")
    org_admin = _bearer(await users.token(org_admin_user))

    # Alice is admitted through the registered Data Eng group; her first request creates the user
    # record a direct grant needs.
    await _register(gateway, admin, f"{IDENTITY}/entra/groups", root_id, ENTRA_DATA_ENG_GROUP)
    alice = _bearer(await entra_token(ALICE))
    admitted = await gateway.get(FOLDERS, params={"limit": 1}, headers=alice)
    assert admitted.status_code == 200, admitted.text

    local_user = await users.create("p7jloc")
    if author == "local":
        await _grant(gateway, admin, team_a, "user", local_user.principal_id, "team_editor")
        writer = _bearer(await users.token(local_user))
        team_b_principal, team_b_actor = alice, ALICE_PRINCIPAL
        await _grant(gateway, admin, team_b, "user", ALICE_PRINCIPAL, "team_admin")
        await _grant(gateway, admin, team_a, "user", ALICE_PRINCIPAL, "consumer")
        author_actor = local_user.principal_id
    else:
        await _grant(gateway, admin, team_a, "user", ALICE_PRINCIPAL, "team_editor")
        writer = alice
        await _grant(gateway, admin, team_b, "user", local_user.principal_id, "team_admin")
        await _grant(gateway, admin, team_a, "user", local_user.principal_id, "consumer")
        team_b_principal = _bearer(await users.token(local_user))
        team_b_actor = local_user.principal_id
        author_actor = ALICE_PRINCIPAL

    account = await gateway.post(
        f"{IDENTITY}/service-accounts",
        json={"owner_folder_id": org, "name": f"p7j-sa-{run}"},
        headers=admin,
    )
    assert account.status_code == 201, account.text
    account_id = str(account.json()["id"])
    sa_actor = f"sa:{account_id}"
    await _grant(gateway, admin, team_a, "service_account", sa_actor, "consumer")
    key = await gateway.post(
        f"{IDENTITY}/service-accounts/{account_id}/keys",
        json={"name": "pipeline", "expires_at": _expiry()},
        headers=admin,
    )
    assert key.status_code == 201, key.text
    key_token = str(key.json()["token"])
    assert key_token.startswith("dcp_"), "service-account keys carry the greppable prefix"
    pipeline = _bearer(key_token)

    # The template's property description starts with this run's forbidden marker.
    template = await gateway.post(
        f"{CONTRACTS_API}/folders/{org}/templates",
        params={"name": f"p7j-template-{run}"},
        content=_contract_yaml(
            f"urn:datacontract:phase7:template:{uuid.uuid4()}",
            "Phase 7 Orders",
            description=f"{doc_text} purpose",
            property_description=f"{forbidden} {doc_text} order id",
        ),
        headers={**admin, "Content-Type": YAML},
    )
    assert template.status_code == 201, template.text
    template_id = str(template.json()["template"]["id"])
    template_published = await gateway.post(
        f"{CONTRACTS_API}/templates/{template_id}/publish", headers=admin
    )
    assert template_published.status_code == 201, template_published.text

    rule_set_id: str | None = None
    mandatory_on = False
    try:
        rule_set_id = await _publish_rule_set(
            gateway, admin, org, f"p7j-rules-{run}", [_rule(rule_key, forbidden)]
        )
        mandatory_on = True
        flagged = await _set_mandatory(gateway, admin, rule_set_id, True)
        assert flagged.status_code == 200, flagged.text
        assert flagged.json()["mandatory"] is True, flagged.text
        enforced = await gateway.post(
            f"{VALIDATION}/rule-sets/{rule_set_id}/versions/1/enforce-now", headers=admin
        )
        assert enforced.status_code == 200, enforced.text

        # --- 2. the author: create from the template, refused, fix, publish 1.0.0 and 1.1.0 --
        odcs_id = f"urn:datacontract:phase7:{uuid.uuid4()}"
        created = await gateway.post(
            f"{CONTRACTS_API}/folders/{team_a}/contracts/from-template",
            json={"template_id": template_id, "odcs_id": odcs_id},
            headers=writer,
        )
        assert created.status_code == 201, created.text
        contract_id = str(created.json()["contract"]["id"])
        draft = created.json()["draft"]
        assert created.json()["contract"]["template"]["template_id"] == template_id

        refused = await _publish(gateway, writer, contract_id, draft)
        assert refused.status_code == 422, refused.text
        assert _problem_type(refused) == "urn:dc:problem:contract-validation-failed"
        report = refused.json()["report"]
        blocking = [f for f in report["findings"] if f["rule_key"] == rule_key]
        assert blocking and blocking[0]["blocking"] is True, report

        fixed = await _save(
            gateway,
            writer,
            contract_id,
            draft,
            _contract_yaml(
                odcs_id,
                "Phase 7 Orders",
                description=f"{doc_text} purpose",
                property_description=f"{doc_text} order id",
            ),
        )
        assert fixed["validation"]["summary"]["publishable"] is True, fixed
        published_1 = await _publish(gateway, writer, contract_id, fixed["draft"])
        assert published_1.status_code == 201, published_1.text
        assert published_1.json()["semver"] == "1.0.0"

        started = await gateway.post(
            f"{CONTRACTS}/{contract_id}/drafts", json={"from_version": "1.0.0"}, headers=writer
        )
        assert started.status_code == 201, started.text
        draft_2 = started.json()["draft"]
        assert draft_2["declared_version"] == "1.1.0", draft_2
        saved_2 = await _save(
            gateway,
            writer,
            contract_id,
            draft_2,
            _contract_yaml(
                odcs_id,
                "Phase 7 Orders",
                version="1.1.0",
                description=f"{doc_text} purpose",
                property_description=f"{doc_text} order id",
                extra_property=True,
            ),
        )
        published_2 = await _publish(gateway, writer, contract_id, saved_2["draft"])
        assert published_2.status_code == 201, published_2.text
        assert published_2.json()["semver"] == "1.1.0"
    finally:
        # Never leave a mandatory rule set behind (T-7-36), even when an assertion failed.
        if mandatory_on and rule_set_id is not None:
            cleared = await _set_mandatory(gateway, admin, rule_set_id, False)
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["mandatory"] is False, cleared.text
    assert rule_set_id is not None

    # --- 3. the other team and the pipeline read; the other team's write is refused ----------
    for reader in (team_b_principal, pipeline):
        for ref in ("latest", "1.0.0"):
            got = await gateway.get(f"{CONTRACTS}/{contract_id}/versions/{ref}", headers=reader)
            assert got.status_code == 200, got.text
            assert doc_text.encode() in got.content  # it really is the content
    request_id = str(uuid.uuid4())
    write = await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts",
        json={"from_version": "1.1.0"},
        headers={**team_b_principal, "X-Request-ID": request_id},
    )
    assert write.status_code == 404, write.text
    assert _problem_type(write) == "urn:dc:problem:not-found"

    # --- 4. the Org admin reads the trail ----------------------------------------------------
    returned: list[dict[str, Any]] = []

    async def trail() -> list[dict[str, Any]] | None:
        page = await _events(
            gateway, org_admin, aggregate_type="contract", aggregate_id=contract_id
        )
        assert page["next_cursor"] is None, "the journey's trail fits in one page"
        events: list[dict[str, Any]] = page["items"]
        access = [e for e in events if e["category"] == "access"]
        complete = (
            len(_actions(events, "contract.version_published")) == 2
            and len(_actions(events, "contract.version.publish_refused")) == 1
            and len([e for e in access if e["actor"]["id"] == team_b_actor]) >= 2
            and len([e for e in access if e["actor"]["id"] == sa_actor]) >= 2
        )
        return events if complete else None

    events = _occurrence_order(
        await wait_for(trail, TRAIL_TIMEOUT_S, interval_s=1.0, description="the contract trail")
    )
    returned.extend(events)
    changes = [e for e in events if e["category"] == "change"]
    names = [e["action"] for e in changes]
    assert names[0] == "contract.created", names
    assert changes[0]["actor"]["id"] == author_actor, changes[0]
    refused_at = names.index("contract.version.publish_refused")
    first_publish = names.index("contract.version_published")
    assert 0 < refused_at < first_publish, names
    assert "contract.draft_updated" in names[refused_at:first_publish], names
    assert names.count("contract.version_published") == 2, names
    assert names.count("contract.version.publish_refused") == 1, names
    refusal = changes[refused_at]
    assert refusal["after"]["draft_id"] == draft["id"], refusal
    assert refusal["after"]["semver"] == "1.0.0", refusal
    assert {"rule_key": rule_key, "code": "fail"} in refusal["after"]["blocking"], refusal
    published_semvers = [
        e["after"]["semver"] for e in _actions(changes, "contract.version_published")
    ]
    assert published_semvers == ["1.0.0", "1.1.0"], published_semvers

    access = [e for e in events if e["category"] == "access"]
    last_publish = max(e["occurred_at"] for e in _actions(changes, "contract.version_published"))
    for actor in (team_b_actor, sa_actor):
        reads = [e for e in access if e["actor"]["id"] == actor]
        assert len(reads) == 2, (actor, reads)
        assert sorted(e["detail"]["ref"] for e in reads) == ["1.0.0", "latest"], reads
        for read in reads:
            assert read["action"] == "read" and read["service"] == "contracts", read
            assert read["before"] is None and read["after"] is None, read
            assert read["detail"]["contract_id"] == contract_id, read
            assert read["occurred_at"] >= last_publish, read
    assert {e["actor"]["id"] for e in access} == {team_b_actor, sa_actor}, access

    async def denial() -> list[dict[str, Any]] | None:
        page = await _events(gateway, org_admin, category="denial", aggregate_id=team_a)
        found = [e for e in page["items"] if e["request_id"] == request_id]
        return page["items"] if found else None

    denials = await wait_for(
        denial, TRAIL_TIMEOUT_S, interval_s=1.0, description="the refused write as a denial"
    )
    returned.extend(denials)
    refused_write = [e for e in denials if e["request_id"] == request_id]
    assert len(refused_write) == 1, refused_write
    row = refused_write[0]
    assert row["aggregate_type"] == "authz" and row["action"] == "check.denied", row
    assert row["actor"]["id"] == team_b_actor, row
    assert row["detail"]["outcome"] == "not_found", row
    assert row["detail"]["route"] == DRAFTS_ROUTE, row
    assert row["detail"]["service"] == "contracts", row
    assert row["detail"]["permission"] == "contract:update", row

    setup = {
        org: "folder.created",
        project: "folder.created",
        team_a: "folder.created",
        team_b: "folder.created",
        template_id: "template.published",
        rule_set_id: "ruleset.enforced_early",
    }
    for aggregate_id, action in setup.items():

        async def found(aggregate_id: str = aggregate_id, action: str = action) -> Any:
            page = await _events(gateway, org_admin, aggregate_id=aggregate_id)
            return page["items"] if _actions(page["items"], action) else None

        setup_events = await wait_for(
            found, TRAIL_TIMEOUT_S, interval_s=1.0, description=f"{action} for {aggregate_id}"
        )
        returned.extend(setup_events)
    rule_set_events = [e for e in returned if e["aggregate_id"] == rule_set_id]
    for action in ("ruleset.created", "ruleset.version_published", "ruleset.mandatory_set"):
        assert _actions(rule_set_events, action), (action, rule_set_events)
    assert _actions([e for e in returned if e["aggregate_id"] == template_id], "template.created")

    # "What did this pipeline read" is one call.
    pipeline_page = await _events(gateway, org_admin, actor_id=sa_actor, category="access")
    pipeline_reads = pipeline_page["items"]
    returned.extend(pipeline_reads)
    assert len(pipeline_reads) == 2, pipeline_reads
    assert {e["actor"]["id"] for e in pipeline_reads} == {sa_actor}, pipeline_reads
    assert {e["aggregate_id"] for e in pipeline_reads} == {contract_id}, pipeline_reads

    # --- 5. isolation: the Team B admin sees nothing of Team A -------------------------------
    for params in (
        {"aggregate_id": contract_id},
        {"aggregate_type": "contract", "aggregate_id": contract_id},
        {"category": "denial", "aggregate_id": team_a},
    ):
        isolated = await _events(gateway, team_b_principal, **params)
        assert isolated == {"items": [], "next_cursor": None}, (params, isolated)

    # --- 6. no secret and no document content in anything the trail returned -----------------
    assert returned
    for event in returned:
        serialised = json.dumps(event)
        assert "dcp_" not in serialised, event
        assert "whsec_" not in serialised, event
        assert doc_text not in serialised, event
        assert forbidden not in serialised, event  # neither the document nor the rule condition


@pytest.fixture(params=["local", "entra"])
def author(request: pytest.FixtureRequest) -> str:
    """Who authors the contract: a local user, or the Entra user alice."""
    return str(request.param)
