"""The Phase 3 goal, end to end through the gateway (ROADMAP Phase 3 success criteria).

"Contract owners take a contract from an editable draft to immutable published versions (Latest,
Preview, older, deprecated, unpublished), and any authorized consumer can fetch exactly the
version they need."

One journey with real local users: an editor (Team editor) edits a draft under ``If-Match``,
publishes 1.0.0, starts 1.1.0 from it, promotes it, deprecates and unpublishes 1.0.0; a Consumer
fetches by ``latest`` and by SemVer and revalidates with ``If-None-Match``. Everything goes
through https://traefik (so 304, 412, 428, ``ETag``, ``Deprecation`` and ``Sunset`` must survive
the replaceable gateway); only the audit read uses the internal audit service.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

FOLDERS = "/api/v1/identity/folders"
CONTRACTS = "/api/v1/contracts/contracts"
ALLOWED_ORIGIN = "http://localhost:5173"
YAML = "application/yaml"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _contract_yaml(odcs_id: str, purpose: str) -> bytes:
    # A comment and odd spacing make byte-exactness through the gateway visible.
    text = f"""# Phase 3 journey: comments must survive byte for byte.
kind: DataContract
id: {odcs_id}   # inline comment
apiVersion: v3.2.0
name:   Phase 3 Orders
version: "1.0.0"
status: active
domain: lifecycle
tags: [phase3, orders]
description:
  purpose: {purpose}
schema:
  - name: orders
    physicalType: table
    properties:
      - name: order_id
        logicalType: string
