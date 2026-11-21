"""Entra v2 access-token matrix (AUTH-02, AUTH-03, T-04-01, T-04-03, T-04-06).

Every malformed token is the same generic 401 and never echoes the token. Claims grant nothing:
the principal's type comes from the token's shape, its scopes from the verifier, and its key from
``(tid, oid)`` only.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from dc_auth.issuers import IssuerRegistry
from dc_auth.jwks import JwksCache
from dc_auth.principal import Principal
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer
from dc_auth.verify import TokenVerifier
from dc_core.http import make_client
from dc_core.problems import ProblemError

pytestmark = pytest.mark.unit

TENANT_B = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OID = "a1b2c3d4-2222-4333-8444-55555555abcd"


def _verifier(fake: FakeIssuer, *extra: Any) -> tuple[TokenVerifier, httpx.AsyncClient]:
    http = make_client()
    registry = IssuerRegistry([fake.entra_issuer_config(), fake.issuer_config(), *extra])
    return TokenVerifier(registry, JwksCache(http)), http


async def _expect_401(verifier: TokenVerifier, token: str) -> None:
    with pytest.raises(ProblemError) as info:
        await verifier.verify(token)
    err = info.value
    assert err.status == 401
    assert err.slug == "unauthenticated"
    assert err.detail == "Authentication required or credentials invalid."
    assert token not in str(err.detail)
    assert err.headers == {"WWW-Authenticate": "Bearer"}


async def _verify(fake: FakeIssuer, token: str, *extra: Any) -> Principal:
    verifier, http = _verifier(fake, *extra)
    try:
        return await verifier.verify(token)
    finally:
        await http.aclose()


@pytest.mark.parametrize("token_type", ["user", "app"])
async def test_entra_principals_carry_the_verified_iat(
    fake_issuer: FakeIssuer, token_type: str
) -> None:
    import jwt

    scp = "access_as_user" if token_type == "user" else None
    token = fake_issuer.mint_entra(
        OID, token_type=token_type, scp=scp, idtyp=None if token_type == "user" else "app"
    )
    principal = await _verify(fake_issuer, token)
    iat = jwt.decode(token, options={"verify_signature": False})["iat"]
    assert isinstance(principal.issued_at, int)
    assert principal.issued_at == int(iat)


async def test_valid_user_token_maps_to_the_tenant_and_object_id(fake_issuer: FakeIssuer) -> None:
    token = fake_issuer.mint_entra(OID, preferred_username="una@corp.test", name="Una")
    principal = await _verify(fake_issuer, token)
    assert principal.id == f"entra:{ENTRA_TENANT_ID}:{OID}"
    assert principal.type == "user"
    assert principal.subject == OID
    assert principal.via == "jwt"
    assert principal.scopes == {"api"}
    assert not principal.restricted
    assert principal.display == "Una"
    assert principal.username_hint == "una@corp.test"
    assert principal.credential_id  # the uti claim
    assert "una" not in principal.id


async def test_credential_id_is_the_uti_claim(fake_issuer: FakeIssuer) -> None:
    token = fake_issuer.mint_entra(OID, extra_claims={"uti": "fixed-uti"})
    assert (await _verify(fake_issuer, token)).credential_id == "fixed-uti"


async def test_valid_app_token_is_a_service_principal(fake_issuer: FakeIssuer) -> None:
    principal = await _verify(fake_issuer, fake_issuer.mint_entra(OID, token_type="app"))
    assert principal.type == "service_principal"
    assert principal.username_hint is None
    assert principal.id == f"entra:{ENTRA_TENANT_ID}:{OID}"
    assert principal.scopes == {"api"}


async def test_user_token_with_explicit_user_idtyp_is_a_user(fake_issuer: FakeIssuer) -> None:
    principal = await _verify(fake_issuer, fake_issuer.mint_entra(OID, idtyp="user"))
    assert principal.type == "user"


async def test_token_without_jti_is_accepted_but_an_oidc_token_without_jti_is_not(
    fake_issuer: FakeIssuer,
) -> None:
    token = fake_issuer.mint_entra(OID, drop_claims=("jti",))
    assert (await _verify(fake_issuer, token)).id.startswith("entra:")
    verifier, http = _verifier(fake_issuer)
    await _expect_401(verifier, fake_issuer.mint("attacker", drop_claims=("jti",)))
    await http.aclose()


_REJECTED: list[tuple[str, dict[str, Any]]] = [
    ("wrong-tenant", {"extra_claims": {"tid": TENANT_B}}),
    ("v1-issuer", {"iss": f"https://sts.windows.net/{ENTRA_TENANT_ID}/"}),
    ("v1-version", {"ver": "1.0"}),
    ("wrong-audience", {"aud": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"}),
    ("expired", {"exp_delta_s": -120}),
    ("not-yet-valid", {"nbf_delta_s": 3600}),
    ("id-token-without-scp", {"scp": None}),
    ("empty-scp", {"scp": "  "}),
    ("app-token-carrying-scp", {"token_type": "app", "extra_claims": {"scp": "x"}}),
    ("user-token-idtyp-app", {"idtyp": "app"}),
    ("unknown-idtyp", {"idtyp": "device"}),
    ("missing-oid", {"drop_claims": ("oid",)}),
    ("non-guid-oid", {"__oid__": "NOT-A-GUID"}),
    ("uppercase-oid", {"__oid__": OID.upper()}),
    ("braced-oid", {"__oid__": "{" + OID + "}"}),
    ("urn-oid", {"__oid__": "urn:uuid:" + OID}),
    ("non-string-oid", {"extra_claims": {"oid": 12345}}),
    ("missing-tid", {"drop_claims": ("tid",)}),
    ("missing-ver", {"drop_claims": ("ver",)}),
    ("missing-nbf", {"drop_claims": ("nbf",)}),
    ("missing-sub", {"drop_claims": ("sub",)}),
    ("missing-exp", {"drop_claims": ("exp",)}),
    ("missing-iat", {"drop_claims": ("iat",)}),
    ("missing-aud", {"drop_claims": ("aud",)}),
]


@pytest.mark.parametrize(("label", "kwargs"), _REJECTED, ids=[label for label, _ in _REJECTED])
async def test_malformed_entra_tokens_are_the_generic_401(
    fake_issuer: FakeIssuer, label: str, kwargs: dict[str, Any]
) -> None:
    args = dict(kwargs)
    oid = args.pop("__oid__", OID)
    verifier, http = _verifier(fake_issuer)
    await _expect_401(verifier, fake_issuer.mint_entra(oid, **args))
    await http.aclose()


async def test_the_rejection_set_is_not_vacuous() -> None:
    assert len(_REJECTED) >= 10


async def test_a_token_signed_by_another_key_is_401(fake_issuer: FakeIssuer) -> None:
    verifier, http = _verifier(fake_issuer)
    forged = FakeIssuer()
    token = forged.mint_entra(OID)  # right kid derivation differs, so the kid is unknown
    await _expect_401(verifier, token)
    await http.aclose()


async def test_claims_grant_nothing(fake_issuer: FakeIssuer) -> None:
    token = fake_issuer.mint_entra(
        OID,
        extra_claims={
            "ptype": "service_account",
            "scope": "password:rotate",
            "roles": ["global_admin"],
            "groups": ["x"],
        },
    )
    principal = await _verify(fake_issuer, token)
    assert principal.type == "user"
    assert principal.scopes == {"api"}
    assert principal.id == f"entra:{ENTRA_TENANT_ID}:{OID}"


async def test_email_upn_and_sub_never_key_the_principal(fake_issuer: FakeIssuer) -> None:
    token = fake_issuer.mint_entra(
        OID,
        preferred_username="victim@corp.test",
        extra_claims={"email": "victim@corp.test", "upn": "victim@corp.test", "sub": "victim"},
    )
    principal = await _verify(fake_issuer, token)
    assert principal.id == f"entra:{ENTRA_TENANT_ID}:{OID}"
    assert principal.subject == OID
    assert "victim" not in principal.id


async def test_username_hint_and_display_are_truncated(fake_issuer: FakeIssuer) -> None:
    token = fake_issuer.mint_entra(OID, name="n" * 500, preferred_username="u" * 500)
    principal = await _verify(fake_issuer, token)
    assert len(principal.display) == 200
    assert principal.username_hint is not None
    assert len(principal.username_hint) == 256


async def test_two_tenants_are_two_configuration_entries(fake_issuer: FakeIssuer) -> None:
    second = fake_issuer.entra_issuer_config(tenant_id=TENANT_B, namespace="entra-b")
    oid_b = str(uuid.uuid4())
    a = await _verify(fake_issuer, fake_issuer.mint_entra(OID), second)
    b = await _verify(fake_issuer, fake_issuer.mint_entra(oid_b, tid=TENANT_B), second)
    assert a.id == f"entra:{ENTRA_TENANT_ID}:{OID}"
    assert b.id == f"entra:{TENANT_B}:{oid_b}"  # the id prefix is "entra", not the namespace
    assert b.issuer == second.issuer


async def test_a_token_of_tenant_b_carrying_the_tid_of_tenant_a_is_401(
    fake_issuer: FakeIssuer,
) -> None:
    second = fake_issuer.entra_issuer_config(tenant_id=TENANT_B, namespace="entra-b")
    verifier, http = _verifier(fake_issuer, second)
    token = fake_issuer.mint_entra(
        OID, tid=TENANT_B, extra_claims={"tid": ENTRA_TENANT_ID}
    )  # B's issuer URL, A's tid claim
    await _expect_401(verifier, token)
    await http.aclose()
