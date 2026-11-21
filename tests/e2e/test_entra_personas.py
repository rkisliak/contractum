"""AUTH-02 / W-5: the Entra-shaped mock issuer mints persona tokens with every required claim.

Plan 15's journey signs in as these personas, so this gate proves first that each token carries
the claim set ``dc_auth.issuers.ENTRA_REQUIRED_CLAIMS`` demands (mirrored here as a literal: the
e2e image carries no service code). The mock server adds ``iss``, ``iat``, ``nbf`` and ``exp``
itself; ``infra/mock-oidc/config.json`` supplies the rest through its ``entra`` issuer mappings.
The mapping is selected by the ``scope`` form parameter (the mock does not match ``client_id``);
every request sends both, as Plan 15's fixture does. The mock derives ``iss`` from ``Host`` and
``X-Forwarded-Proto``, so every in-network call sends ``localhost:8443`` and ``https``: the issuer
is then the browser-visible ``DC_E2E_ENTRA_ISSUER`` the platform trusts (Plan 08-12, A-1).
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest

pytestmark = pytest.mark.e2e

# Mirrors dc_auth.issuers.ENTRA_REQUIRED_CLAIMS.
ENTRA_REQUIRED_CLAIMS = ("exp", "iss", "aud", "iat", "nbf", "sub", "oid", "tid", "ver")
TENANT = "11111111-2222-4333-8444-555555555555"
API_AUDIENCE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
DATA_ENG_GROUP = "9a0f0000-0000-4000-8000-0000000000a1"
REDIRECT_URI = "https://localhost:8443/redirect.html"
OIDS = {
    "alice": "a11ce000-0000-4000-8000-000000000001",
    "bob": "b0b00000-0000-4000-8000-000000000002",
    "carol": "ca201000-0000-4000-8000-000000000003",
    "pipeline": "5e2b1ce0-0000-4000-8000-000000000004",
    "alice-idtoken": "a11ce000-0000-4000-8000-000000000001",
    "alice-wrong-aud": "a11ce000-0000-4000-8000-000000000001",
}


ISSUER_HEADERS = {"Host": "localhost:8443", "X-Forwarded-Proto": "https"}


def _expected_issuer() -> str:
    value = os.environ.get("DC_E2E_ENTRA_ISSUER")
    if not value:
        raise RuntimeError("DC_E2E_ENTRA_ISSUER is not set; e2e tests run through scripts/e2e.sh")
    return value.rstrip("/")


def _issuer_url() -> str:
    value = os.environ.get("DC_E2E_ENTRA_OIDC_URL")
    if not value:
        raise RuntimeError("DC_E2E_ENTRA_OIDC_URL is not set; e2e tests run through scripts/e2e.sh")
    return value.rstrip("/")


def _claims(persona: str) -> dict[str, Any]:
    url = _issuer_url() + "/token"
    last: httpx.Response | None = None
    for _ in range(30):
        try:
            last = httpx.post(
                url,
                headers=ISSUER_HEADERS,
                data={
                    "grant_type": "client_credentials",
                    "client_id": persona,
                    "client_secret": "dev",
                    "scope": persona,
                },
                timeout=10.0,
            )
        except httpx.TransportError:
            last = None
        if last is not None and last.status_code == 200:
            break
        time.sleep(1.0)
    assert last is not None and last.status_code == 200, f"no token for persona {persona}"
    decoded: dict[str, Any] = jwt.decode(
        last.json()["access_token"], options={"verify_signature": False}
    )
    return decoded


@pytest.mark.parametrize("persona", ["alice", "pipeline"])
def test_persona_token_carries_every_claim_the_entra_mapper_requires(persona: str) -> None:
    claims = _claims(persona)
    missing = [name for name in ENTRA_REQUIRED_CLAIMS if name not in claims]
    assert not missing, f"{persona} token lacks {missing}"
    assert claims["ver"] == "2.0"
    assert claims["tid"] == TENANT
    assert claims["oid"] == OIDS[persona]
    assert claims["iss"] == _expected_issuer()
    assert claims["aud"] == [API_AUDIENCE] or claims["aud"] == API_AUDIENCE


def test_user_persona_has_a_delegated_scope_and_the_app_persona_does_not() -> None:
    alice = _claims("alice")
    assert alice["scp"] == "access_as_user"
    assert alice.get("idtyp") != "app"
    pipeline = _claims("pipeline")
    assert pipeline["idtyp"] == "app"
    assert "scp" not in pipeline
    assert pipeline["roles"] == ["Contracts.Access"]


def test_personas_have_distinct_objects_selected_by_scope() -> None:
    alice, bob = _claims("alice"), _claims("bob")
    assert alice["oid"] != bob["oid"]
    assert (alice["oid"], bob["oid"]) == (OIDS["alice"], OIDS["bob"])


def test_id_token_shaped_and_wrong_audience_personas_differ_as_intended() -> None:
    idtoken = _claims("alice-idtoken")
    assert "scp" not in idtoken
    assert idtoken["oid"] == OIDS["alice"]
    wrong = _claims("alice-wrong-aud")
    assert wrong["scp"] == "access_as_user"
    assert API_AUDIENCE not in (wrong["aud"] if isinstance(wrong["aud"], list) else [wrong["aud"]])


async def test_entra_discovery_document_through_the_gateway_reports_the_browser_visible_issuer(
    gateway: httpx.AsyncClient,
) -> None:
    response = await gateway.get(
        "/entra/.well-known/openid-configuration", headers={"Host": "localhost:8443"}
    )
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["issuer"] == _expected_issuer()
    assert document["jwks_uri"].startswith(_expected_issuer() + "/")
    # The dev router sits behind the same transport headers as every other gateway route.
    assert "strict-transport-security" in response.headers


async def _register_data_eng_group(gateway: httpx.AsyncClient, admin_token: str) -> None:
    """Register the data-eng Entra group at ROOT (409 means an earlier run already did)."""
    admin = {"Authorization": f"Bearer {admin_token}"}
    roots = await gateway.get("/api/v1/identity/folders", params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    created = await gateway.post(
        "/api/v1/identity/entra/groups",
        json={"owner_folder_id": root["id"], "object_id": DATA_ENG_GROUP},
        headers=admin,
    )
    assert created.status_code in (200, 201, 409), created.text


async def test_the_spa_persona_completes_authorization_code_pkce_and_identity_accepts_it(
    gateway: httpx.AsyncClient, admin_token: str
) -> None:
    """A public SPA client (``ui-alice``) runs code + PKCE through the gateway; no redirects are
    followed and the JWT payload is decoded without verification only to read its claims. The
    persona comes from the ``client_id`` mapping (A-3), so no persona scope is sent."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    browser = {"Host": "localhost:8443"}

    authorize = await gateway.get(
        "/entra/authorize",
        params={
            "client_id": "ui-alice",
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": "openid profile offline_access",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "nonce": nonce,
        },
        headers=browser,
        follow_redirects=False,
    )
    assert authorize.status_code == 302, authorize.text
    location = urlsplit(authorize.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT_URI
    query = parse_qs(location.query)
    assert query["state"] == [state]
    assert query["code"] and query["code"][0]

    exchange = await gateway.post(
        "/entra/token",
        headers=browser,
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "code_verifier": verifier,
            "client_id": "ui-alice",
            "redirect_uri": REDIRECT_URI,
        },
    )
    assert exchange.status_code == 200, exchange.text
    body = exchange.json()
    assert body["access_token"] and body["id_token"]

    claims: dict[str, Any] = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["iss"] == _expected_issuer()
    assert claims["tid"] == TENANT
    assert claims["oid"] == OIDS["alice"]

    await _register_data_eng_group(gateway, admin_token)
    me = await gateway.get(
        "/api/v1/identity/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["principal_type"] == "entra_user"
