"""Fail-closed with the gateway bypassed (ROADMAP Phase 1 success criterion 5, T-01-58, T-01-59).

``internal(service)`` talks straight to a service on the compose network, so Traefik is not in
the path. Every service must authenticate and authorize on its own: the gateway performs no
authentication and no security control may live only there (CLAUDE.md).

Base URLs under test: http://identity:8000, http://contracts:8000, http://audit:8000.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa

pytestmark = pytest.mark.e2e

IDENTITY_URL = "http://identity:8000"
CONTRACTS_URL = "http://contracts:8000"
AUDIT_URL = "http://audit:8000"

FOLDERS = "/api/v1/identity/folders"
CONTRACTS = "/api/v1/contracts/contracts"
EVENTS = "/internal/v1/events"
AUTHZ_CHECK = "/internal/v1/authz/check"

# The issuers the services trust (compose.yaml, DC_AUTH_ISSUERS).
LOCAL_ISSUER = "http://identity:8000"
MOCK_ISSUER = "http://mock-oidc:8080/default"
AUDIENCE = "contractum"


def _yaml_contract(odcs_id: str) -> bytes:
    return f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: Bypass Orders
version: "1.0.0"
status: draft
schema:
  - name: orders
    properties:
      - name: order_id
        logicalType: string
""".encode()


def _assert_problem(
    response: httpx.Response, status: int, problem_type: str | None = None
) -> dict[str, Any]:
    assert response.status_code == status, f"{response.request.url}: {response.text}"
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    if problem_type is not None:
        assert body["type"] == problem_type
    return body


@dataclass(frozen=True)
class World:
    root_id: str
    team_id: str
    contract_id: str
    contract_bytes: bytes


