"""API-04 on the real stack: every area as every principal type, through the gateway (D-181, D-79).

REQUIREMENTS API-04: "every operation ... is available through the versioned REST API to any
authorized user, service principal, PAT or service account". ``api04_areas.json`` (next to this
module) is the gap audit as data: the twelve API-04 areas, each with a representative read and,
where the area writes, its representative writes, plus the interactive-only permission set. The e2e
image carries no ``dc_*`` package, so the permissions and the interactive-only set come from that
file; ``tests/repo/test_operation_exemptions.py`` proves the file matches ``dc_auth.roles`` and the
merged OpenAPI document.

Five principal types hold Org admin and Policy Manager at a per-run Organization:

* ``local_user``: a local user's access JWT (``via="jwt"``);
* ``entra_user``: the Entra persona alice, admitted through the registered "Data Eng" group
  (registration reused on 409), granted directly as ``entra:<tid>:<oid>`` (``via="jwt"``);
* ``entra_service_principal``: the persona pipeline, a registered Entra service principal using
  client credentials (registration reused on 409). Its app token is a JWT, but it is a machine
  credential, so interactive-only operations refuse it like a PAT or key (D-79, WR-01);
* ``pat``: a read-write personal access token of the local user (``via="pat"``);
* ``service_account_key``: a key of a service account owned by the Organization (``via="api_key"``).

A sixth column, ``read_only_pat`` (a read-only PAT of the same local user, D-66), succeeds on every
read and is refused with 403 ``forbidden`` on every write.

The expected outcome of each call is computed from ``api04_areas.json`` alone: the read-only PAT
gets 403 ``forbidden`` on a write; the PAT, the key and the service principal get 403
``interactive-session-required`` on
an operation whose permission is interactive-only (D-79); everything else succeeds. Each area's
runner asserts that it exercised exactly the operations the file lists for it.

Writes create their resources under per-run names inside the per-run Organization (folders,
groups, grants, keys, contracts, versions, templates, rule sets, rules); when a refused principal
needs an existing resource (a grant or key to revoke, a version to deprecate), the Global admin
creates it and removes it afterwards. No rule set is made mandatory. The matrix never uses the Entra
persona that the Phase 4 journey asserts is unregistered, makes grants only on the per-run folders,
and never mutates the fake Graph.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.e2e

AREAS_FILE = Path(__file__).with_name("api04_areas.json")
SPEC: dict[str, Any] = json.loads(AREAS_FILE.read_text(encoding="utf-8"))
AREAS: dict[str, list[dict[str, str]]] = SPEC["areas"]
INTERACTIVE_ONLY: frozenset[str] = frozenset(SPEC["interactive_only"])

# Mirrors conftest.ENTRA_TENANT / ENTRA_DATA_ENG_GROUP / ENTRA_PERSONAS (a test module does not
# import its conftest under --import-mode=importlib); both mirror the Plan 04-07 seeds.
ENTRA_TENANT = "11111111-2222-4333-8444-555555555555"
ENTRA_DATA_ENG_GROUP = "9a0f0000-0000-4000-8000-0000000000a1"
ALICE = "alice"
ALICE_OID = "a11ce000-0000-4000-8000-000000000001"
PIPELINE = "pipeline"
PIPELINE_OID = "5e2b1ce0-0000-4000-8000-000000000004"

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
CONTRACTS_API = "/api/v1/contracts"
CONTRACTS = f"{CONTRACTS_API}/contracts"
VALIDATION = "/api/v1/validation"
AUDIT_EVENTS = "/api/v1/audit/events"
YAML = "application/yaml"
JSON = "application/json"
INTERACTIVE_PROBLEM = "urn:dc:problem:interactive-session-required"
FORBIDDEN_PROBLEM = "urn:dc:problem:forbidden"
AUDIT_WAIT_S = 60.0

PRINCIPALS = (
    "local_user",
    "entra_user",
    "entra_service_principal",
    "pat",
    "service_account_key",
)
READ_ONLY_PAT = "read_only_pat"
NON_INTERACTIVE = frozenset({"pat", "service_account_key", "entra_service_principal"})
ROLES_AT_ORG = ("org_admin", "policy_manager")
# Short, slug-safe tags for per-principal resource names.
TAGS = {
    "local_user": "lu",
    "entra_user": "eu",
    "entra_service_principal": "sp",
    "pat": "pat",
    "service_account_key": "sak",
    READ_ONLY_PAT: "ro",
}

Headers = dict[str, str]


def _bearer(token: str) -> Headers:
    return {"Authorization": f"Bearer {token}"}


def _expiry(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _contract_yaml(odcs_id: str, name: str, version: str = "1.0.0") -> bytes:
    text = f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: {name}
version: "{version}"
status: draft
domain: phase7
tags: [phase7, matrix]
description:
  purpose: Phase 7 principal matrix.
schema:
  - name: orders
    physicalType: table
    properties:
      - name: order_id
        logicalType: string
"""
    return text.encode("utf-8")


