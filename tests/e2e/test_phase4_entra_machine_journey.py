"""The Phase 4 goal, end to end through the gateway (ROADMAP Phase 4 success criteria).

"People sign in with Azure Entra ID, and pipelines authenticate as service principals, personal
access tokens or service accounts. Each gets exactly what its grants and groups allow, and loses
it immediately on deactivation."

One journey on the compose stack, every call through https://traefik except the audit read and
the fake-Graph mutators (backend network, like the other e2e tests):

* an Entra user admitted through a registered Entra group (team_editor grant) creates and
  publishes a contract; the audit trail names ``entra:<tid>:<oid>``; an unregistered Entra user is
  403 ``principal-not-registered`` from contracts; an ID-token-shaped and a wrong-audience token
  are 401; a user in 251 groups is admitted (AUTH-02, AUTH-04);
* a registered Entra service principal with a consumer grant fetches Latest; a PAT works until it
  is revoked; a read-only PAT cannot write; a service account (local group) rotates its keys
  and the revoked key stops at once, a third key is refused (AUTH-05, AUTH-06, AUTH-07);
* deactivating a local user stops its access JWT and PAT on the next request, and disabling an
  Entra user in the directory stops its token within the directory cache window (AUTH-08).

``contracts`` contains no Entra or credential code (AUTH-03): it serves every step. The journey
is re-runnable on a warm stack (unique slugs and names, registration reuse on 409, the fake
directory reset at start and end and in the fixture's finalizer).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from conftest import FakeGraph

pytestmark = pytest.mark.e2e

# Mirrors conftest.ENTRA_TENANT / ENTRA_PERSONAS (tests/e2e has no package, so a test module
# does not import its conftest); both mirror the Plan 04-07 seeds.
ENTRA_TENANT = "11111111-2222-4333-8444-555555555555"
ENTRA_DATA_ENG_GROUP = "9a0f0000-0000-4000-8000-0000000000a1"
ENTRA_PERSONAS = {
    "alice": "a11ce000-0000-4000-8000-000000000001",
    "pipeline": "5e2b1ce0-0000-4000-8000-000000000004",
}

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
CONTRACTS = "/api/v1/contracts/contracts"
YAML = "application/yaml"
# The Entra directory cache is 60 s in the dev stack; a disabled account is refused after that.
DIRECTORY_CACHE_WAIT_S = 90.0
ALICE_ACTOR = f"entra:{ENTRA_TENANT}:{ENTRA_PERSONAS['alice']}"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _expiry(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _contract_yaml(odcs_id: str, name: str) -> bytes:
    text = f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: {name}
version: "1.0.0"
status: draft
domain: phase4
tags: [phase4, identities]
description:
  purpose: Phase 4 journey.
schema:
  - name: orders
    physicalType: table
    properties:
      - name: order_id
        logicalType: string
"""
    return text.encode("utf-8")


def _problem_type(response: httpx.Response) -> str:
    assert response.headers["content-type"].startswith("application/problem+json"), (
        response.headers,
        response.text,
    )
    return str(response.json()["type"])


async def _folder(
    gateway: httpx.AsyncClient, headers: dict[str, str], parent_id: str, slug: str
) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Phase 4 {slug}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _grant(
    gateway: httpx.AsyncClient,
    headers: dict[str, str],
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
    gateway: httpx.AsyncClient,
    headers: dict[str, str],
    path: str,
    owner_folder_id: str,
    object_id: str,
) -> str:
    """Register a group or service principal; on 409 (an earlier run) reuse the registration."""
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


async def _audit_events(
    audit: httpx.AsyncClient, headers: dict[str, str], aggregate_id: str, action: str
) -> list[dict[str, Any]] | None:
    response = await audit.get(
        "/internal/v1/events", params={"aggregate_id": aggregate_id}, headers=headers
    )
    response.raise_for_status()
    events: list[dict[str, Any]] = [e for e in response.json() if e["action"] == action]
    return events or None


