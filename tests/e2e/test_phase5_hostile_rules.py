"""The Phase 5 memory exit gate on the Linux stack, through the gateway (D-137, D-141, RULE-09).

The validation container pins the rule worker's address-space limit to 1 GiB
(``DC_WORKER_MEMORY_BYTES`` in compose.yaml). A rule whose fixture builds a 4 GiB string cannot
finish under it, so the worker raises ``MemoryError`` (a tenth of a second of user CPU, inside the
500 ms per-rule CPU allowance) and the refusal names ``memory_exceeded``, never ``timeout``. The
sandbox judges a worker by its user CPU time (system time is page-fault work that depends on the
host), so a host that is merely slow cannot turn the refusal into
``timeout`` (WR-01); only a worker that burns its CPU allowance does. The service keeps answering
afterwards.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

FOLDERS = "/api/v1/identity/folders"
VALIDATION = "/api/v1/validation"
# 370 bytes, 11,329 compiled bytes: it passes every static limit and reaches the fixtures. The
# 24-chain finishes in 0.22 s at 1 GiB, so it is not a memory case; the 28-chain needs 4 GiB.
STRING_DOUBLING = "['aaaaaaaaaaaaaaaa']" + ".map(s, s+s)" * 28 + "[0].size() > 0"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _folder(
    gateway: httpx.AsyncClient, headers: dict[str, str], parent_id: str, slug: str
) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Phase 5 {slug}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _grant(
    gateway: httpx.AsyncClient, admin_token: str, folder_id: str, principal_id: str, role: str
) -> None:
    response = await gateway.post(
        f"{FOLDERS}/{folder_id}/grants",
        json={"principal_type": "user", "principal_id": principal_id, "role": role},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 201, response.text


async def test_a_memory_blow_up_in_a_rule_fixture_is_refused_with_memory_exceeded(
    gateway: httpx.AsyncClient, admin_token: str, users: Any
) -> None:
    admin = _bearer(admin_token)
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    run = uuid.uuid4().hex[:10]
    org = await _folder(gateway, admin, root["id"], f"p5-{run}")  # never anything at ROOT
    project = await _folder(gateway, admin, org, "payments")
    team = await _folder(gateway, admin, project, "ingest")

    manager = await users.create("p5pm")
    editor = await users.create("p5ed")
    await _grant(gateway, admin_token, org, manager.principal_id, "policy_manager")
    await _grant(gateway, admin_token, team, editor.principal_id, "team_editor")
    pm = _bearer(await users.token(manager))
    ed = _bearer(await users.token(editor))

    created = await gateway.post(
        f"{VALIDATION}/folders/{org}/rule-sets", json={"name": "Memory bomb"}, headers=pm
    )
    assert created.status_code == 201, created.text
    rule_set_id = created.json()["id"]
    fixture = {"document": {"x": 1}}
    draft = {
        "rules": [
            {
                "key": "string-doubling",
                "target": "$",
                "condition": STRING_DOUBLING,
                "severity": "error",
                "message": "Never satisfied.",
                "fixtures": {"pass": [fixture], "fail": [fixture]},
            }
        ]
    }
    saved = await gateway.put(
        f"{VALIDATION}/rule-sets/{rule_set_id}/draft",
        content=json.dumps(draft),
        headers={**pm, "Content-Type": "application/yaml", "If-Match": '"r0"'},
    )
    assert saved.status_code == 200, saved.text

    started = time.monotonic()
    refused = await gateway.post(
        f"{VALIDATION}/rule-sets/{rule_set_id}/publish",
        headers={**pm, "If-Match": saved.headers["etag"]},
    )
    elapsed = time.monotonic() - started

    assert refused.status_code == 422, refused.text
    assert elapsed < 10  # bounded; the deadline stopped nothing, the limit did
    body = refused.json()
    problems = [p for rule in body["check"]["rules"] for p in rule["problems"]]
    fail_fixture = next(
        p for p in problems if p["code"] == "fixture_misbehaved" and p["fixture_kind"] == "fail"
    )
    assert fail_fixture["error_code"] == "memory_exceeded"  # a "timeout" fails the test

    # The service keeps answering: an on-demand validation and the OpenAPI document.
    document = (
        "kind: DataContract\napiVersion: v3.2.0\nid: urn:datacontract:p5:" + run + "\n"
        'version: "1.0.0"\nstatus: draft\n'
    )
    started = time.monotonic()
    validated = await gateway.post(
        f"{VALIDATION}/folders/{team}/validate",
        content=document,
        headers={**ed, "Content-Type": "application/yaml"},
    )
    assert validated.status_code == 200, validated.text
    assert time.monotonic() - started < 3
    openapi = await gateway.get(f"{VALIDATION}/openapi.json")
    assert openapi.status_code == 200, openapi.text