def _odcs_id() -> str:
    return f"urn:datacontract:phase7-matrix:{uuid.uuid4()}"


def _rule(marker: str) -> dict[str, Any]:
    """A rule that can fail only a document carrying this run's marker (T-5-54 hygiene)."""
    forbidden = f"FORBIDDEN-{marker}"
    return {
        "target": "$.schema[*].properties[*]",
        "condition": f'!has(this.description) || !this.description.startsWith("{forbidden}")',
        "severity": "error",
        "message": "A property description must not start with the forbidden marker.",
        "fixtures": {
            "pass": [{"document": {"schema": [{"name": "t", "properties": [{"name": "a"}]}]}}],
            "fail": [
                {
                    "document": {
                        "schema": [
                            {
                                "name": "t",
                                "properties": [{"name": "a", "description": f"{forbidden} x"}],
                            }
                        ]
                    }
                }
            ],
        },
    }


def _operation(method: str, path: str) -> dict[str, str]:
    for operations in AREAS.values():
        for operation in operations:
            if operation["method"] == method and operation["path"] == path:
                return operation
    raise AssertionError(f"{method} {path} is not listed in {AREAS_FILE.name}")


def _expected_problem(principal: str, operation: dict[str, str]) -> str | None:
    """The problem type the principal must get, or None when the call must succeed."""
    if principal == READ_ONLY_PAT and operation["kind"] == "write":
        return FORBIDDEN_PROBLEM  # D-66
    if principal in NON_INTERACTIVE and operation["permission"] in INTERACTIVE_ONLY:
        return INTERACTIVE_PROBLEM  # D-79
    return None