@pytest_asyncio.fixture
async def world(gateway: httpx.AsyncClient, admin_token: str) -> World:
    """An organization chain with a team and one contract, created by the admin."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=headers)
    assert roots.status_code == 200, roots.text
    parent = str(roots.json()["items"][0]["id"])
    root_id = parent
    run = uuid.uuid4().hex[:10]
    for slug in (f"bypass-{run}", "payments", "ingest"):
        created = await gateway.post(
            FOLDERS,
            json={"parent_id": parent, "slug": slug, "display_name": slug},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        parent = str(created.json()["id"])
    raw = _yaml_contract(f"urn:datacontract:bypass:{uuid.uuid4()}")
    stored = await gateway.post(
        f"/api/v1/contracts/folders/{parent}/contracts",
        content=raw,
        headers={**headers, "Content-Type": "application/yaml"},
    )
    assert stored.status_code == 201, stored.text
    return World(
        root_id=root_id,
        team_id=parent,
        contract_id=str(stored.json()["contract"]["id"]),
        contract_bytes=raw,
    )


def test_the_bypass_clients_really_skip_the_gateway(
    internal: Callable[[str], httpx.AsyncClient],
) -> None:
    assert str(internal("identity").base_url).rstrip("/") == IDENTITY_URL
    assert str(internal("contracts").base_url).rstrip("/") == CONTRACTS_URL
    assert str(internal("audit").base_url).rstrip("/") == AUDIT_URL


async def test_every_service_answers_401_problem_json_without_a_token(
    internal: Callable[[str], httpx.AsyncClient], world: World
) -> None:
    identity, contracts, audit = internal("identity"), internal("contracts"), internal("audit")
    team = world.team_id
    contract = world.contract_id
    responses = [
        await identity.get(FOLDERS),
        await identity.post(AUTHZ_CHECK, json={"permission": "folder:read"}),
        await contracts.get(CONTRACTS),
        await contracts.post(
            f"/api/v1/contracts/folders/{team}/contracts",
            content=world.contract_bytes,
            headers={"Content-Type": "application/yaml"},
        ),
        await contracts.get(f"{CONTRACTS}/{contract}"),
        await audit.get(EVENTS),
    ]
    for response in responses:
        _assert_problem(response, 401, "urn:dc:problem:unauthenticated")


async def test_a_non_admin_gets_404_for_objects_addressed_by_id(
    internal: Callable[[str], httpx.AsyncClient], non_admin_token: str, world: World
) -> None:
    """D-08: a caller who cannot read the target gets the same 404 as for a missing object."""
    headers = {"Authorization": f"Bearer {non_admin_token}"}
    identity, contracts = internal("identity"), internal("contracts")
    for folder_id in (world.root_id, world.team_id):
        _assert_problem(
            await identity.get(f"{FOLDERS}/{folder_id}", headers=headers),
            404,
            "urn:dc:problem:not-found",
        )
    contract_url = f"{CONTRACTS}/{world.contract_id}"
    _assert_problem(await contracts.get(contract_url, headers=headers), 404)
    _assert_problem(await contracts.get(f"{contract_url}/document", headers=headers), 404)
    _assert_problem(
        await contracts.post(
            f"/api/v1/contracts/folders/{world.team_id}/contracts",
            content=_yaml_contract(f"urn:datacontract:bypass:{uuid.uuid4()}"),
            headers={**headers, "Content-Type": "application/yaml"},
        ),
        404,
        "urn:dc:problem:not-found",
    )


async def test_a_non_admin_gets_an_empty_page_from_list_and_search_and_403_from_audit(
    internal: Callable[[str], httpx.AsyncClient], non_admin_token: str, world: World
) -> None:
    """D-35: lists are trimmed to what the caller may see, so no grants means an empty 200."""
    headers = {"Authorization": f"Bearer {non_admin_token}"}
    empty = {"items": [], "next_cursor": None}
    folders = await internal("identity").get(FOLDERS, headers=headers)
    assert folders.status_code == 200 and folders.json() == empty
    contracts = internal("contracts")
    listed = await contracts.get(CONTRACTS, headers=headers)
    assert listed.status_code == 200 and listed.json() == empty
    searched = await contracts.get(CONTRACTS, params={"q": "Bypass"}, headers=headers)
    assert searched.status_code == 200 and searched.json() == empty
    # The audit read is not a list of the caller's own data: it stays admin-only.
    _assert_problem(
        await internal("audit").get(EVENTS, headers=headers), 403, "urn:dc:problem:forbidden"
    )


def _hs256_token_with_the_local_issuer() -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": LOCAL_ISSUER,
            "sub": "forger",
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 600,
            "ptype": "user",
        },
        "h" * 64,  # a throwaway shared secret, long enough for HS256
        algorithm="HS256",
        headers={"kid": "forged"},
    )


def _rs256_token_from_an_untrusted_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    return jwt.encode(
        {
            "iss": MOCK_ISSUER,
            "sub": "forger",
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 600,
            "ptype": "user",
            "roles": ["global_admin"],
        },
        key,
        algorithm="RS256",
        headers={"kid": "not-a-published-key"},
    )


async def test_forged_and_header_only_credentials_are_401_on_every_service(
    internal: Callable[[str], httpx.AsyncClient],
) -> None:
    attempts: list[dict[str, str]] = [
        {"Authorization": f"Bearer {_hs256_token_with_the_local_issuer()}"},
        {"Authorization": f"Bearer {_rs256_token_from_an_untrusted_key()}"},
        # Identity assertions in plain headers are never read: no token, no access.
        {"X-User": "admin"},
        {"X-User": "admin", "X-Forwarded-User": "admin", "X-Remote-User": "admin"},
    ]
    targets = (
        ("identity", FOLDERS),
        ("contracts", CONTRACTS),
        ("audit", EVENTS),
    )
    for headers in attempts:
        for service, path in targets:
            response = await internal(service).get(path, headers=headers)
            _assert_problem(response, 401, "urn:dc:problem:unauthenticated")


@pytest.mark.parametrize(
    ("service", "target"),
    [
        ("identity", "/api/v1/identity%2Ffolders"),
        ("identity", "/api/v1/identity/folders%5Cx"),
        ("identity", "/api/v1/identity/folders%00"),
        ("identity", "/api/v1/identity/folders%3Bx"),
        ("contracts", "/api/v1/contracts/contracts%2F..%2F..%2Finternal/v1/authz/check"),
        ("contracts", "/api/v1/contracts/contracts%2f..%2f..%2finternal/v1/authz/check"),
        ("audit", "/internal/v1%2Fevents"),
    ],
)
async def test_encoded_path_characters_are_rejected_by_the_service_itself(
    internal: Callable[[str], httpx.AsyncClient], service: str, target: str
) -> None:
    """The EncodedPathGuard holds without the gateway, and answers before authentication."""
    response = await internal(service).get(target)
    _assert_problem(response, 400, "urn:dc:problem:invalid-path")