async def test_entra_users_machine_credentials_and_deactivation_through_the_gateway(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    admin_token: str,
    users: Any,
    entra_token: Callable[[str], Awaitable[str]],
    fake_graph: FakeGraph,
    wait_for: Callable[..., Any],
) -> None:
    await fake_graph.reset()
    admin = _bearer(admin_token)
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    run = uuid.uuid4().hex[:10]
    org = await _folder(gateway, admin, root["id"], f"p4-{run}")
    project = await _folder(gateway, admin, org, "payments")
    team = await _folder(gateway, admin, project, "ingest")

    # --- setup: register the Entra group and the service principal, then grant them ---------
    await _register(gateway, admin, f"{IDENTITY}/entra/groups", root["id"], ENTRA_DATA_ENG_GROUP)
    await _grant(gateway, admin, team, "entra_group", ENTRA_DATA_ENG_GROUP, "team_editor")
    pipeline_oid = ENTRA_PERSONAS["pipeline"]
    await _register(
        gateway, admin, f"{IDENTITY}/entra/service-principals", root["id"], pipeline_oid
    )
    await _grant(
        gateway,
        admin,
        team,
        "service_principal",
        f"entra:{ENTRA_TENANT}:{pipeline_oid}",
        "consumer",
    )

    # --- alice (data-eng) creates a contract; the audit trail names her Entra principal ------
    alice = _bearer(await entra_token("alice"))
    odcs_id = f"urn:datacontract:phase4:{uuid.uuid4()}"
    created = await gateway.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=_contract_yaml(odcs_id, "Phase 4 Orders"),
        headers={**alice, "Content-Type": YAML},
    )
    assert created.status_code == 201, created.text
    contract_id = str(created.json()["contract"]["id"])
    draft = created.json()["draft"]
    contract_url = f"{CONTRACTS}/{contract_id}"
    latest_url = f"{contract_url}/versions/latest"

    events = await wait_for(
        lambda: _audit_events(internal("audit"), admin, contract_id, "contract.created"),
        30.0,
        description=f"the contract.created audit event for {contract_id}",
    )
    assert {event["actor"]["id"] for event in events} == {ALICE_ACTOR}, events

    # --- who is let in and who is not --------------------------------------------------------
    bob = await gateway.get(contract_url, headers=_bearer(await entra_token("bob")))
    assert bob.status_code == 403, bob.text
    assert _problem_type(bob) == "urn:dc:problem:principal-not-registered"
    idtoken = await gateway.get(contract_url, headers=_bearer(await entra_token("alice-idtoken")))
    assert idtoken.status_code == 401, idtoken.text
    wrong_aud = await gateway.get(
        contract_url, headers=_bearer(await entra_token("alice-wrong-aud"))
    )
    assert wrong_aud.status_code == 401, wrong_aud.text
    carol = await gateway.get(contract_url, headers=_bearer(await entra_token("carol")))
    assert carol.status_code == 200, carol.text  # 251 groups, paged through the directory

    # --- alice publishes; the service principal fetches Latest --------------------------------
    published = await gateway.post(
        f"{contract_url}/drafts/{draft['id']}/publish",
        headers={**alice, "If-Match": draft["etag"]},
    )
    assert published.status_code == 201, published.text
    assert published.json()["status"] == "latest"
    pipeline = await gateway.get(latest_url, headers=_bearer(await entra_token("pipeline")))
    assert pipeline.status_code == 200, pipeline.text

    # --- PATs: work until revoked; a read-only PAT cannot write -------------------------------
    pat = await gateway.post(
        f"{IDENTITY}/pats", json={"name": f"p4-{run}", "expires_at": _expiry()}, headers=alice
    )
    assert pat.status_code == 201, pat.text
    pat_token = str(pat.json()["token"])
    assert pat_token.startswith("dcp_")
    assert (await gateway.get(latest_url, headers=_bearer(pat_token))).status_code == 200
    revoked = await gateway.delete(f"{IDENTITY}/pats/{pat.json()['id']}", headers=alice)
    assert revoked.status_code == 204, revoked.text
    assert (await gateway.get(latest_url, headers=_bearer(pat_token))).status_code == 401

    read_only = await gateway.post(
        f"{IDENTITY}/pats",
        json={"name": f"p4-ro-{run}", "expires_at": _expiry(), "read_only": True},
        headers=alice,
    )
    assert read_only.status_code == 201, read_only.text
    assert read_only.json()["read_only"] is True
    ro_token = str(read_only.json()["token"])
    assert (await gateway.get(latest_url, headers=_bearer(ro_token))).status_code == 200
    attempt = await gateway.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=_contract_yaml(f"urn:datacontract:phase4:{uuid.uuid4()}", "Read-only attempt"),
        headers={**_bearer(ro_token), "Content-Type": YAML},
    )
    assert attempt.status_code in (403, 404), attempt.text  # never 201
    await gateway.delete(f"{IDENTITY}/pats/{read_only.json()['id']}", headers=alice)

    # --- a service account in a local group: two keys, rotation, a third is refused -----------
    account = await gateway.post(
        f"{IDENTITY}/service-accounts",
        json={"owner_folder_id": root["id"], "name": f"p4-sa-{run}"},
        headers=admin,
    )
    assert account.status_code == 201, account.text
    account_id = str(account.json()["id"])
    group = await gateway.post(
        f"{IDENTITY}/groups",
        json={"owner_folder_id": root["id"], "name": f"p4-readers-{run}"},
        headers=admin,
    )
    assert group.status_code == 201, group.text
    group_id = str(group.json()["id"])
    member = await gateway.post(
        f"{IDENTITY}/groups/{group_id}/members",
        json={"principal_id": account.json()["principal_id"]},
        headers=admin,
    )
    assert member.status_code == 201, member.text
    await _grant(gateway, admin, team, "local_group", group_id, "consumer")
    keys_url = f"{IDENTITY}/service-accounts/{account_id}/keys"
    key1 = await gateway.post(
        keys_url, json={"name": "key-1", "expires_at": _expiry()}, headers=admin
    )
    key2 = await gateway.post(
        keys_url, json={"name": "key-2", "expires_at": _expiry()}, headers=admin
    )
    assert key1.status_code == 201, key1.text
    assert key2.status_code == 201, key2.text
    for key in (key1, key2):
        got = await gateway.get(latest_url, headers=_bearer(str(key.json()["token"])))
        assert got.status_code == 200, got.text
    key3 = await gateway.post(
        keys_url, json={"name": "key-3", "expires_at": _expiry()}, headers=admin
    )
    assert key3.status_code == 409, key3.text
    assert _problem_type(key3) == "urn:dc:problem:credential-limit-reached"
    dropped = await gateway.delete(f"{keys_url}/{key1.json()['id']}", headers=admin)
    assert dropped.status_code == 204, dropped.text
    assert (
        await gateway.get(latest_url, headers=_bearer(str(key1.json()["token"])))
    ).status_code == 401
    assert (
        await gateway.get(latest_url, headers=_bearer(str(key2.json()["token"])))
    ).status_code == 200

    # --- deactivating a local user stops its access JWT and PAT at once ----------------------
    reader = await users.create("p4rd")
    await _grant(gateway, admin, team, "user", reader.principal_id, "team_reader")
    reader_jwt = _bearer(await users.token(reader))
    reader_pat = await gateway.post(
        f"{IDENTITY}/pats",
        json={"name": f"p4-rd-{run}", "expires_at": _expiry()},
        headers=reader_jwt,
    )
    assert reader_pat.status_code == 201, reader_pat.text
    reader_pat_header = _bearer(str(reader_pat.json()["token"]))
    assert (await gateway.get(contract_url, headers=reader_jwt)).status_code == 200
    assert (await gateway.get(contract_url, headers=reader_pat_header)).status_code == 200
    deactivated = await gateway.post(f"{IDENTITY}/users/{reader.id}/deactivate", headers=admin)
    assert deactivated.status_code == 200, deactivated.text
    assert (await gateway.get(contract_url, headers=reader_jwt)).status_code == 401
    assert (await gateway.get(contract_url, headers=reader_pat_header)).status_code == 401

    # --- disabling the Entra user in the directory ends her access within the cache window ---
    await fake_graph.set_principal(ENTRA_PERSONAS["alice"], accountEnabled=False)

    async def alice_refused() -> bool:
        return (await gateway.get(contract_url, headers=alice)).status_code == 401

    await wait_for(
        alice_refused,
        DIRECTORY_CACHE_WAIT_S,
        interval_s=2.0,
        description="the disabled Entra user's token to be refused",
    )

    # Leave the shared directory as found: reset, and wait until alice is admitted again so a
    # run started straight after this one (or another e2e test) does not hit the cached answer.
    await fake_graph.reset()

    async def alice_admitted() -> bool:
        return (await gateway.get(contract_url, headers=alice)).status_code == 200

    await wait_for(
        alice_admitted,
        DIRECTORY_CACHE_WAIT_S,
        interval_s=2.0,
        description="the re-enabled Entra user to be admitted again",
    )