async def _folder(gateway: httpx.AsyncClient, headers: Headers, parent_id: str, slug: str) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Phase 7 matrix {slug}"},
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
) -> str:
    response = await gateway.post(
        f"{FOLDERS}/{folder_id}/grants",
        json={"principal_type": principal_type, "principal_id": principal_id, "role": role},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _register(
    gateway: httpx.AsyncClient, headers: Headers, path: str, owner_folder_id: str, object_id: str
) -> str:
    """Register an Entra group or service principal; on 409 (an earlier run) reuse it."""
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


async def _new_contract(
    gateway: httpx.AsyncClient, headers: Headers, folder_id: str, name: str
) -> tuple[str, dict[str, Any], str]:
    """Create a contract with its first draft; returns (contract id, draft, ODCS id)."""
    odcs_id = _odcs_id()
    created = await gateway.post(
        f"{CONTRACTS_API}/folders/{folder_id}/contracts",
        content=_contract_yaml(odcs_id, name),
        headers={**headers, "Content-Type": YAML},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    return str(body["contract"]["id"]), dict(body["draft"]), odcs_id


async def _publish(
    gateway: httpx.AsyncClient, headers: Headers, contract_id: str, draft: dict[str, Any]
) -> httpx.Response:
    return await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts/{draft['id']}/publish",
        headers={**headers, "If-Match": draft["etag"]},
    )


@dataclass
class World:
    """The per-run Organization, its principals and the shared resources the reads address."""

    gateway: httpx.AsyncClient
    admin_tokens: Any
    users: Any
    entra_token: Callable[[str], Awaitable[str]]
    run: str
    marker: str
    root: str
    org: str
    project: str
    team: str
    local_user: Any
    pat: str
    read_only_pat: str
    service_account_id: str
    service_account_key: str
    target_service_account_id: str
    grantee: str
    group_id: str
    contract_id: str
    draft_id: str
    template_id: str
    rule_set_id: str
    data_eng_registration: str
    pipeline_registration: str
    event_id: str | None = None

    async def admin(self) -> Headers:
        return _bearer(await self.admin_tokens.get())

    async def bearer(self, principal: str) -> Headers:
        if principal == "local_user":
            return _bearer(await self.users.token(self.local_user))
        if principal == "entra_user":
            return _bearer(await self.entra_token(ALICE))
        if principal == "entra_service_principal":
            return _bearer(await self.entra_token(PIPELINE))
        if principal == "pat":
            return _bearer(self.pat)
        if principal == "service_account_key":
            return _bearer(self.service_account_key)
        if principal == READ_ONLY_PAT:
            return _bearer(self.read_only_pat)
        raise AssertionError(f"unknown principal {principal}")


@pytest_asyncio.fixture(scope="module")
async def world(
    gateway: httpx.AsyncClient,
    _admin_tokens: Any,
    users: Any,
    entra_token: Callable[[str], Awaitable[str]],
) -> World:
    admin = _bearer(await _admin_tokens.get())
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root", root
    root_id = str(root["id"])
    run = uuid.uuid4().hex[:10]
    marker = uuid.uuid4().hex[:12]
    org = await _folder(gateway, admin, root_id, f"p7m-{run}")
    project = await _folder(gateway, admin, org, "payments")
    team = await _folder(gateway, admin, project, "ingest")

    # local user (JWT) with a read-write and a read-only PAT
    local_user = await users.create("p7m")
    for role in ROLES_AT_ORG:
        await _grant(gateway, admin, org, "user", local_user.principal_id, role)
    local = _bearer(await users.token(local_user))
    pat = await gateway.post(
        f"{IDENTITY}/pats", json={"name": f"p7m-{run}", "expires_at": _expiry()}, headers=local
    )
    assert pat.status_code == 201, pat.text
    read_only = await gateway.post(
        f"{IDENTITY}/pats",
        json={"name": f"p7m-ro-{run}", "expires_at": _expiry(), "read_only": True},
        headers=local,
    )
    assert read_only.status_code == 201, read_only.text
    assert read_only.json()["read_only"] is True, read_only.text

    # Entra user: admitted through the registered Data Eng group (its first request creates the
    # user record a grant needs), then granted directly.
    data_eng_registration = await _register(
        gateway, admin, f"{IDENTITY}/entra/groups", root_id, ENTRA_DATA_ENG_GROUP
    )
    alice = _bearer(await entra_token(ALICE))
    admitted = await gateway.get(FOLDERS, params={"limit": 1}, headers=alice)
    assert admitted.status_code == 200, admitted.text
    for role in ROLES_AT_ORG:
        await _grant(gateway, admin, org, "user", f"entra:{ENTRA_TENANT}:{ALICE_OID}", role)

    # Entra service principal (client credentials)
    pipeline_registration = await _register(
        gateway, admin, f"{IDENTITY}/entra/service-principals", root_id, PIPELINE_OID
    )
    for role in ROLES_AT_ORG:
        await _grant(
            gateway, admin, org, "service_principal", f"entra:{ENTRA_TENANT}:{PIPELINE_OID}", role
        )

    # service account owned by the Organization, granted directly, with one key; a second
    # account is the target of the key-management writes so the principal's own key is untouched
    accounts: list[str] = []
    for suffix in ("sa", "target"):
        account = await gateway.post(
            f"{IDENTITY}/service-accounts",
            json={"owner_folder_id": org, "name": f"p7m-{suffix}-{run}"},
            headers=admin,
        )
        assert account.status_code == 201, account.text
        accounts.append(str(account.json()["id"]))
    account_id, target_id = accounts
    for role in ROLES_AT_ORG:
        await _grant(gateway, admin, org, "service_account", f"sa:{account_id}", role)
    key = await gateway.post(
        f"{IDENTITY}/service-accounts/{account_id}/keys",
        json={"name": "matrix", "expires_at": _expiry()},
        headers=admin,
    )
    assert key.status_code == 201, key.text

    # shared resources the reads address
    grantee = await users.create("p7mg")
    group = await gateway.post(
        f"{IDENTITY}/groups",
        json={"owner_folder_id": org, "name": f"p7m-group-{run}"},
        headers=admin,
    )
    assert group.status_code == 201, group.text
    contract_id, draft, _ = await _new_contract(gateway, admin, team, "Phase 7 Matrix Orders")
    published = await _publish(gateway, admin, contract_id, draft)
    assert published.status_code == 201, published.text
    open_draft = await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts", json={"from_version": "1.0.0"}, headers=admin
    )
    assert open_draft.status_code == 201, open_draft.text
    template = await gateway.post(
        f"{CONTRACTS_API}/folders/{org}/templates",
        params={"name": f"p7m-template-{run}"},
        content=_contract_yaml(_odcs_id(), "Phase 7 Matrix Template"),
        headers={**admin, "Content-Type": YAML},
    )
    assert template.status_code == 201, template.text
    template_id = str(template.json()["template"]["id"])
    template_published = await gateway.post(
        f"{CONTRACTS_API}/templates/{template_id}/publish", headers=admin
    )
    assert template_published.status_code == 201, template_published.text
    rule_set = await gateway.post(
        f"{VALIDATION}/folders/{org}/rule-sets", json={"name": f"p7m-rules-{run}"}, headers=admin
    )
    assert rule_set.status_code == 201, rule_set.text
    rule_set_id = str(rule_set.json()["id"])
    saved = await gateway.put(
        f"{VALIDATION}/rule-sets/{rule_set_id}/draft",
        content=json.dumps(
            {"description": "Phase 7 matrix", "rules": [{"key": "no-marker", **_rule(marker)}]}
        ),
        headers={**admin, "Content-Type": JSON, "If-Match": rule_set.headers["etag"]},
    )
    assert saved.status_code == 200, saved.text
    rules_published = await gateway.post(
        f"{VALIDATION}/rule-sets/{rule_set_id}/publish",
        headers={**admin, "If-Match": saved.headers["etag"]},
    )
    assert rules_published.status_code == 201, rules_published.text

    return World(
        gateway=gateway,
        admin_tokens=_admin_tokens,
        users=users,
        entra_token=entra_token,
        run=run,
        marker=marker,
        root=root_id,
        org=org,
        project=project,
        team=team,
        local_user=local_user,
        pat=str(pat.json()["token"]),
        read_only_pat=str(read_only.json()["token"]),
        service_account_id=account_id,
        service_account_key=str(key.json()["token"]),
        target_service_account_id=target_id,
        grantee=grantee.principal_id,
        group_id=str(group.json()["id"]),
        contract_id=contract_id,
        draft_id=str(open_draft.json()["draft"]["id"]),
        template_id=template_id,
        rule_set_id=rule_set_id,
        data_eng_registration=data_eng_registration,
        pipeline_registration=pipeline_registration,
    )