"""
    return text.encode("utf-8")


async def _folder(
    gateway: httpx.AsyncClient, headers: dict[str, str], parent_id: str, slug: str
) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Phase 3 {slug}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _grant(
    gateway: httpx.AsyncClient,
    admin_token: str,
    folder_id: str,
    principal_id: str,
    role: str,
) -> None:
    response = await gateway.post(
        f"{FOLDERS}/{folder_id}/grants",
        json={"principal_type": "user", "principal_id": principal_id, "role": role},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 201, response.text


async def _audit_events(
    audit: httpx.AsyncClient, headers: dict[str, str], aggregate_id: str, count: int
) -> list[dict[str, Any]] | None:
    response = await audit.get(
        "/internal/v1/events", params={"aggregate_id": aggregate_id}, headers=headers
    )
    response.raise_for_status()
    events: list[dict[str, Any]] = response.json()
    return events if len(events) >= count else None


def _problem_type(response: httpx.Response) -> str:
    assert response.headers["content-type"].startswith("application/problem+json"), (
        response.headers,
        response.text,
    )
    return str(response.json()["type"])


async def test_the_whole_version_lifecycle_works_for_real_users_through_the_gateway(
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
    run = uuid.uuid4().hex[:10]
    org = await _folder(gateway, admin, root["id"], f"p3-{run}")
    project = await _folder(gateway, admin, org, "payments")
    team = await _folder(gateway, admin, project, "ingest")

    editor = await users.create("p3ed")
    consumer = await users.create("p3cs")
    await _grant(gateway, admin_token, team, editor.principal_id, "team_editor")
    await _grant(gateway, admin_token, team, consumer.principal_id, "consumer")
    ed = _bearer(await users.token(editor))
    cs = _bearer(await users.token(consumer))

    # --- editor: create, edit under If-Match, publish 1.0.0 ---------------------------------
    odcs_id = f"urn:datacontract:phase3:{uuid.uuid4()}"
    created = await gateway.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=_contract_yaml(odcs_id, "First draft."),
        headers={**ed, "Content-Type": YAML},
    )
    assert created.status_code == 201, created.text
    contract_id = str(created.json()["contract"]["id"])
    draft = created.json()["draft"]
    draft_id = str(draft["id"])
    assert draft["etag"] == '"r1"'
    drafts_url = f"{CONTRACTS}/{contract_id}/drafts/{draft_id}"

    # A published-only reader sees nothing of a contract that has no Latest yet.
    hidden = await gateway.get(f"{CONTRACTS}/{contract_id}/versions/latest", headers=cs)
    assert hidden.status_code == 404, hidden.text

    raw_v1 = _contract_yaml(odcs_id, "Edited under If-Match, published as 1.0.0.")
    edited = await gateway.put(
        drafts_url, content=raw_v1, headers={**ed, "Content-Type": YAML, "If-Match": '"r1"'}
    )
    assert edited.status_code == 200, edited.text
    assert edited.headers["etag"] == '"r2"'
    assert edited.json()["draft"]["revision"] == 2

    # 412 / 428 are the service's own problems: the gateway re-renders only 413, 429 and 5xx.
    stale = await gateway.put(
        drafts_url, content=raw_v1, headers={**ed, "Content-Type": YAML, "If-Match": '"r1"'}
    )
    assert stale.status_code == 412, stale.text
    assert _problem_type(stale) == "urn:dc:problem:precondition-failed"
    missing = await gateway.put(drafts_url, content=raw_v1, headers={**ed, "Content-Type": YAML})
    assert missing.status_code == 428, missing.text
    assert _problem_type(missing) == "urn:dc:problem:precondition-required"

    published = await gateway.post(f"{drafts_url}/publish", headers={**ed, "If-Match": '"r2"'})
    assert published.status_code == 201, published.text
    assert published.json()["semver"] == "1.0.0"
    assert published.json()["status"] == "latest"

    # --- consumer: fetch, byte identity, revalidation ---------------------------------------
    fetched = await gateway.get(f"{CONTRACTS}/{contract_id}/versions/latest", headers=cs)
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == raw_v1
    etag = fetched.headers["etag"]
    assert etag == published.json()["etag"]
    assert fetched.headers["cache-control"] == "private, no-cache"

    revalidated = await gateway.get(
        f"{CONTRACTS}/{contract_id}/versions/latest", headers={**cs, "If-None-Match": etag}
    )
    assert revalidated.status_code == 304, revalidated.text
    assert revalidated.content == b""
    assert revalidated.headers["etag"] == etag
    cache_control = {p.strip() for p in revalidated.headers["cache-control"].split(",")}
    assert cache_control == {"private", "no-cache"}

    # --- editor: 1.1.0 from 1.0.0 as a Preview, then promote ---------------------------------
    started = await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts", json={"from_version": "1.0.0"}, headers=ed
    )
    assert started.status_code == 201, started.text
    assert started.json()["draft"]["declared_version"] == "1.1.0"
    draft2 = started.json()["draft"]
    publish_2 = await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts/{draft2['id']}/publish",
        headers={**ed, "If-Match": draft2["etag"]},
    )
    assert publish_2.status_code == 201, publish_2.text
    assert publish_2.json()["semver"] == "1.1.0"
    assert publish_2.json()["status"] == "preview"
    still_latest = await gateway.get(f"{CONTRACTS}/{contract_id}/versions/latest", headers=cs)
    assert still_latest.status_code == 200
    assert still_latest.content == raw_v1  # a Preview does not move Latest

    promoted = await gateway.post(f"{CONTRACTS}/{contract_id}/versions/1.1.0/promote", headers=ed)
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["status"] == "latest"
    now_latest = await gateway.get(
        f"{CONTRACTS}/{contract_id}/versions/latest/metadata", headers=cs
    )
    assert now_latest.status_code == 200, now_latest.text
    assert now_latest.json()["semver"] == "1.1.0"

    # --- deprecate 1.0.0: headers reach the client and the browser may read them -------------
    sunset = datetime.now(UTC).date() + timedelta(days=30)
    deprecated = await gateway.post(
        f"{CONTRACTS}/{contract_id}/versions/1.0.0/deprecate",
        json={"sunset_date": sunset.isoformat()},
        headers=ed,
    )
    assert deprecated.status_code == 200, deprecated.text
    assert deprecated.json()["deprecated"] is True
    old = await gateway.get(
        f"{CONTRACTS}/{contract_id}/versions/1.0.0", headers={**cs, "Origin": ALLOWED_ORIGIN}
    )
    assert old.status_code == 200, old.text
    assert old.content == raw_v1
    assert re.fullmatch(r"@\d+", old.headers["deprecation"]), old.headers
    assert old.headers["sunset"].endswith("23:59:59 GMT"), old.headers
    assert old.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    exposed = {h.strip().lower() for h in old.headers["access-control-expose-headers"].split(",")}
    assert {"etag", "deprecation", "sunset"} <= exposed
    # A deprecation changes metadata, not bytes: the 304 refreshes it.
    old_not_modified = await gateway.get(
        f"{CONTRACTS}/{contract_id}/versions/1.0.0",
        headers={**cs, "If-None-Match": old.headers["etag"]},
    )
    assert old_not_modified.status_code == 304
    assert old_not_modified.headers["deprecation"] == old.headers["deprecation"]
    assert old_not_modified.headers["sunset"] == old.headers["sunset"]

    # --- unpublish 1.0.0: consumers get 404, the editor still sees it ------------------------
    unpublished = await gateway.post(
        f"{CONTRACTS}/{contract_id}/versions/1.0.0/unpublish", headers=ed
    )
    assert unpublished.status_code == 200, unpublished.text
    assert unpublished.json()["lifecycle"] == "unpublished"
    gone = await gateway.get(f"{CONTRACTS}/{contract_id}/versions/1.0.0", headers=cs)
    assert gone.status_code == 404, gone.text
    assert _problem_type(gone) == "urn:dc:problem:not-found"
    editor_view = await gateway.get(f"{CONTRACTS}/{contract_id}/versions/1.0.0", headers=ed)
    assert editor_view.status_code == 200, editor_view.text
    consumer_versions = await gateway.get(f"{CONTRACTS}/{contract_id}/versions", headers=cs)
    assert consumer_versions.status_code == 200, consumer_versions.text
    assert [v["semver"] for v in consumer_versions.json()["items"]] == ["1.1.0"]

    # --- the consumer's list shows the contract with its Latest ------------------------------
    listing = await gateway.get(CONTRACTS, headers=cs)
    assert listing.status_code == 200, listing.text
    items = {item["id"]: item for item in listing.json()["items"]}
    assert items[contract_id]["latest_version"]["semver"] == "1.1.0"

    # --- audit: every lifecycle write reached the audit service ------------------------------
    expected = [
        "contract.created",
        "contract.draft_updated",
        "contract.version_published",
        "contract.draft_created",
        "contract.version_published",
        "contract.version_promoted",
        "contract.version_deprecated",
        "contract.version_unpublished",
    ]
    events = await wait_for(
        lambda: _audit_events(internal("audit"), admin, contract_id, len(expected)),
        30.0,
        description=f"{len(expected)} audit events for {contract_id}",
    )
    actions = sorted(event["action"] for event in events)
    assert actions == sorted(expected), actions
    assert {event["actor"]["id"] for event in events} == {editor.principal_id}
    # Metadata only: no document content in any event.
    assert "Edited under If-Match" not in str(events)
