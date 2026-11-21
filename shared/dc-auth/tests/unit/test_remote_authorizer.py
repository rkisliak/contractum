"""RemoteAuthorizer: forwards the caller's token, fails closed (D-08)."""

from __future__ import annotations

import json
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from asgi_correlation_id import correlation_id
from dc_auth.authorizer import AuthorizerUnavailable, GranteeAuthorizer, Outcome
from dc_auth.authz_wire import (
    AccessibleFoldersOut,
    AuthzCheckIn,
    AuthzCheckOut,
    CheckContext,
)
from dc_auth.deps import auth_lifespan, require
from dc_auth.issuers import AuthSettings
from dc_auth.principal import Principal
from dc_auth.remote import RemoteAuthorizer, remote_authorizer_factory
from dc_auth.testing import FakeIssuer
from dc_core.app import create_app
from dc_core.http import make_client
from dc_core.problems import ProblemError
from dc_core.settings import CoreSettings
from fastapi import APIRouter
from pydantic import SecretStr

pytestmark = pytest.mark.unit

BASE = "http://identity:8000"
CHECK = f"{BASE}/internal/v1/authz/check"
FOLDERS = f"{BASE}/internal/v1/authz/accessible-folders"
GRANTEES = f"{BASE}/internal/v1/authz/grantees"
MEMBERS = f"{BASE}/internal/v1/authz/members/check"

PRINCIPAL = Principal(
    id="fake:u", type="user", display="U", issuer="https://fake-issuer.test", subject="u",
    via="jwt", scopes=frozenset({"api"}),
)  # fmt: skip


def _authorizer(http: httpx.AsyncClient) -> RemoteAuthorizer:
    return RemoteAuthorizer(http, base_url=BASE, bearer=SecretStr("tok-123"), timeout_s=2.0)


async def test_check_posts_wire_model_with_callers_bearer_token() -> None:
    folder_id = uuid4()
    facts = {
        "id": str(folder_id), "kind": "team", "slug": "ingest", "path": "acme/payments/ingest",
        "ancestor_ids": [str(uuid4()), str(folder_id)], "organization_id": str(uuid4()),
        "archived": False,
    }  # fmt: skip
    http = make_client()
    with respx.mock() as router:
        route = router.post(CHECK).respond(json={"outcome": "allow", "folder": facts})
        decision = await _authorizer(http).check(
            PRINCIPAL, "contract:create", folder_id, fresh=True
        )
    await http.aclose()
    assert decision.outcome is Outcome.ALLOW
    assert decision.folder is not None
    assert decision.folder.slug == "ingest"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tok-123"
    body = json.loads(request.content)
    assert body == {
        "permission": "contract:create",
        "folder_id": str(folder_id),
        "fresh": True,
        "service": None,
        "route": None,
    }
    assert AuthzCheckIn.model_validate(body).permission == "contract:create"
    assert "principal" not in body  # derived from the forwarded token, never from the body


@pytest.mark.parametrize("outcome", ["not_found", "forbidden"])
async def test_check_maps_deny_outcomes(outcome: str) -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(CHECK).respond(json={"outcome": outcome, "folder": None})
        decision = await _authorizer(http).check(PRINCIPAL, "x:y")
    await http.aclose()
    assert decision.outcome is Outcome(outcome)
    assert decision.folder is None


async def test_identity_401_becomes_problem_401() -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(CHECK).respond(401)
        with pytest.raises(ProblemError) as info:
            await _authorizer(http).check(PRINCIPAL, "x:y")
    await http.aclose()
    assert info.value.status == 401
    assert info.value.slug == "unauthenticated"


@pytest.mark.parametrize(
    "mock",
    [
        lambda route: route.respond(500),
        lambda route: route.respond(502),
        lambda route: route.respond(200, content=b"not json"),
        lambda route: route.respond(200, json={"outcome": "bogus"}),
        lambda route: route.mock(side_effect=httpx.ReadTimeout("slow")),
        lambda route: route.mock(side_effect=httpx.ConnectError("down")),
    ],
)
async def test_failures_are_authorizer_unavailable(mock: Any) -> None:
    http = make_client()
    with respx.mock() as router:
        mock(router.post(CHECK))
        with pytest.raises(AuthorizerUnavailable):
            await _authorizer(http).check(PRINCIPAL, "x:y")
    await http.aclose()