@dataclass
class Probe:
    """One principal's run through one area: asserts each call and records what it exercised."""

    world: World
    principal: str
    headers: Headers
    wait_for: Callable[..., Awaitable[Any]]
    exercised: set[tuple[str, str]] = field(default_factory=set)

    @property
    def gateway(self) -> httpx.AsyncClient:
        return self.world.gateway

    @property
    def tag(self) -> str:
        return f"{TAGS[self.principal]}-{uuid.uuid4().hex[:6]}"

    def check(self, method: str, path: str, response: httpx.Response, ok: int) -> bool:
        """Assert the outcome ``api04_areas.json`` predicts; True when the call succeeded."""
        self.exercised.add((method, path))
        expected = _expected_problem(self.principal, _operation(method, path))
        label = (self.principal, method, path)
        if expected is None:
            assert response.status_code == ok, (*label, response.status_code, response.text)
            return True
        assert response.status_code == 403, (*label, response.status_code, response.text)
        assert response.headers["content-type"].startswith("application/problem+json"), (
            *label,
            response.headers,
        )
        assert response.json()["type"] == expected, (*label, response.text)
        return False


# --- one runner per API-04 area --------------------------------------------------------------


async def _folders(p: Probe) -> None:
    w = p.world
    got = await p.gateway.get(f"{FOLDERS}/{w.team}", headers=p.headers)
    p.check("GET", f"{FOLDERS}/{{folder_id}}", got, 200)
    slug = f"m-{p.tag}"
    created = await p.gateway.post(
        FOLDERS,
        json={"parent_id": w.project, "slug": slug, "display_name": f"Matrix {slug}"},
        headers=p.headers,
    )
    p.check("POST", FOLDERS, created, 201)


async def _groups(p: Probe) -> None:
    w = p.world
    got = await p.gateway.get(f"{IDENTITY}/groups/{w.group_id}", headers=p.headers)
    p.check("GET", f"{IDENTITY}/groups/{{group_id}}", got, 200)
    created = await p.gateway.post(
        f"{IDENTITY}/groups",
        json={"owner_folder_id": w.org, "name": f"p7m-{w.run}-{p.tag}"},
        headers=p.headers,
    )
    p.check("POST", f"{IDENTITY}/groups", created, 201)
    renamed = await p.gateway.patch(
        f"{IDENTITY}/groups/{w.group_id}",
        json={"name": f"p7m-group-{w.run}-{p.tag}"},
        headers=p.headers,
    )
    p.check("PATCH", f"{IDENTITY}/groups/{{group_id}}", renamed, 200)


async def _grants(p: Probe) -> None:
    w = p.world
    admin = await w.admin()
    listed = await p.gateway.get(f"{FOLDERS}/{w.team}/grants", headers=p.headers)
    p.check("GET", f"{FOLDERS}/{{folder_id}}/grants", listed, 200)
    body = {"principal_type": "user", "principal_id": w.grantee, "role": "team_editor"}
    created = await p.gateway.post(f"{FOLDERS}/{w.team}/grants", json=body, headers=p.headers)
    if p.check("POST", f"{FOLDERS}/{{folder_id}}/grants", created, 201):
        grant_id = str(created.json()["id"])
    else:
        grant_id = await _grant(gateway=p.gateway, headers=admin, folder_id=w.team, **body)
    revoked = await p.gateway.delete(f"{IDENTITY}/grants/{grant_id}", headers=p.headers)
    if not p.check("DELETE", f"{IDENTITY}/grants/{{grant_id}}", revoked, 204):
        cleanup = await p.gateway.delete(f"{IDENTITY}/grants/{grant_id}", headers=admin)
        assert cleanup.status_code == 204, cleanup.text


async def _tokens(p: Probe) -> None:
    w = p.world
    admin = await w.admin()
    keys = f"{IDENTITY}/service-accounts/{w.target_service_account_id}/keys"
    keys_template = f"{IDENTITY}/service-accounts/{{service_account_id}}/keys"
    listed = await p.gateway.get(keys, headers=p.headers)
    p.check("GET", keys_template, listed, 200)
    body = {"name": f"matrix-{p.tag}", "expires_at": _expiry()}
    created = await p.gateway.post(keys, json=body, headers=p.headers)
    if p.check("POST", keys_template, created, 201):
        key_id = str(created.json()["id"])
    else:
        by_admin = await p.gateway.post(keys, json=body, headers=admin)
        assert by_admin.status_code == 201, by_admin.text
        key_id = str(by_admin.json()["id"])
    revoked = await p.gateway.delete(f"{keys}/{key_id}", headers=p.headers)
    if not p.check("DELETE", f"{keys_template}/{{key_id}}", revoked, 204):
        cleanup = await p.gateway.delete(f"{keys}/{key_id}", headers=admin)
        assert cleanup.status_code == 204, cleanup.text


