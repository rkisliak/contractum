"""Token rejection matrix (T-01-02, T-01-07, T-01-08)."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
import pytest
import respx
from dc_auth.authorizer import Outcome
from dc_auth.deps import require
from dc_auth.issuers import IssuerConfig, IssuerRegistry
from dc_auth.jwks import JwksCache
from dc_auth.principal import Principal
from dc_auth.testing import FakeIssuer
from dc_auth.verify import TokenVerifier
from dc_core.http import make_client
from dc_core.problems import ProblemError
from fastapi import APIRouter

pytestmark = pytest.mark.unit


def _verifier(fake: FakeIssuer, *, static: bool = True) -> tuple[TokenVerifier, httpx.AsyncClient]:
    http = make_client()
    registry = IssuerRegistry([fake.issuer_config(static=static)])
    return TokenVerifier(registry, JwksCache(http)), http


async def _expect_401(verifier: TokenVerifier, token: str) -> ProblemError:
    with pytest.raises(ProblemError) as info:
        await verifier.verify(token)
    err = info.value
    assert err.status == 401
    assert err.slug == "unauthenticated"
    assert not token or token not in str(err.detail)
    assert err.headers == {"WWW-Authenticate": "Bearer"}
    return err


async def test_valid_token_yields_namespaced_principal(fake_issuer: FakeIssuer) -> None:
    verifier, http = _verifier(fake_issuer)
    token = fake_issuer.mint("u-1", name="Una", scope="api extra")
    principal = await verifier.verify(token)
    await http.aclose()
    assert principal.id == "fake:u-1"
    assert principal.subject == "u-1"
    assert principal.display == "Una"
    assert principal.type == "user"
    assert principal.via == "jwt"
    assert principal.scopes == {"api", "extra"}
    assert principal.credential_id
    assert not principal.restricted


async def test_principal_carries_the_verified_iat_as_whole_seconds(
    fake_issuer: FakeIssuer,
) -> None:
    import jwt

    verifier, http = _verifier(fake_issuer)
    token = fake_issuer.mint("u-3")
    principal = await verifier.verify(token)
    await http.aclose()
    iat = jwt.decode(token, options={"verify_signature": False})["iat"]
    assert isinstance(principal.issued_at, int)
    assert principal.issued_at == int(iat)


async def test_display_defaults_to_subject(fake_issuer: FakeIssuer) -> None:
    verifier, http = _verifier(fake_issuer)
    token = fake_issuer.mint("u-2", drop_claims=("name",))
    principal = await verifier.verify(token)
    await http.aclose()
    assert principal.display == "u-2"


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("expired", {"exp_delta_s": -3600}),
        ("not-yet-valid", {"nbf_delta_s": 3600}),
        ("wrong-audience", {"aud": "someone-else"}),
        ("unknown-issuer", {"iss": "https://evil.test"}),
        ("alg-none", {"alg": "none"}),
        ("missing-kid", {"headers": {"kid": None}}),
        ("missing-jti", {"drop_claims": ("jti",)}),
        ("missing-exp", {"drop_claims": ("exp",)}),
        ("missing-sub", {"drop_claims": ("sub",)}),
        ("missing-aud", {"drop_claims": ("aud",)}),
        ("missing-iat", {"drop_claims": ("iat",)}),
        ("bad-ptype", {"ptype": "admin"}),
        ("empty-sub", {"extra_claims": {"sub": ""}}),
        ("boolean-iat", {"extra_claims": {"iat": True}}),
        ("non-string-scope", {"extra_claims": {"scope": 7}}),
    ],
)
async def test_rejected_tokens_are_401(
    fake_issuer: FakeIssuer, label: str, kwargs: dict[str, Any]
) -> None:
    verifier, http = _verifier(fake_issuer)
    await _expect_401(verifier, fake_issuer.mint("attacker", **kwargs))
    await http.aclose()


async def test_hs256_forgery_is_401(fake_issuer: FakeIssuer) -> None:
    verifier, http = _verifier(fake_issuer)
    await _expect_401(verifier, fake_issuer.hs256_forgery("admin"))
    await http.aclose()


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "x" * 9000])
async def test_garbage_tokens_are_401(fake_issuer: FakeIssuer, token: str) -> None:
    verifier, http = _verifier(fake_issuer)
    await _expect_401(verifier, token)
    await http.aclose()


async def test_token_signed_by_another_key_is_401(fake_issuer: FakeIssuer) -> None:
    other = FakeIssuer()
    verifier, http = _verifier(fake_issuer)
    token = other.mint("admin", headers={"kid": fake_issuer.kid})  # right kid, wrong key
    await _expect_401(verifier, token)
    await http.aclose()


async def test_unknown_kid_refreshes_jwks_once_then_401(fake_issuer: FakeIssuer) -> None:
    verifier, http = _verifier(fake_issuer, static=False)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(fake_issuer.jwks_uri).respond(json=fake_issuer.jwks())
        await _expect_401(verifier, fake_issuer.mint("a", headers={"kid": "unknown-kid"}))
        assert route.call_count == 1
        # Rate limited per issuer: a second unknown kid inside the interval does not refetch.
        await _expect_401(verifier, fake_issuer.mint("a", headers={"kid": "another-unknown"}))
        assert route.call_count == 1
        # A known kid is served from the cache without another fetch.
        await verifier.verify(fake_issuer.mint("a"))
        assert route.call_count == 1
    await http.aclose()


# --- quick 261121-0sh: the issuer is chosen by full verification, never by an unverified claim ---

SECOND_ISSUER = "https://second.test"
SECOND_JWKS_URI = "https://second.test/jwks"


def _second_config(other: FakeIssuer, *, static: bool = True) -> IssuerConfig:
    return IssuerConfig(
        issuer=SECOND_ISSUER,
        jwks_uri=SECOND_JWKS_URI,
        audiences=("contractum",),
        namespace="second",
        static_jwks=other.jwks() if static else None,
    )


def _two_issuer_verifier(
    first: IssuerConfig, second: IssuerConfig
) -> tuple[TokenVerifier, httpx.AsyncClient]:
    http = make_client()
    return TokenVerifier(IssuerRegistry([first, second]), JwksCache(http)), http


async def test_an_unconfigured_issuer_with_a_known_kid_is_401_and_fetches_nothing(
    fake_issuer: FakeIssuer,
) -> None:
    verifier, http = _verifier(fake_issuer, static=False)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(fake_issuer.jwks_uri).respond(json=fake_issuer.jwks())
        await verifier.verify(fake_issuer.mint("warm"))  # the cache now knows the kid
        assert route.call_count == 1
        await _expect_401(verifier, fake_issuer.mint("a", iss="https://evil.test"))
        assert route.call_count == 1
    await http.aclose()


async def test_a_token_claiming_one_issuer_but_signed_by_the_other_is_401(
    fake_issuer: FakeIssuer,
) -> None:
    other = FakeIssuer()
    verifier, http = _two_issuer_verifier(fake_issuer.issuer_config(), _second_config(other))
    # Claims the first issuer, signed with the second issuer's key (its kid): the second owns the
    # kid, but its issuer check rejects the claim.
    await _expect_401(verifier, other.mint("admin", iss=fake_issuer.issuer))
    # Claims the second issuer, signed with the first issuer's key and kid.
    await _expect_401(verifier, fake_issuer.mint("admin", iss=SECOND_ISSUER))
    # Claims the first issuer, second issuer's key but the first issuer's kid: signature fails.
    await _expect_401(
        verifier, other.mint("admin", iss=fake_issuer.issuer, headers={"kid": fake_issuer.kid})
    )
    await http.aclose()


def _forged(header: dict[str, Any], payload: bytes) -> str:
    import base64
    import json

    def enc(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{enc(json.dumps(header).encode())}.{enc(payload)}.{enc(b'sig')}"


async def test_more_garbage_tokens_are_401(fake_issuer: FakeIssuer) -> None:
    verifier, http = _verifier(fake_issuer)
    header = {"alg": "RS256", "typ": "JWT", "kid": fake_issuer.kid}
    await _expect_401(verifier, _forged(header, b"this is not json"))
    await _expect_401(verifier, _forged(header, b'{"iss": 5}'))
    await _expect_401(verifier, _forged({"alg": 7, "kid": fake_issuer.kid}, b"{}"))
    await _expect_401(verifier, _forged({"alg": "RS256", "kid": ""}, b"{}"))
    await http.aclose()


async def test_two_issuers_with_different_keys_map_each_token_to_its_own_namespace(
    fake_issuer: FakeIssuer,
) -> None:
    other = FakeIssuer()
    verifier, http = _two_issuer_verifier(fake_issuer.issuer_config(), _second_config(other))
    first = await verifier.verify(fake_issuer.mint("u-1"))
    second = await verifier.verify(other.mint("u-2", iss=SECOND_ISSUER))
    await http.aclose()
    assert (first.id, first.issuer) == ("fake:u-1", fake_issuer.issuer)
    assert (second.id, second.issuer) == ("second:u-2", SECOND_ISSUER)


async def test_two_issuers_sharing_one_key_and_kid_still_map_to_their_own_issuer(
    fake_issuer: FakeIssuer,
) -> None:
    shared = IssuerConfig(
        issuer=SECOND_ISSUER,
        jwks_uri=SECOND_JWKS_URI,
        audiences=("contractum",),
        namespace="second",
        static_jwks=fake_issuer.jwks(),
    )
    verifier, http = _two_issuer_verifier(fake_issuer.issuer_config(), shared)
    first = await verifier.verify(fake_issuer.mint("u-1"))
    second = await verifier.verify(fake_issuer.mint("u-2", iss=SECOND_ISSUER))
    await http.aclose()
    assert first.id == "fake:u-1"
    assert second.id == "second:u-2"


async def test_a_known_kid_never_triggers_another_issuers_jwks_fetch_or_outage(
    fake_issuer: FakeIssuer,
) -> None:
    other = FakeIssuer()
    verifier, http = _two_issuer_verifier(
        fake_issuer.issuer_config(), _second_config(other, static=False)
    )
    with respx.mock(assert_all_called=False) as router:
        down = router.get(SECOND_JWKS_URI).mock(side_effect=httpx.ConnectError("down"))
        principal = await verifier.verify(fake_issuer.mint("u-1"))
        assert principal.id == "fake:u-1"
        assert down.call_count == 0
        # An unknown kid might belong to the issuer that is down: fail closed with a retryable 503.
        with pytest.raises(ProblemError) as info:
            await verifier.verify(fake_issuer.mint("u-1", headers={"kid": "rotated-key"}))
        assert info.value.status == 503
        assert down.call_count == 1
    await http.aclose()


async def test_unknown_kid_rate_limit_is_per_issuer_across_two_issuers(
    fake_issuer: FakeIssuer,
) -> None:
    other = FakeIssuer()
    first_config = fake_issuer.issuer_config(static=False)
    verifier, http = _two_issuer_verifier(first_config, _second_config(other, static=False))
    with respx.mock(assert_all_called=False) as router:
        first_route = router.get(fake_issuer.jwks_uri).respond(json=fake_issuer.jwks())
        second_route = router.get(SECOND_JWKS_URI).respond(json=other.jwks())
        for kid in ("unknown-1", "unknown-2", "unknown-3"):
            await _expect_401(verifier, fake_issuer.mint("a", headers={"kid": kid}))
        assert first_route.call_count == 1
        assert second_route.call_count == 1
    await http.aclose()


async def test_jwks_outage_fails_closed_with_503_not_401(fake_issuer: FakeIssuer) -> None:
    """WR-04: an IdP outage is a retryable 503, never a credential-rejecting 401."""
    verifier, http = _verifier(fake_issuer, static=False)
    with respx.mock(assert_all_called=False) as router:
        router.get(fake_issuer.jwks_uri).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(ProblemError) as info:
            await verifier.verify(fake_issuer.mint("a"))
    assert info.value.status == 503
    assert info.value.slug == "service-unavailable"
    await http.aclose()


async def test_cold_start_recovers_within_the_failure_retry_interval(
    fake_issuer: FakeIssuer,
) -> None:
    """WR-04: identity not up yet at first use must not poison the cache for 30 s."""
    now = [0.0]
    http = make_client()
    registry = IssuerRegistry([fake_issuer.issuer_config(static=False)])
    verifier = TokenVerifier(registry, JwksCache(http, clock=lambda: now[0]))
    token = fake_issuer.mint("a")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(fake_issuer.jwks_uri)
        route.mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(ProblemError) as info:
            await verifier.verify(token)
        assert info.value.status == 503

        route.respond(json=fake_issuer.jwks())  # identity is healthy now
        now[0] = 3.0  # well inside the 30 s unknown-kid interval, past the failure retry
        principal = await verifier.verify(token)
        assert principal.id == "fake:a"
    await http.aclose()


async def test_a_non_object_jwks_document_is_an_outage_not_a_500(fake_issuer: FakeIssuer) -> None:
    verifier, http = _verifier(fake_issuer, static=False)
    with respx.mock(assert_all_called=False) as router:
        router.get(fake_issuer.jwks_uri).respond(json=[{"kid": "x"}])
        with pytest.raises(ProblemError) as info:
            await verifier.verify(fake_issuer.mint("a"))
    assert info.value.status == 503
    await http.aclose()


async def test_a_cached_kid_is_served_while_another_refresh_is_in_flight(
    fake_issuer: FakeIssuer,
) -> None:
    """WR-04: the refresh lock is not taken for a kid that is already cached and fresh."""
    import asyncio

    verifier, http = _verifier(fake_issuer, static=False)
    release = asyncio.Event()

    async def slow_jwks(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=fake_issuer.jwks())

    with respx.mock(assert_all_called=False) as router:
        router.get(fake_issuer.jwks_uri).respond(json=fake_issuer.jwks())
        await verifier.verify(fake_issuer.mint("a"))  # warm the cache
        router.get(fake_issuer.jwks_uri).mock(side_effect=slow_jwks)
        # An unknown kid inside the interval does not fetch, so force one by ageing the attempt.
        state = next(iter(verifier._jwks._state.values()))  # noqa: SLF001
        state.last_attempt = -1_000_000.0
        blocked = asyncio.create_task(
            verifier.verify(fake_issuer.mint("b", headers={"kid": "rotated"}))
        )
        await asyncio.sleep(0.05)
        assert state.lock.locked()
        principal = await asyncio.wait_for(verifier.verify(fake_issuer.mint("c")), timeout=1)
        assert principal.id == "fake:c"
        release.set()
        with pytest.raises(ProblemError):
            await blocked
    await http.aclose()


def _probe_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/probe/normal")
    async def normal(principal: Annotated[Principal, require("probe:read")]) -> dict[str, str]:
        return {"id": principal.id}

    @router.post("/api/v1/probe/rotate")
    async def rotate(
        principal: Annotated[Principal, require("auth:rotate", allow_restricted=True)],
    ) -> dict[str, str]:
        return {"id": principal.id}

    return router


async def test_restricted_token_is_403_except_on_allow_restricted_route(
    fake_issuer: FakeIssuer, asgi_client: Any, probe_app: Any
) -> None:
    token = fake_issuer.mint("boot", scope="password:rotate")
    headers = {"Authorization": f"Bearer {token}"}
    async with asgi_client(probe_app(routers=[_probe_router()])) as client:
        denied = await client.get("/api/v1/probe/normal", headers=headers)
        assert denied.status_code == 403
        assert denied.headers["content-type"].startswith("application/problem+json")
        assert denied.json()["type"] == "urn:dc:problem:password-rotation-required"
        allowed = await client.post("/api/v1/probe/rotate", headers=headers)
        assert allowed.status_code == 200
        assert allowed.json() == {"id": "fake:boot"}
        full = await client.get(
            "/api/v1/probe/normal", headers={"Authorization": f"Bearer {fake_issuer.mint('u')}"}
        )
        assert full.status_code == 200


async def test_scheme_and_header_variants_are_401(
    fake_issuer: FakeIssuer, asgi_client: Any, probe_app: Any
) -> None:
    token = fake_issuer.mint("u")
    async with asgi_client(probe_app(routers=[_probe_router()])) as client:
        for headers in (
            {},
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer"},
            {"Authorization": f"Token {token}"},
        ):
            response = await client.get("/api/v1/probe/normal", headers=headers)
            assert response.status_code == 401, headers
            assert response.json()["type"] == "urn:dc:problem:unauthenticated"
        ok = await client.get("/api/v1/probe/normal", headers={"Authorization": f"bearer {token}"})
        assert ok.status_code == 200


@pytest.mark.parametrize(
    ("outcome", "status", "slug"),
    [(Outcome.NOT_FOUND, 404, "not-found"), (Outcome.FORBIDDEN, 403, "forbidden")],
)
async def test_a_denying_decision_is_the_matching_problem_and_never_reaches_the_handler(
    fake_issuer: FakeIssuer,
    asgi_client: Any,
    probe_app: Any,
    outcome: Outcome,
    status: int,
    slug: str,
) -> None:
    headers = {"Authorization": f"Bearer {fake_issuer.mint('u')}"}
    app = probe_app(routers=[_probe_router()], outcome=outcome)
    async with asgi_client(app) as client:
        response = await client.get("/api/v1/probe/normal", headers=headers)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    assert "id" not in body