PIPELINE_APP_ID = "a99b1ce0-0000-4000-8000-000000000041"  # the fake directory's Pipeline appId


async def test_directory_search_through_the_gateway(
    gateway: httpx.AsyncClient,
    admin_token: str,
    fake_graph: FakeGraph,
) -> None:
    """Quick 261119-ofa: the admin directory search, through the gateway against the fake Graph."""
    await fake_graph.reset()
    admin = _bearer(admin_token)
    search = f"{IDENTITY}/entra/directory/search"
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]["id"]

    by_name = await gateway.get(
        search,
        params={"kind": "service_principal", "q": "Pipeline", "owner_folder_id": root},
        headers=admin,
    )
    assert by_name.status_code == 200, by_name.text
    assert ENTRA_PERSONAS["pipeline"] in {i["object_id"] for i in by_name.json()["items"]}
    assert by_name.json()["truncated"] is False

    by_app_id = await gateway.get(
        search,
        params={"kind": "service_principal", "q": PIPELINE_APP_ID, "owner_folder_id": root},
        headers=admin,
    )
    assert by_app_id.status_code == 200, by_app_id.text
    items = by_app_id.json()["items"]
    assert [i["object_id"] for i in items] == [ENTRA_PERSONAS["pipeline"]]
    assert items[0]["secondary"] == PIPELINE_APP_ID

    users = await gateway.get(search, params={"kind": "user", "q": "Searchable"}, headers=admin)
    assert users.status_code == 200, users.text
    assert len(users.json()["items"]) == 5
    assert all(i["display_name"].startswith("Searchable ") for i in users.json()["items"])

    group = await gateway.get(
        search, params={"kind": "group", "q": "Searchable", "owner_folder_id": root}, headers=admin
    )
    assert group.status_code == 200, group.text
    assert [i["display_name"] for i in group.json()["items"]] == ["Searchable Analysts"]

    short = await gateway.get(search, params={"kind": "user", "q": "a"}, headers=admin)
    assert short.status_code == 422, short.text
    anonymous = await gateway.get(search, params={"kind": "user", "q": "Searchable"})
    assert anonymous.status_code == 401, anonymous.text