async def _contracts(p: Probe) -> None:
    w = p.world
    got = await p.gateway.get(f"{CONTRACTS}/{w.contract_id}", headers=p.headers)
    p.check("GET", f"{CONTRACTS}/{{contract_id}}", got, 200)
    created = await p.gateway.post(
        f"{CONTRACTS_API}/folders/{w.team}/contracts",
        content=_contract_yaml(_odcs_id(), f"Matrix {p.tag}"),
        headers={**p.headers, "Content-Type": YAML},
    )
    p.check("POST", f"{CONTRACTS_API}/folders/{{folder_id}}/contracts", created, 201)


async def _versions(p: Probe) -> None:
    w = p.world
    listed = await p.gateway.get(f"{CONTRACTS}/{w.contract_id}/versions", headers=p.headers)
    p.check("GET", f"{CONTRACTS}/{{contract_id}}/versions", listed, 200)
    fetched = await p.gateway.get(f"{CONTRACTS}/{w.contract_id}/versions/1.0.0", headers=p.headers)
    p.check("GET", f"{CONTRACTS}/{{contract_id}}/versions/{{ref}}", fetched, 200)
    contract_id, draft, odcs_id = await _new_contract(
        p.gateway, await w.admin(), w.team, f"Matrix versions {p.tag}"
    )
    saved = await p.gateway.put(
        f"{CONTRACTS}/{contract_id}/drafts/{draft['id']}",
        content=_contract_yaml(odcs_id, f"Matrix versions {p.tag} edited"),
        headers={**p.headers, "Content-Type": YAML, "If-Match": draft["etag"]},
    )
    p.check("PUT", f"{CONTRACTS}/{{contract_id}}/drafts/{{draft_id}}", saved, 200)


async def _lifecycle(p: Probe) -> None:
    w = p.world
    admin = await w.admin()
    metadata = await p.gateway.get(
        f"{CONTRACTS}/{w.contract_id}/versions/latest/metadata", headers=p.headers
    )
    p.check("GET", f"{CONTRACTS}/{{contract_id}}/versions/{{ref}}/metadata", metadata, 200)
    contract_id, draft, _ = await _new_contract(
        p.gateway, admin, w.team, f"Matrix lifecycle {p.tag}"
    )
    published = await _publish(p.gateway, p.headers, contract_id, draft)
    if not p.check(
        "POST", f"{CONTRACTS}/{{contract_id}}/drafts/{{draft_id}}/publish", published, 201
    ):
        by_admin = await _publish(p.gateway, admin, contract_id, draft)
        assert by_admin.status_code == 201, by_admin.text
    sunset = (date.today() + timedelta(days=30)).isoformat()
    deprecated = await p.gateway.post(
        f"{CONTRACTS}/{contract_id}/versions/1.0.0/deprecate",
        json={"sunset_date": sunset},
        headers=p.headers,
    )
    p.check("POST", f"{CONTRACTS}/{{contract_id}}/versions/{{ref}}/deprecate", deprecated, 200)


async def _templates(p: Probe) -> None:
    w = p.world
    got = await p.gateway.get(f"{CONTRACTS_API}/templates/{w.template_id}", headers=p.headers)
    p.check("GET", f"{CONTRACTS_API}/templates/{{template_id}}", got, 200)
    created = await p.gateway.post(
        f"{CONTRACTS_API}/folders/{w.org}/templates",
        params={"name": f"p7m-{w.run}-{p.tag}"},
        content=_contract_yaml(_odcs_id(), f"Matrix template {p.tag}"),
        headers={**p.headers, "Content-Type": YAML},
    )
    p.check("POST", f"{CONTRACTS_API}/folders/{{folder_id}}/templates", created, 201)


async def _rules(p: Probe) -> None:
    w = p.world
    got = await p.gateway.get(
        f"{VALIDATION}/rule-sets/{w.rule_set_id}/versions/1", headers=p.headers
    )
    p.check("GET", f"{VALIDATION}/rule-sets/{{rule_set_id}}/versions/{{version_no}}", got, 200)
    rule_set = await p.gateway.post(
        f"{VALIDATION}/folders/{w.org}/rule-sets",
        json={"name": f"p7m-{w.run}-rules-{p.tag}"},
        headers=await w.admin(),
    )
    assert rule_set.status_code == 201, rule_set.text
    put = await p.gateway.put(
        f"{VALIDATION}/rule-sets/{rule_set.json()['id']}/draft/rules/no-marker",
        content=json.dumps(_rule(w.marker)),
        headers={**p.headers, "Content-Type": JSON, "If-Match": rule_set.headers["etag"]},
    )
    p.check("PUT", f"{VALIDATION}/rule-sets/{{rule_set_id}}/draft/rules/{{rule_key}}", put, 200)


