"""Opaque dcp_ bearer tokens: parser, verifier branch, remote resolver (D-71, D-72, AUTH-03)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import jwt
import pytest
import structlog
from dc_auth.authz_wire import PrincipalOut
from dc_auth.deps import auth_lifespan
from dc_auth.issuers import AuthSettings, IssuerRegistry
from dc_auth.jwks import JwksCache
from dc_auth.opaque import (
    DCP_PREFIX,
    OPAQUE_ISSUER,
    PUBLIC_ID_RE,
    SECRET_RE,
    RemoteOpaqueResolver,
    parse_dcp,
)
from dc_auth.principal import Principal, principal_to_actor
from dc_auth.testing import FakeIssuer
from dc_auth.verify import TokenVerifier
from dc_core.problems import ProblemError
from fastapi import FastAPI
from pydantic import ValidationError

pytestmark = pytest.mark.unit

PUBLIC_ID = "0123456789abcdef"
SECRET = "A" * 21 + "_" + "b" * 21
TOKEN = f"dcp_{PUBLIC_ID}_{SECRET}"
BASE = "http://identity:8000"
ENDPOINT = f"{BASE}/internal/v1/authz/principal"


def _problem_body(slug: str) -> bytes:
    return json.dumps({"type": f"urn:dc:problem:{slug}", "status": 403}).encode()


def _principal(via: str = "pat") -> Principal:
    return Principal(
        id="local:abc",
        type="user",
        display="Una",
        issuer=OPAQUE_ISSUER,
        subject="abc",
        via=via,  # type: ignore[arg-type]
        scopes=frozenset({"api"}),
        credential_id=PUBLIC_ID,
    )


# --- parse_dcp --------------------------------------------------------------------------------


def test_constants() -> None:
    assert DCP_PREFIX == "dcp_"
    assert OPAQUE_ISSUER == "urn:dc:opaque"
    assert PUBLIC_ID_RE.pattern == "^[0-9a-f]{16}$"
    assert SECRET_RE.pattern == "^[A-Za-z0-9_-]{43}$"


def test_parse_valid_token() -> None:
    secret = "x" * 43
    assert parse_dcp(f"dcp_{PUBLIC_ID}_{secret}") == (PUBLIC_ID, secret)


def test_parse_secret_with_underscore_and_dash() -> None:
    assert parse_dcp(TOKEN) == (PUBLIC_ID, SECRET)
    assert len(SECRET) == 43
    dashed = "a-b" * 14 + "c"
    assert parse_dcp(f"dcp_{PUBLIC_ID}_{dashed}") == (PUBLIC_ID, dashed)


@pytest.mark.parametrize(
    "token",
    [
        "dcp_XYZ0123456789abc_" + "x" * 43,  # non-hex id
        "dcp_0123456789ABCDEF_" + "x" * 43,  # upper-case hex is not the canonical id form
        "dcp_0123456789abcde_" + "x" * 43,  # 15-char id
        "dcp_0123456789abcdef0_" + "x" * 43,  # 17-char id
        f"dcp_{PUBLIC_ID}_" + "x" * 42,  # 42-char secret
        f"dcp_{PUBLIC_ID}_" + "x" * 44,  # 44-char secret
        f"dcp_{PUBLIC_ID}_" + "x" * 42 + "!",  # bad secret character
        "dcp__" + "x" * 43,  # empty id
        "dcp__x",
        f"dcp_{PUBLIC_ID}_",  # empty secret
        f"dcp_{PUBLIC_ID}",  # no secret separator
        "dcp_",
        "",
        "eyJhbGciOiJSUzI1NiJ9.e30.sig",
        f"DCP_{PUBLIC_ID}_" + "x" * 43,  # prefix is case-sensitive
    ],
)
def test_parse_rejects_malformed(token: str) -> None:
    assert parse_dcp(token) is None


# --- Principal.via ----------------------------------------------------------------------------


@pytest.mark.parametrize("via", ["jwt", "system", "pat", "api_key"])
def test_principal_via_accepts(via: str) -> None:
    assert _principal(via).via == via


def test_principal_via_rejects_other() -> None:
    with pytest.raises(ValidationError):
        _principal("other")


def test_pat_principal_audit_actor_records_via_and_public_id() -> None:
    actor = principal_to_actor(_principal("pat"))
    assert actor.via == "pat"
    assert actor.credential_id == PUBLIC_ID
    assert principal_to_actor(_principal("api_key")).via == "api_key"


# --- TokenVerifier branch ---------------------------------------------------------------------


class _StubResolver:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._error = error

    async def resolve(self, token: str) -> Principal:
        self.calls.append(token)
        if self._error is not None:
            raise self._error
        return _principal("pat")


def _forbid_jwt_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("JWT parsing must not run for a dcp_ token")

    monkeypatch.setattr(jwt, "get_unverified_header", _boom)
    monkeypatch.setattr(jwt, "decode", _boom)


def _verifier(
    fake: FakeIssuer, opaque: _StubResolver | None
) -> tuple[TokenVerifier, httpx.AsyncClient]:
    http = httpx.AsyncClient()
    registry = IssuerRegistry([fake.issuer_config(static=True)])
    return TokenVerifier(registry, JwksCache(http), opaque=opaque), http


async def test_dcp_token_reaches_resolver_and_never_the_jwt_path(
    fake_issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = _StubResolver()
    verifier, http = _verifier(fake_issuer, resolver)
    _forbid_jwt_parsing(monkeypatch)
    principal = await verifier.verify(TOKEN)
    await http.aclose()
    assert resolver.calls == [TOKEN]
    assert principal.via == "pat"


async def test_dcp_token_without_resolver_is_generic_401(
    fake_issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier, http = _verifier(fake_issuer, None)
    _forbid_jwt_parsing(monkeypatch)
    with pytest.raises(ProblemError) as info:
        await verifier.verify(TOKEN)
    await http.aclose()
    assert info.value.status == 401
    assert info.value.slug == "unauthenticated"
    assert TOKEN not in str(info.value.detail)


async def test_resolver_value_error_is_401(fake_issuer: FakeIssuer) -> None:
    verifier, http = _verifier(fake_issuer, _StubResolver(error=ValueError("nope")))
    with pytest.raises(ProblemError) as info:
        await verifier.verify(TOKEN)
    await http.aclose()
    assert info.value.status == 401
    assert info.value.slug == "unauthenticated"


async def test_resolver_problem_error_is_reraised(fake_issuer: FakeIssuer) -> None:
    err = ProblemError(503, "service-unavailable", "Service Unavailable", "down")
    verifier, http = _verifier(fake_issuer, _StubResolver(error=err))
    with pytest.raises(ProblemError) as info:
        await verifier.verify(TOKEN)
    await http.aclose()
    assert info.value is err


async def test_jwt_still_goes_through_the_jwt_path(fake_issuer: FakeIssuer) -> None:
    resolver = _StubResolver()
    verifier, http = _verifier(fake_issuer, resolver)
    principal = await verifier.verify(fake_issuer.mint("u-1"))
    await http.aclose()
    assert principal.id == "fake:u-1"
    assert resolver.calls == []


# --- RemoteOpaqueResolver ---------------------------------------------------------------------


class _Recorder:
    def __init__(self, handler: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self._handler(request)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, httpx.Response)
        return result


def _resolver(handler: Any) -> tuple[RemoteOpaqueResolver, _Recorder, httpx.AsyncClient]:
    rec = _Recorder(handler)
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    return RemoteOpaqueResolver(http, base_url=BASE + "/", timeout_s=2.0), rec, http


def _ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "local:abc",
            "type": "user",
            "display": "Una",
            "issuer": OPAQUE_ISSUER,
            "subject": "abc",
            "via": "pat",
            "credential_id": PUBLIC_ID,
        },
    )


async def test_remote_200_builds_a_principal_with_api_scope() -> None:
    resolver, rec, http = _resolver(_ok)
    principal = await resolver.resolve(TOKEN)
    await http.aclose()
    assert principal.id == "local:abc"
    assert principal.via == "pat"
    assert principal.credential_id == PUBLIC_ID
    assert principal.scopes == {"api"}
    (request,) = rec.requests
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_remote_401_is_value_error_without_the_token() -> None:
    resolver, _, http = _resolver(lambda _r: httpx.Response(401))
    with pytest.raises(ValueError, match="opaque token rejected") as info:
        await resolver.resolve(TOKEN)
    await http.aclose()
    assert TOKEN not in str(info.value)


@pytest.mark.parametrize("slug", ["principal-not-registered", "interactive-session-required"])
async def test_remote_known_403_passes_through(slug: str) -> None:
    resolver, _, http = _resolver(lambda _r: httpx.Response(403, content=_problem_body(slug)))
    with pytest.raises(ProblemError) as info:
        await resolver.resolve(TOKEN)
    await http.aclose()
    assert info.value.status == 403
    assert info.value.slug == slug


def _assert_503(err: ProblemError) -> None:
    assert err.status == 503
    assert err.slug == "service-unavailable"
    assert TOKEN not in str(err.detail)
    assert TOKEN not in str(err)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(502, content=b"bad gateway"),
        httpx.Response(404),
        httpx.Response(403, content=_problem_body("forbidden")),  # an unknown 403 is no answer
        httpx.Response(403, content=b"not json"),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"id": "local:abc"}),  # missing fields
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
async def test_remote_everything_else_is_503(response: httpx.Response) -> None:
    resolver, _, http = _resolver(lambda _r: response)
    with pytest.raises(ProblemError) as info:
        await resolver.resolve(TOKEN)
    await http.aclose()
    _assert_503(info.value)


async def test_remote_transport_error_is_503() -> None:
    resolver, _, http = _resolver(lambda r: httpx.ConnectError(f"boom {r.url}", request=r))
    with pytest.raises(ProblemError) as info:
        await resolver.resolve(TOKEN)
    await http.aclose()
    _assert_503(info.value)


async def test_remote_resolution_is_never_cached() -> None:
    resolver, rec, http = _resolver(_ok)
    await resolver.resolve(TOKEN)
    await resolver.resolve(TOKEN)
    await http.aclose()
    assert len(rec.requests) == 2


async def test_a_revoked_token_stops_working_on_the_next_request() -> None:
    answers = iter([_ok(None), httpx.Response(401)])  # type: ignore[arg-type]
    resolver, rec, http = _resolver(lambda _r: next(answers))
    await resolver.resolve(TOKEN)
    with pytest.raises(ValueError, match="opaque token rejected"):
        await resolver.resolve(TOKEN)
    await http.aclose()
    assert len(rec.requests) == 2


@pytest.mark.parametrize(
    "handler",
    [
        lambda _r: httpx.Response(401),
        lambda _r: httpx.Response(500),
        lambda r: httpx.ConnectError("down", request=r),
        lambda _r: httpx.Response(200, content=b"junk"),
        _ok,
    ],
)
async def test_the_token_is_never_logged(handler: Any, caplog: pytest.LogCaptureFixture) -> None:
    resolver, _, http = _resolver(handler)
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as captured:
        try:
            await resolver.resolve(TOKEN)
        except ValueError, ProblemError:
            pass
    await http.aclose()
    assert SECRET not in json.dumps(captured, default=str)
    assert SECRET not in caplog.text


def test_principal_out_round_trips_the_wire_fields() -> None:
    out = PrincipalOut(
        id="sa:1",
        type="service_account",
        display="Pipeline",
        issuer=OPAQUE_ISSUER,
        subject="1",
        via="api_key",
        credential_id=PUBLIC_ID,
    )
    assert PrincipalOut.model_validate_json(out.model_dump_json()) == out
    with pytest.raises(ValidationError):
        PrincipalOut.model_validate({**out.model_dump(), "via": "other"})


# --- auth_lifespan default --------------------------------------------------------------------


async def _verifier_of(**kwargs: Any) -> Any:
    hook = auth_lifespan(AuthSettings(issuers=[]), authorizer_factory=lambda _r: None, **kwargs)  # type: ignore[arg-type,return-value]
    app = FastAPI()
    async with hook(app):
        return app.state.verifier._opaque


async def test_auth_lifespan_installs_remote_resolver_by_default() -> None:
    assert isinstance(await _verifier_of(), RemoteOpaqueResolver)


async def test_auth_lifespan_uses_the_given_resolver() -> None:
    stub = _StubResolver()
    assert await _verifier_of(opaque_resolver=stub) is stub