async def test_accessible_folders_maps_scope() -> None:
    ids = [uuid4(), uuid4()]
    http = make_client()
    with respx.mock() as router:
        route = router.post(FOLDERS).respond(
            json={"all": False, "folder_ids": [str(i) for i in ids]}
        )
        scope = await _authorizer(http).accessible_folders(PRINCIPAL, "contract:read")
    await http.aclose()
    assert scope.all is False
    assert scope.folder_ids == frozenset(ids)
    assert json.loads(route.calls.last.request.content) == {"permission": "contract:read"}
    assert route.calls.last.request.headers["authorization"] == "Bearer tok-123"


@pytest.mark.parametrize(
    "body",
    [
        {"content": b"not json"},
        {"json": {"folder_ids": []}},  # "all" missing
        {"json": {"all": False, "folder_ids": ["not-a-uuid"]}},
    ],
    ids=["not-json", "missing-all", "bad-folder-id"],
)
async def test_an_invalid_accessible_folders_answer_is_authorizer_unavailable(
    body: dict[str, Any],
) -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(FOLDERS).respond(200, **body)
        with pytest.raises(AuthorizerUnavailable, match="invalid authorization response"):
            await _authorizer(http).accessible_folders(PRINCIPAL, "contract:read")
    await http.aclose()


async def test_grantees_parses_the_wire_model_and_sends_no_principal() -> None:
    http = make_client()
    group = str(uuid4())
    with respx.mock() as router:
        route = router.post(GRANTEES).respond(
            json={
                "grantees": [
                    {"type": "user", "id": "local:u"},
                    {"type": "local_group", "id": group},
                ]
            }
        )
        found = await _authorizer(http).grantees(PRINCIPAL)
    await http.aclose()
    assert found == frozenset({("user", "local:u"), ("local_group", group)})
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tok-123"
    assert json.loads(request.content) == {}  # derived from the forwarded token, never the body


@pytest.mark.parametrize(
    "mock",
    [
        lambda route: route.respond(500),
        lambda route: route.respond(503),
        lambda route: route.respond(200, content=b"not json"),
        lambda route: route.respond(200, json={"grantees": [{"type": "user"}]}),
        lambda route: route.mock(side_effect=httpx.ReadTimeout("slow")),
        lambda route: route.mock(side_effect=httpx.ConnectError("down")),
    ],
)
async def test_grantees_failures_are_authorizer_unavailable_never_a_partial_set(
    mock: Any,
) -> None:
    http = make_client()
    with respx.mock() as router:
        mock(router.post(GRANTEES))
        with pytest.raises(AuthorizerUnavailable):
            await _authorizer(http).grantees(PRINCIPAL)
    await http.aclose()


async def test_grantees_identity_401_is_the_401_problem() -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(GRANTEES).respond(401)
        with pytest.raises(ProblemError) as info:
            await _authorizer(http).grantees(PRINCIPAL)
    await http.aclose()
    assert info.value.status == 401
    assert info.value.slug == "unauthenticated"


async def test_check_members_maps_results_by_member_and_never_sends_a_principal() -> None:
    folder = uuid4()
    group, account = str(uuid4()), f"sa:{uuid4()}"
    http = make_client()
    with respx.mock() as router:
        route = router.post(MEMBERS).respond(
            json={
                "results": [
                    {"type": "service_account", "id": account, "valid": False},
                    {"type": "local_group", "id": group, "valid": True},
                ]
            }
        )
        got = await _authorizer(http).check_members(
            PRINCIPAL, folder, [("local_group", group), ("service_account", account)]
        )
    await http.aclose()
    assert got == {("local_group", group): True, ("service_account", account): False}
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tok-123"
    assert json.loads(request.content) == {
        "folder_id": str(folder),
        "members": [
            {"type": "local_group", "id": group},
            {"type": "service_account", "id": account},
        ],
    }