async def _rule_sets(p: Probe) -> None:
    w = p.world
    got = await p.gateway.get(f"{VALIDATION}/rule-sets/{w.rule_set_id}", headers=p.headers)
    p.check("GET", f"{VALIDATION}/rule-sets/{{rule_set_id}}", got, 200)
    created = await p.gateway.post(
        f"{VALIDATION}/folders/{w.org}/rule-sets",
        json={"name": f"p7m-{w.run}-set-{p.tag}"},
        headers=p.headers,
    )
    p.check("POST", f"{VALIDATION}/folders/{{folder_id}}/rule-sets", created, 201)


async def _validation(p: Probe) -> None:
    w = p.world
    uploaded = await p.gateway.post(
        f"{VALIDATION}/folders/{w.team}/validate",
        content=_contract_yaml(_odcs_id(), f"Matrix upload {p.tag}"),
        headers={**p.headers, "Content-Type": YAML},
    )
    p.check("POST", f"{VALIDATION}/folders/{{folder_id}}/validate", uploaded, 200)
    stored = await p.gateway.post(
        f"{CONTRACTS}/{w.contract_id}/drafts/{w.draft_id}/validate", headers=p.headers
    )
    p.check("POST", f"{CONTRACTS}/{{contract_id}}/drafts/{{draft_id}}/validate", stored, 200)
    report = await p.gateway.get(
        f"{CONTRACTS}/{w.contract_id}/versions/1.0.0/validation-report", headers=p.headers
    )
    p.check("GET", f"{CONTRACTS}/{{contract_id}}/versions/{{ref}}/validation-report", report, 200)


async def _audit(p: Probe) -> None:
    w = p.world
    if w.event_id is None:
        owner = await w.bearer("local_user")

        async def first_event() -> str | None:
            page = await p.gateway.get(
                AUDIT_EVENTS, params={"aggregate_id": w.contract_id, "limit": 1}, headers=owner
            )
            assert page.status_code == 200, page.text
            items = page.json()["items"]
            return str(items[0]["event_id"]) if items else None

        w.event_id = await p.wait_for(
            first_event, AUDIT_WAIT_S, description="an audit event of the matrix contract"
        )
    listed = await p.gateway.get(
        AUDIT_EVENTS, params={"aggregate_id": w.contract_id, "limit": 50}, headers=p.headers
    )
    p.check("GET", AUDIT_EVENTS, listed, 200)
    assert w.event_id in {item["event_id"] for item in listed.json()["items"]}, (
        p.principal,
        listed.text,
    )
    got = await p.gateway.get(f"{AUDIT_EVENTS}/{w.event_id}", headers=p.headers)
    p.check("GET", f"{AUDIT_EVENTS}/{{event_id}}", got, 200)


RUNNERS: dict[str, Callable[[Probe], Awaitable[None]]] = {
    "folders": _folders,
    "groups": _groups,
    "grants": _grants,
    "tokens": _tokens,
    "contracts": _contracts,
    "versions": _versions,
    "publish/promote/unpublish/deprecate": _lifecycle,
    "templates": _templates,
    "rules": _rules,
    "rule sets": _rule_sets,
    "validation": _validation,
    "audit queries": _audit,
}


def test_every_api04_area_has_a_runner_and_the_golden_interactive_set() -> None:
    assert set(RUNNERS) == set(AREAS)
    assert len(AREAS) == 12
    assert all(AREAS.values())
    assert "service-account:manage-keys" in INTERACTIVE_ONLY and len(INTERACTIVE_ONLY) == 14


async def test_tracer_every_principal_type_reads_the_audit_trail_of_its_organization(
    world: World,
) -> None:
    """D-181 tracer: one representative read succeeds for all five principal types."""
    for principal in PRINCIPALS:
        response = await world.gateway.get(
            AUDIT_EVENTS,
            params={"aggregate_id": world.org, "limit": 50},
            headers=await world.bearer(principal),
        )
        assert response.status_code == 200, (principal, response.text)
        assert isinstance(response.json()["items"], list), (principal, response.text)


@pytest.mark.parametrize("principal", (*PRINCIPALS, READ_ONLY_PAT))
@pytest.mark.parametrize("area", tuple(AREAS))
async def test_every_area_as_every_principal_type(
    area: str, principal: str, world: World, wait_for: Callable[..., Awaitable[Any]]
) -> None:
    """D-181 part 2, D-79, D-66: the area's reads and writes as one principal type."""
    probe = Probe(world, principal, await world.bearer(principal), wait_for)
    await RUNNERS[area](probe)
    listed = {(operation["method"], operation["path"]) for operation in AREAS[area]}
    assert probe.exercised == listed, (area, sorted(listed - probe.exercised))


def _ok(response: httpx.Response, *statuses: int) -> httpx.Response:
    assert response.status_code in statuses, (
        response.request.method,
        response.request.url.path,
        response.status_code,
        response.text,
    )
    return response