@pytest.mark.parametrize(
    "mock",
    [
        lambda route: route.respond(500),
        lambda route: route.respond(503),
        lambda route: route.respond(422, json={"detail": "too many"}),
        lambda route: route.respond(200, content=b"not json"),
        lambda route: route.respond(200, json={"results": []}),  # a requested member is missing
        lambda route: route.mock(side_effect=httpx.ReadTimeout("slow")),
    ],
)
async def test_check_members_failures_and_missing_members_are_authorizer_unavailable(
    mock: Any,
) -> None:
    http = make_client()
    with respx.mock() as router:
        mock(router.post(MEMBERS))
        with pytest.raises(AuthorizerUnavailable):
            await _authorizer(http).check_members(PRINCIPAL, uuid4(), [("local_group", "g")])
    await http.aclose()


def test_the_remote_authorizer_is_a_grantee_authorizer_and_the_plain_protocol_is_not_widened() -> (
    None
):
    http = make_client()
    assert isinstance(_authorizer(http), GranteeAuthorizer)

    class _Plain:
        async def check(self, *args: Any, **kwargs: Any) -> None: ...

        async def accessible_folders(self, *args: Any, **kwargs: Any) -> None: ...

    assert not isinstance(_Plain(), GranteeAuthorizer)


def test_wire_models_round_trip() -> None:
    out = AuthzCheckOut.model_validate({"outcome": "allow", "folder": None})
    assert out.outcome is Outcome.ALLOW
    assert isinstance(UUID(str(uuid4())), UUID)


def _app(fake: FakeIssuer, http: httpx.AsyncClient):  # type: ignore[no-untyped-def]
    settings = AuthSettings(issuers=[fake.issuer_config()], identity_internal_url=BASE)
    router = APIRouter()

    @router.get("/api/v1/probe/thing")
    async def thing(principal: Annotated[Principal, require("probe:read")]) -> dict[str, str]:
        return {"id": principal.id}

    return create_app(
        service="probe",
        settings=CoreSettings(service="probe", database_url="postgresql+psycopg://x/y"),
        routers=[router],
        lifespan_hooks=[
            auth_lifespan(
                settings, authorizer_factory=remote_authorizer_factory(settings), http_client=http
            )
        ],
    )


async def test_route_forwards_token_and_serves_allowed_request(
    fake_issuer: FakeIssuer, asgi_client: Any
) -> None:
    http = make_client()
    token = fake_issuer.mint("u")
    with respx.mock() as router:
        route = router.post(CHECK).respond(json={"outcome": "allow", "folder": None})
        async with asgi_client(_app(fake_issuer, http)) as client:
            response = await client.get(
                "/api/v1/probe/thing", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 200
    assert route.calls.last.request.headers["authorization"] == f"Bearer {token}"
    assert json.loads(route.calls.last.request.content)["fresh"] is False  # GET
    assert not http.is_closed  # injected clients are not closed by the lifespan hook
    await http.aclose()


@pytest.mark.parametrize(
    "mock",
    [
        lambda route: route.respond(503),
        lambda route: route.mock(side_effect=httpx.ReadTimeout("slow")),
        lambda route: route.mock(side_effect=httpx.ConnectError("down")),
    ],
)
async def test_unreachable_authorization_service_is_503_problem(
    fake_issuer: FakeIssuer, asgi_client: Any, mock: Any
) -> None:
    http = make_client()
    token = fake_issuer.mint("u")
    with respx.mock() as router:
        mock(router.post(CHECK))
        async with asgi_client(_app(fake_issuer, http)) as client:
            response = await client.get(
                "/api/v1/probe/thing", headers={"Authorization": f"Bearer {token}"}
            )
    await http.aclose()
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:dc:problem:service-unavailable"


async def test_identity_rejecting_the_token_surfaces_as_401(
    fake_issuer: FakeIssuer, asgi_client: Any
) -> None:
    http = make_client()
    token = fake_issuer.mint("u")
    with respx.mock() as router:
        router.post(CHECK).respond(401)
        async with asgi_client(_app(fake_issuer, http)) as client:
            response = await client.get(
                "/api/v1/probe/thing", headers={"Authorization": f"Bearer {token}"}
            )
    await http.aclose()
    assert response.status_code == 401
    assert response.json()["type"] == "urn:dc:problem:unauthenticated"


async def test_accessible_folders_passes_excluded_subtrees_through() -> None:
    ids = [uuid4()]
    excluded = [uuid4(), uuid4()]
    http = make_client()
    with respx.mock() as router:
        router.post(FOLDERS).respond(
            json={
                "all": True,
                "folder_ids": [str(i) for i in ids],
                "excluded_subtrees": [str(i) for i in excluded],
            }
        )
        scope = await _authorizer(http).accessible_folders(PRINCIPAL, "contract:read")
    await http.aclose()
    assert scope.all is True
    assert scope.folder_ids == frozenset(ids)
    assert scope.excluded_subtrees == frozenset(excluded)


async def test_accessible_folders_phase_one_payload_has_no_exclusions() -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(FOLDERS).respond(json={"all": True, "folder_ids": []})
        scope = await _authorizer(http).accessible_folders(PRINCIPAL, "contract:read")
    await http.aclose()
    assert scope.excluded_subtrees == frozenset()


def test_accessible_folders_out_without_excluded_subtrees_parses_to_empty_list() -> None:
    out = AccessibleFoldersOut.model_validate_json('{"all":false,"folder_ids":[]}')
    assert out.excluded_subtrees == []
    folder = uuid4()
    again = AccessibleFoldersOut.model_validate_json(
        AccessibleFoldersOut(all=True, excluded_subtrees=[folder]).model_dump_json()
    )
    assert again.excluded_subtrees == [folder]


def test_authz_check_out_phase_one_payload_has_no_permissions() -> None:
    out = AuthzCheckOut.model_validate_json('{"outcome":"allow","folder":null}')
    assert out.permissions == []
    again = AuthzCheckOut.model_validate_json(
        AuthzCheckOut(outcome=Outcome.ALLOW, permissions=["a:b", "c:d"]).model_dump_json()
    )
    assert again.permissions == ["a:b", "c:d"]


async def test_check_maps_permissions_to_a_frozenset() -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(CHECK).respond(
            json={"outcome": "allow", "folder": None, "permissions": ["contract:read", "a:b"]}
        )
        decision = await _authorizer(http).check(PRINCIPAL, "contract:read")
    await http.aclose()
    assert decision.permissions == frozenset({"contract:read", "a:b"})


async def test_check_without_permissions_in_the_payload_holds_none() -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(CHECK).respond(json={"outcome": "allow", "folder": None})
        decision = await _authorizer(http).check(PRINCIPAL, "contract:read")
    await http.aclose()
    assert decision.permissions == frozenset()


def _problem_body(slug: str, detail: str = "identity's own words") -> dict[str, Any]:
    return {
        "type": f"urn:dc:problem:{slug}",
        "title": "Forbidden",
        "status": 403,
        "detail": detail,
    }


@pytest.mark.parametrize("slug", ["principal-not-registered", "interactive-session-required"])
async def test_identity_known_403_problems_pass_through_with_a_fixed_detail(slug: str) -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(CHECK).respond(403, json=_problem_body(slug, "secret internal detail"))
        with pytest.raises(ProblemError) as info:
            await _authorizer(http).check(PRINCIPAL, "x:y")
    await http.aclose()
    assert info.value.status == 403
    assert info.value.slug == slug
    assert info.value.detail
    assert "secret internal detail" not in str(info.value.detail)


async def test_accessible_folders_follows_the_same_mapping() -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(FOLDERS).respond(403, json=_problem_body("principal-not-registered"))
        with pytest.raises(ProblemError) as info:
            await _authorizer(http).accessible_folders(PRINCIPAL, "contract:read")
        router.post(FOLDERS).respond(401)
        with pytest.raises(ProblemError) as unauthenticated:
            await _authorizer(http).accessible_folders(PRINCIPAL, "contract:read")
        router.post(FOLDERS).respond(403, json=_problem_body("something-else"))
        with pytest.raises(AuthorizerUnavailable):
            await _authorizer(http).accessible_folders(PRINCIPAL, "contract:read")
    await http.aclose()
    assert info.value.status == 403
    assert info.value.slug == "principal-not-registered"
    assert unauthenticated.value.status == 401


@pytest.mark.parametrize(
    "mock",
    [
        lambda route: route.respond(403, json=_problem_body("grant-ceiling-exceeded")),
        lambda route: route.respond(403, json=_problem_body("forbidden")),
        lambda route: route.respond(403, json={"type": "about:blank", "status": 403}),
        lambda route: route.respond(403, json={"detail": "no type"}),
        lambda route: route.respond(403, json=["not", "an", "object"]),
        lambda route: route.respond(403, content=b"<html>forbidden</html>"),
        lambda route: route.respond(403, content=b""),
        lambda route: route.respond(500, json=_problem_body("principal-not-registered")),
        lambda route: route.respond(502, json=_problem_body("interactive-session-required")),
        lambda route: route.respond(404, json=_problem_body("principal-not-registered")),
    ],
)
async def test_any_other_identity_answer_stays_authorizer_unavailable(mock: Any) -> None:
    """A 403 with an unknown problem type, a non-JSON 403 body, or any 5xx is an outage (503)."""
    http = make_client()
    with respx.mock() as router:
        mock(router.post(CHECK))
        with pytest.raises(AuthorizerUnavailable):
            await _authorizer(http).check(PRINCIPAL, "x:y")
    await http.aclose()


async def test_route_answers_identitys_known_403_not_503(
    fake_issuer: FakeIssuer, asgi_client: Any
) -> None:
    http = make_client()
    token = fake_issuer.mint("u")
    with respx.mock() as router:
        router.post(CHECK).respond(403, json=_problem_body("interactive-session-required"))
        async with asgi_client(_app(fake_issuer, http)) as client:
            response = await client.get(
                "/api/v1/probe/thing", headers={"Authorization": f"Bearer {token}"}
            )
    await http.aclose()
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:dc:problem:interactive-session-required"


async def test_route_with_an_unknown_identity_403_is_503(
    fake_issuer: FakeIssuer, asgi_client: Any
) -> None:
    http = make_client()
    token = fake_issuer.mint("u")
    with respx.mock() as router:
        router.post(CHECK).respond(403, json=_problem_body("something-unknown"))
        async with asgi_client(_app(fake_issuer, http)) as client:
            response = await client.get(
                "/api/v1/probe/thing", headers={"Authorization": f"Bearer {token}"}
            )
    await http.aclose()
    assert response.status_code == 503
    assert response.json()["type"] == "urn:dc:problem:service-unavailable"


RESOLVE = f"{BASE}/internal/v1/authz/principals/resolve"


async def test_resolve_principals_posts_with_the_callers_bearer_and_returns_the_maps() -> None:
    http = make_client()
    with respx.mock() as router:
        route = router.post(RESOLVE).respond(
            json={"users": {"alice": "found", "ghost": "not_found"}, "groups": {"g": "found"}}
        )
        out = await _authorizer(http).resolve_principals(PRINCIPAL, ["alice", "ghost"], ["g"])
    await http.aclose()
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tok-123"
    assert json.loads(request.content) == {"users": ["alice", "ghost"], "groups": ["g"]}
    assert out.users == {"alice": "found", "ghost": "not_found"}
    assert out.groups == {"g": "found"}


@pytest.mark.parametrize(
    "mock",
    [
        lambda route: route.mock(side_effect=httpx.ReadTimeout("slow")),
        lambda route: route.mock(side_effect=httpx.ConnectError("down")),
        lambda route: route.respond(429, headers={"Retry-After": "5"}),
        lambda route: route.respond(500),
        lambda route: route.respond(503),
        lambda route: route.respond(200, content=b"not json"),
        lambda route: route.respond(200, json={"users": {"alice": "maybe"}, "groups": {}}),
        lambda route: route.respond(200, json={"users": {}, "groups": {}}),
    ],
)
async def test_every_identity_problem_leaves_every_name_unverifiable_and_never_raises(
    mock: Any,
) -> None:
    http = make_client()
    with respx.mock() as router:
        mock(router.post(RESOLVE))
        out = await _authorizer(http).resolve_principals(PRINCIPAL, ["alice", "bob"], ["g"])
    await http.aclose()
    assert out.users == {"alice": "unverifiable", "bob": "unverifiable"}
    assert out.groups == {"g": "unverifiable"}


async def test_resolve_principals_still_raises_identitys_authentication_answers() -> None:
    http = make_client()
    with respx.mock() as router:
        router.post(RESOLVE).respond(401)
        with pytest.raises(ProblemError) as caught:
            await _authorizer(http).resolve_principals(PRINCIPAL, ["alice"], [])
    await http.aclose()
    assert caught.value.status == 401


async def test_resolve_principals_chunks_to_the_route_cap_and_marks_unsendable_names() -> None:
    http = make_client()
    users = [f"u{i}" for i in range(150)]
    too_long = "x" * 257

    def answer(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert len(sent["users"]) <= 100
        return httpx.Response(
            200, json={"users": {n: "found" for n in sent["users"]}, "groups": {}}
        )

    with respx.mock() as router:
        route = router.post(RESOLVE).mock(side_effect=answer)
        out = await _authorizer(http).resolve_principals(PRINCIPAL, [*users, too_long, "u0"], [])
    await http.aclose()
    assert route.call_count == 2
    assert out.users[too_long] == "unverifiable"
    assert all(out.users[name] == "found" for name in users)
    assert len(out.users) == 151


async def test_the_fake_directory_answers_from_its_configured_directory() -> None:
    from dc_auth.testing import FakePrincipalDirectory

    fake = FakePrincipalDirectory(
        users={"alice"}, groups={"payments"}, unverifiable_users={"bob@corp.example"}
    )
    out = await fake.resolve_principals(
        PRINCIPAL, ["Alice", "bob@corp.example", "ghost"], ["Payments", "nope"]
    )
    assert out.users == {"Alice": "found", "bob@corp.example": "unverifiable", "ghost": "not_found"}
    assert out.groups == {"Payments": "found", "nope": "not_found"}


async def test_the_fake_directory_in_an_outage_answers_every_name_unverifiable() -> None:
    from dc_auth.testing import FakePrincipalDirectory

    fake = FakePrincipalDirectory(users={"alice"}, groups={"payments"}, outage=True)
    out = await fake.resolve_principals(PRINCIPAL, ["alice", "ghost"], ["payments", "nope"])
    assert out.users == {"alice": "unverifiable", "ghost": "unverifiable"}
    assert out.groups == {"payments": "unverifiable", "nope": "unverifiable"}


# --- check context and request-id forwarding (D-173) ---------------------------------------


async def test_check_sends_the_asking_service_and_route_template() -> None:
    http = make_client()
    authorizer = RemoteAuthorizer(
        http,
        base_url=BASE,
        bearer=SecretStr("tok-123"),
        timeout_s=2.0,
        context=CheckContext(service="contracts", route="/api/v1/contracts/things/{thing_id}"),
    )
    with respx.mock() as router:
        route = router.post(CHECK).respond(json={"outcome": "not_found", "folder": None})
        await authorizer.check(PRINCIPAL, "contract:read")
    await http.aclose()
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "permission": "contract:read",
        "folder_id": None,
        "fresh": False,
        "service": "contracts",
        "route": "/api/v1/contracts/things/{thing_id}",
    }


async def test_every_post_forwards_the_callers_request_id_when_one_is_set() -> None:
    http = make_client()
    request_id = str(uuid4())
    token = correlation_id.set(request_id)
    try:
        with respx.mock() as router:
            checked = router.post(CHECK).respond(json={"outcome": "allow", "folder": None})
            scoped = router.post(FOLDERS).respond(json={"all": True, "folder_ids": []})
            listed = router.post(GRANTEES).respond(json={"grantees": []})
            authorizer = _authorizer(http)
            await authorizer.check(PRINCIPAL, "x:y")
            await authorizer.accessible_folders(PRINCIPAL, "x:y")
            await authorizer.grantees(PRINCIPAL)
    finally:
        correlation_id.reset(token)
    await http.aclose()
    for route in (checked, scoped, listed):
        assert route.calls.last.request.headers["x-request-id"] == request_id


async def test_no_request_id_header_without_a_correlation_id() -> None:
    http = make_client()
    token = correlation_id.set(None)
    try:
        with respx.mock() as router:
            route = router.post(CHECK).respond(json={"outcome": "allow", "folder": None})
            await _authorizer(http).check(PRINCIPAL, "x:y")
    finally:
        correlation_id.reset(token)
    await http.aclose()
    assert "x-request-id" not in route.calls.last.request.headers


async def test_the_factory_builds_the_context_from_the_app_and_the_route_template(
    fake_issuer: FakeIssuer, asgi_client: Any
) -> None:
    http = make_client()
    settings = AuthSettings(issuers=[fake_issuer.issuer_config()], identity_internal_url=BASE)
    router = APIRouter(prefix="/api/v1/x")

    @router.get("/things/{thing_id}")
    async def thing(
        thing_id: str, principal: Annotated[Principal, require("probe:read")]
    ) -> dict[str, str]:
        return {"id": thing_id}

    app = create_app(
        service="x",
        settings=CoreSettings(service="x", database_url="postgresql+psycopg://x/y"),
        routers=[router],
        lifespan_hooks=[
            auth_lifespan(
                settings, authorizer_factory=remote_authorizer_factory(settings), http_client=http
            )
        ],
    )
    request_id = str(uuid4())
    with respx.mock() as mocked:
        route = mocked.post(CHECK).respond(json={"outcome": "allow", "folder": None})
        async with asgi_client(app) as client:
            response = await client.get(
                "/api/v1/x/things/abc?secret=1",
                headers={
                    "Authorization": f"Bearer {fake_issuer.mint('u')}",
                    "X-Request-ID": request_id,
                },
            )
    await http.aclose()
    assert response.status_code == 200, response.text
    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert body["service"] == "x"
    assert body["route"] == "/api/v1/x/things/{thing_id}"  # the template, never the raw URL
    assert sent.headers["x-request-id"] == request_id


def test_check_context_falls_back_to_unknown_without_a_route() -> None:
    from types import SimpleNamespace

    from dc_auth.authz_wire import check_context

    request = cast(Any, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), scope={}))
    assert check_context(request) == CheckContext(service="unknown", route="unknown")


@pytest.mark.parametrize(
    "field",
    [
        {"service": "Contracts!"},
        {"service": "x" * 33},
        {"service": 7},
        {"route": "/elsewhere/things"},
        {"route": "/api/v1/" + "a" * 256},
        {"route": "/api/v1/things?token=1"},
        {"route": 7},
    ],
)
def test_an_invalid_service_or_route_parses_to_none_never_a_validation_error(
    field: dict[str, Any],
) -> None:
    parsed = AuthzCheckIn.model_validate({"permission": "x:y", **field})
    name = next(iter(field))
    assert getattr(parsed, name) is None
    assert parsed.permission == "x:y"


def test_a_valid_service_and_route_are_kept() -> None:
    parsed = AuthzCheckIn.model_validate(
        {
            "permission": "x:y",
            "service": "contracts",
            "route": "/internal/v1/things/{thing_id}",
        }
    )
    assert parsed.service == "contracts"
    assert parsed.route == "/internal/v1/things/{thing_id}"