async def test_the_rest_of_each_area_as_a_signed_in_org_admin(world: World) -> None:
    """D-181 part 3: the area operations outside the representative set, exercised once.

    The local user (Org admin and Policy Manager, JWT) performs them in the per-run Organization;
    the Global admin performs the root-evaluated ones (user, admin-PAT and Entra-record reads,
    archive and unarchive). Runs after the matrix, so deprecating the shared template and rule-set
    versions and renaming the target service account cannot disturb it. Shared Entra
    registrations are only read, never changed.
    """
    w = world
    gw = w.gateway
    me = await w.bearer("local_user")
    admin = await w.admin()
    tag = uuid.uuid4().hex[:6]

    # --- folders ------------------------------------------------------------------------------
    sub = await _folder(gw, me, w.project, f"x-{tag}")
    _ok(
        await gw.patch(f"{FOLDERS}/{sub}", json={"display_name": f"Renamed {tag}"}, headers=me), 200
    )
    _ok(await gw.get(f"{FOLDERS}/{w.team}/effective-access", headers=me), 200)
    _ok(await gw.get(f"{FOLDERS}/{w.team}/grantable-groups", headers=me), 200)
    _ok(await gw.post(f"{FOLDERS}/{sub}/archive", headers=admin), 200, 204)
    _ok(await gw.post(f"{FOLDERS}/{sub}/unarchive", headers=admin), 200, 204)

    # --- groups -------------------------------------------------------------------------------
    _ok(await gw.get(f"{IDENTITY}/groups", params={"limit": 5}, headers=me), 200)
    group = _ok(
        await gw.post(
            f"{IDENTITY}/groups",
            json={"owner_folder_id": w.org, "name": f"p7m-{w.run}-x-{tag}"},
            headers=me,
        ),
        201,
    )
    members = f"{IDENTITY}/groups/{group.json()['id']}/members"
    _ok(await gw.post(members, json={"principal_id": w.grantee}, headers=me), 201)
    _ok(await gw.get(members, headers=me), 200)
    _ok(await gw.delete(f"{members}/{w.grantee}", headers=me), 204)
    _ok(await gw.delete(f"{IDENTITY}/groups/{group.json()['id']}", headers=me), 204)

    # --- grants -------------------------------------------------------------------------------
    grant_id = await _grant(gw, me, w.team, "user", w.grantee, "team_reader")
    _ok(await gw.get(f"{IDENTITY}/grants/{grant_id}", headers=me), 200)
    _ok(await gw.delete(f"{IDENTITY}/grants/{grant_id}", headers=me), 204)

    # --- tokens, users and Entra records --------------------------------------------------------
    _ok(await gw.get(f"{IDENTITY}/pats", headers=me), 200)
    throwaway = _ok(
        await gw.post(
            f"{IDENTITY}/pats", json={"name": f"p7m-x-{tag}", "expires_at": _expiry()}, headers=me
        ),
        201,
    )
    _ok(await gw.get(f"{IDENTITY}/admin/pats", params={"limit": 5}, headers=admin), 200)
    _ok(await gw.delete(f"{IDENTITY}/admin/pats/{throwaway.json()['id']}", headers=admin), 204)
    account = f"{IDENTITY}/service-accounts/{w.target_service_account_id}"
    _ok(await gw.get(account, headers=me), 200)
    _ok(await gw.patch(account, json={"name": f"p7m-target-{w.run}-{tag}"}, headers=me), 200)
    _ok(await gw.post(f"{account}/deactivate", headers=me), 200)
    _ok(await gw.post(f"{account}/reactivate", headers=me), 200)
    _ok(await gw.get(f"{IDENTITY}/users", params={"limit": 5}, headers=admin), 200)
    _ok(await gw.get(f"{IDENTITY}/users/{w.local_user.id}", headers=admin), 200)
    leaver = await w.users.create("p7mx")
    _ok(await gw.post(f"{IDENTITY}/users/{leaver.id}/deactivate", headers=admin), 200)
    _ok(await gw.post(f"{IDENTITY}/users/{leaver.id}/reactivate", headers=admin), 200)
    _ok(await gw.get(f"{IDENTITY}/entra/groups/{w.data_eng_registration}", headers=admin), 200)
    _ok(
        await gw.get(
            f"{IDENTITY}/entra/service-principals/{w.pipeline_registration}", headers=admin
        ),
        200,
    )
    alice_record: str | None = None
    cursor: str | None = None
    while alice_record is None:
        params: dict[str, Any] = {"limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        page = _ok(await gw.get(f"{IDENTITY}/entra/users", params=params, headers=admin), 200)
        for item in page.json()["items"]:
            if item["object_id"] == ALICE_OID:
                alice_record = str(item["id"])
        cursor = page.json()["next_cursor"]
        assert alice_record is not None or cursor is not None, "alice has no Entra user record"
    _ok(await gw.get(f"{IDENTITY}/entra/users/{alice_record}", headers=admin), 200)

    # --- contracts and versions -----------------------------------------------------------------
    _ok(
        await gw.post(
            f"{CONTRACTS_API}/folders/{w.team}/contracts/starter",
            json={"odcs_id": _odcs_id(), "name": f"Matrix starter {tag}"},
            headers=me,
        ),
        201,
    )
    _ok(
        await gw.post(
            f"{CONTRACTS_API}/folders/{w.team}/contracts/from-version",
            json={
                "source_contract_id": w.contract_id,
                "source_version": "1.0.0",
                "odcs_id": _odcs_id(),
            },
            headers=me,
        ),
        201,
    )
    doomed, _, _ = await _new_contract(gw, me, w.team, f"Matrix deleted {tag}")
    _ok(await gw.delete(f"{CONTRACTS}/{doomed}", headers=me), 204)
    _ok(await gw.post(f"{CONTRACTS}/{doomed}/restore", headers=me), 200)

    contract_id, draft, _ = await _new_contract(gw, me, w.team, f"Matrix lifecycle x {tag}")
    _ok(await _publish(gw, me, contract_id, draft), 201)
    started = _ok(
        await gw.post(
            f"{CONTRACTS}/{contract_id}/drafts", json={"from_version": "1.0.0"}, headers=me
        ),
        201,
    )
    preview = started.json()["draft"]
    published = _ok(await _publish(gw, me, contract_id, preview), 201)
    semver = published.json()["semver"]
    _ok(await gw.post(f"{CONTRACTS}/{contract_id}/versions/{semver}/unpublish", headers=me), 200)
    _ok(await gw.post(f"{CONTRACTS}/{contract_id}/versions/{semver}/republish", headers=me), 200)
    spare = _ok(
        await gw.post(
            f"{CONTRACTS}/{contract_id}/drafts", json={"from_version": "1.0.0"}, headers=me
        ),
        201,
    ).json()["draft"]
    drafts = f"{CONTRACTS}/{contract_id}/drafts"
    _ok(await gw.get(drafts, headers=me), 200)
    _ok(await gw.get(f"{drafts}/{spare['id']}", headers=me), 200)
    _ok(await gw.get(f"{drafts}/{spare['id']}/document", headers=me), 200)
    _ok(
        await gw.delete(f"{drafts}/{spare['id']}", headers={**me, "If-Match": spare["etag"]}),
        204,
    )
    attachment = f"{CONTRACTS}/{contract_id}/rule-sets/{w.rule_set_id}"
    _ok(await gw.put(attachment, json={"version": "latest"}, headers=me), 200, 201)
    _ok(await gw.delete(attachment, headers=me), 204)

    # --- templates ----------------------------------------------------------------------------
    template = f"{CONTRACTS_API}/templates/{w.template_id}"
    _ok(await gw.get(f"{CONTRACTS_API}/templates", params={"limit": 5}, headers=me), 200)
    _ok(await gw.get(f"{template}/document", headers=me), 200)
    _ok(
        await gw.put(
            f"{template}/draft",
            content=_contract_yaml(_odcs_id(), f"Matrix template edited {tag}"),
            headers={**me, "Content-Type": YAML},
        ),
        200,
    )
    _ok(await gw.get(f"{template}/versions", headers=me), 200)
    _ok(await gw.get(f"{template}/versions/1/document", headers=me), 200)
    _ok(
        await gw.get(
            f"{CONTRACTS_API}/folders/{w.team}/available-templates/{w.template_id}"
            "/versions/1/document",
            headers=me,
        ),
        200,
    )
    _ok(await gw.post(f"{template}/versions/1/deprecate", headers=me), 200)

    # --- rules and rule sets --------------------------------------------------------------------
    _ok(await gw.get(f"{VALIDATION}/folders/{w.org}/rule-sets", headers=me), 200)
    scratch = _ok(
        await gw.post(
            f"{VALIDATION}/folders/{w.org}/rule-sets",
            json={"name": f"p7m-{w.run}-scratch-{tag}"},
            headers=me,
        ),
        201,
    )
    scratch_url = f"{VALIDATION}/rule-sets/{scratch.json()['id']}"
    saved = _ok(
        await gw.put(
            f"{scratch_url}/draft/rules/no-marker",
            content=json.dumps(_rule(w.marker)),
            headers={**me, "Content-Type": JSON, "If-Match": scratch.headers["etag"]},
        ),
        200,
    )
    _ok(await gw.post(f"{scratch_url}/draft/check", headers=me), 200)
    _ok(
        await gw.delete(
            f"{scratch_url}/draft/rules/no-marker",
            headers={**me, "If-Match": saved.headers["etag"]},
        ),
        200,
    )
    _ok(await gw.delete(scratch_url, headers=me), 204)
    shared = f"{VALIDATION}/rule-sets/{w.rule_set_id}"
    _ok(await gw.get(f"{shared}/versions", headers=me), 200)
    available = f"{VALIDATION}/folders/{w.team}/available-rule-sets"
    _ok(await gw.get(available, headers=me), 200)
    _ok(await gw.get(f"{available}/{w.rule_set_id}", headers=me), 200)
    _ok(await gw.get(f"{available}/{w.rule_set_id}/versions/1", headers=me), 200)
    _ok(await gw.post(f"{shared}/versions/1/deprecate", headers=me), 200)
