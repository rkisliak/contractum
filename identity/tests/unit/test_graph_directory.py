"""The Microsoft Graph directory client: paging, tenancy, failures and secret hygiene (D-60, D-62).

Graph is stubbed with respx; no network and no Docker.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from dc_auth.testing import ENTRA_TENANT_ID
from dc_core.http import make_client
from dc_identity.directory import DirectoryEntry, DirectoryGroup, DirectoryUnavailable
from dc_identity.directory.graph import GraphDirectoryClient, GraphTokenProvider

pytestmark = pytest.mark.unit

OTHER_TENANT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
UNKNOWN_TENANT = "11111111-2222-4333-8444-555555555555"
GRAPH = "https://graph.example.test/v1.0"
TOKEN_TEMPLATE = "https://login.example.test/{tenant_id}/oauth2/v2.0/token"  # noqa: S105
SECRET = "super-secret-client-value-0123456789"  # noqa: S105
GROUP = "#microsoft.graph.group"
ROLE = "#microsoft.graph.directoryRole"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _token_url(tenant: str) -> str:
    return TOKEN_TEMPLATE.format(tenant_id=tenant)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def _token_route(router: respx.MockRouter, tenant: str = ENTRA_TENANT_ID) -> respx.Route:
    return router.post(_token_url(tenant)).respond(
        json={"access_token": f"token-for-{tenant}", "expires_in": 3600, "token_type": "Bearer"}
    )


@pytest.fixture
def make_graph(
    tmp_path: Path,
) -> Iterator[Callable[..., tuple[GraphDirectoryClient, Clock]]]:
    clients: list[httpx.AsyncClient] = []

    def _make(
        *,
        tenants: frozenset[str] = frozenset({ENTRA_TENANT_ID, OTHER_TENANT}),
        max_pages: int = 20,
    ) -> tuple[GraphDirectoryClient, Clock]:
        secret_file = tmp_path / "graph_secret"
        secret_file.write_text(SECRET + "\n", encoding="utf-8")
        clock = Clock()
        http = make_client(timeout_s=2.0)
        clients.append(http)
        tokens = GraphTokenProvider(
            http,
            token_url_template=TOKEN_TEMPLATE,
            client_id="graph-client-id",
            secret_file=secret_file,
            scope="https://graph.microsoft.com/.default",
            clock=clock,
        )
        return (
            GraphDirectoryClient(
                http, tokens, base_url=GRAPH, max_pages=max_pages, tenants=tenants
            ),
            clock,
        )

    yield _make
    # respx has no pending work here; the clients hold no sockets (all calls were mocked).


def _groups(count: int, *, roles: int = 0) -> list[dict[str, str]]:
    items = [{"@odata.type": GROUP, "id": str(uuid.uuid4())} for _ in range(count)]
    items.extend({"@odata.type": ROLE, "id": str(uuid.uuid4())} for _ in range(roles))
    return items


def _stub_user(
    router: respx.MockRouter, oid: str, *, enabled: bool = True, name: str = "Alice"
) -> respx.Route:
    return router.get(f"{GRAPH}/users/{oid}").respond(
        json={"id": oid, "accountEnabled": enabled, "displayName": name}
    )


async def test_a_user_in_250_groups_across_three_pages_resolves_all_of_them(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    first, second, third = _groups(100), _groups(100), _groups(50, roles=2)
    expected = {
        item["id"]
        for page in (first, second, third)
        for item in page
        if item["@odata.type"] == GROUP
    }
    roles = {item["id"] for item in third if item["@odata.type"] == ROLE}
    _token_route(router)
    _stub_user(router, oid)
    page_url = f"{GRAPH}/users/{oid}/transitiveMemberOf"
    router.get(page_url, params={"$select": "id", "$top": "999"}).respond(
        json={
            "value": first,
            "@odata.nextLink": f"{GRAPH}/users/{oid}/transitiveMemberOf?$skiptoken=p2",
        }
    )
    router.get(page_url, params={"$skiptoken": "p2"}).respond(
        json={
            "value": second,
            "@odata.nextLink": f"{GRAPH}/users/{oid}/transitiveMemberOf?$skiptoken=p3",
        }
    )
    router.get(page_url, params={"$skiptoken": "p3"}).respond(json={"value": third})
    entry = await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert entry.found is True
    assert entry.account_enabled is True
    assert entry.display_name == "Alice"
    assert len(entry.group_ids) == 250
    assert entry.group_ids == frozenset(expected)
    assert entry.group_ids.isdisjoint(roles)


async def test_the_membership_request_carries_no_eventual_consistency_header(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    _stub_user(router, oid)
    members = router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(json={"value": []})
    await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    request = members.calls.last.request
    assert "consistencylevel" not in {name.lower() for name in request.headers}
    assert request.url.params["$select"] == "id"
    assert request.url.params["$top"] == "999"
    assert request.headers["authorization"] == f"Bearer token-for-{ENTRA_TENANT_ID}"


async def test_a_404_for_the_principal_is_not_found_and_membership_is_never_requested(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    router.get(f"{GRAPH}/users/{oid}").respond(
        404, json={"error": {"code": "Request_ResourceNotFound"}}
    )
    members = router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(json={"value": []})
    entry = await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert entry == DirectoryEntry(found=False)
    assert members.call_count == 0


async def test_a_disabled_account_is_reported_as_not_enabled(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    _stub_user(router, oid, enabled=False)
    router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(json={"value": []})
    entry = await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert entry.found is True
    assert entry.account_enabled is False


async def test_service_principals_use_their_own_path_segment(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    group = str(uuid.uuid4())
    _token_route(router)
    router.get(f"{GRAPH}/servicePrincipals/{oid}").respond(
        json={"id": oid, "accountEnabled": True, "displayName": "pipeline"}
    )
    router.get(f"{GRAPH}/servicePrincipals/{oid}/transitiveMemberOf").respond(
        json={"value": [{"@odata.type": GROUP, "id": group.upper()}]}
    )
    entry = await graph.lookup(ENTRA_TENANT_ID, "service_principal", oid)
    assert entry.display_name == "pipeline"
    assert entry.group_ids == frozenset({group})


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(500),
        httpx.Response(503),
        httpx.Response(401),
        httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}}),
        httpx.Response(418),
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.ConnectTimeout("slow"),
        httpx.ConnectError("down"),
    ],
    ids=["429", "500", "503", "401", "403", "418", "bad-json", "timeout", "transport"],
)
async def test_every_graph_failure_is_directory_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any], outcome: Any
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    if isinstance(outcome, Exception):
        router.get(f"{GRAPH}/users/{oid}").mock(side_effect=outcome)
    else:
        router.get(f"{GRAPH}/users/{oid}").mock(return_value=outcome)
    with pytest.raises(DirectoryUnavailable):
        await graph.lookup(ENTRA_TENANT_ID, "user", oid)


async def test_a_failure_on_the_membership_call_is_also_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    _stub_user(router, oid)
    router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(503)
    with pytest.raises(DirectoryUnavailable):
        await graph.lookup(ENTRA_TENANT_ID, "user", oid)


async def test_a_token_endpoint_failure_is_unavailable_and_never_leaks_the_secret(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    router.post(_token_url(ENTRA_TENANT_ID)).respond(
        400, json={"error": "invalid_client", "error_description": f"bad {SECRET}"}
    )
    user = _stub_user(router, oid)
    with pytest.raises(DirectoryUnavailable) as info:
        await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert SECRET not in str(info.value)
    assert SECRET not in repr(info.value)
    assert user.call_count == 0


async def test_a_token_response_without_an_access_token_is_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    router.post(_token_url(ENTRA_TENANT_ID)).respond(json={"expires_in": 3600})
    with pytest.raises(DirectoryUnavailable):
        await graph.lookup(ENTRA_TENANT_ID, "user", str(uuid.uuid4()))


async def test_a_next_link_on_another_host_is_never_followed(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    _stub_user(router, oid)
    router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(
        json={"value": _groups(2), "@odata.nextLink": "https://evil.example.test/steal?x=1"}
    )
    evil = router.get(host="evil.example.test").respond(json={"value": []})
    with pytest.raises(DirectoryUnavailable):
        await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert evil.call_count == 0


async def test_a_next_link_downgraded_to_http_is_never_followed(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    _stub_user(router, oid)
    router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(
        json={"value": [], "@odata.nextLink": f"http://graph.example.test/v1.0/users/{oid}/x"}
    )
    plain = router.get(scheme="http").respond(json={"value": []})
    with pytest.raises(DirectoryUnavailable):
        await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert plain.call_count == 0


async def test_more_pages_than_the_cap_is_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph(max_pages=3)
    oid = str(uuid.uuid4())
    _token_route(router)
    _stub_user(router, oid)
    endless = router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(
        json={
            "value": _groups(1),
            "@odata.nextLink": f"{GRAPH}/users/{oid}/transitiveMemberOf?$skiptoken=again",
        }
    )
    with pytest.raises(DirectoryUnavailable):
        await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert endless.call_count == 3


@pytest.mark.parametrize("bad", ["ABC", "4F1C2D3E-5A6B-4C7D-8E9F-0A1B2C3D4E5F", "../users", ""])
async def test_a_malformed_object_id_is_refused_before_any_http_call(
    router: respx.MockRouter, make_graph: Callable[..., Any], bad: str
) -> None:
    graph, _clock = make_graph()
    anything = router.route().respond(json={})
    with pytest.raises(ValueError):
        await graph.lookup(ENTRA_TENANT_ID, "user", bad)
    with pytest.raises(ValueError):
        await graph.group(ENTRA_TENANT_ID, bad)
    assert anything.call_count == 0


async def test_a_tenant_that_is_not_configured_is_refused_before_any_http_call(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    anything = router.route().respond(json={})
    with pytest.raises(ValueError):
        await graph.lookup(UNKNOWN_TENANT, "user", str(uuid.uuid4()))
    with pytest.raises(ValueError):
        await graph.group(UNKNOWN_TENANT, str(uuid.uuid4()))
    assert anything.call_count == 0


async def test_an_empty_answer_is_an_empty_group_set(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    _stub_user(router, oid)
    router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(json={"value": []})
    entry = await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert entry.group_ids == frozenset()
    assert entry.found is True


async def test_page_boundaries_and_item_order_never_change_the_group_set(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    oid = str(uuid.uuid4())
    items = _groups(30, roles=3)
    results: list[frozenset[str]] = []
    for seed, size in ((1, 30), (2, 7), (3, 11)):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)  # noqa: S311
        pages = [shuffled[i : i + size] for i in range(0, len(shuffled), size)]
        with respx.mock(assert_all_called=False) as local:
            _token_route(local)
            _stub_user(local, oid)
            for index, page in enumerate(pages):
                params = (
                    {"$select": "id", "$top": "999"} if index == 0 else {"$skiptoken": f"p{index}"}
                )
                body: dict[str, Any] = {"value": page}
                if index + 1 < len(pages):
                    body["@odata.nextLink"] = (
                        f"{GRAPH}/users/{oid}/transitiveMemberOf?$skiptoken=p{index + 1}"
                    )
                local.get(f"{GRAPH}/users/{oid}/transitiveMemberOf", params=params).respond(
                    json=body
                )
            graph, _clock = make_graph()
            results.append((await graph.lookup(ENTRA_TENANT_ID, "user", oid)).group_ids)
    assert results[0] == results[1] == results[2]
    assert len(results[0]) == 30


async def test_two_lookups_in_one_tenant_request_one_token(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    token = _token_route(router)
    for oid in (first, second):
        _stub_user(router, oid)
        router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(json={"value": []})
    await graph.lookup(ENTRA_TENANT_ID, "user", first)
    await graph.lookup(ENTRA_TENANT_ID, "user", second)
    assert token.call_count == 1
    form = token.calls.last.request.content.decode()
    assert "grant_type=client_credentials" in form
    assert "client_id=graph-client-id" in form
    assert f"client_secret={SECRET}" in form
    assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in form


async def test_an_expired_token_is_requested_again(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, clock = make_graph()
    oid = str(uuid.uuid4())
    token = _token_route(router)
    _stub_user(router, oid)
    router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(json={"value": []})
    await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    clock.now += 3600 - 61  # still inside expires_in - 60
    await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert token.call_count == 1
    clock.now += 2
    await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert token.call_count == 2


async def test_each_configured_tenant_gets_its_own_token(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    token_a = _token_route(router, ENTRA_TENANT_ID)
    token_b = _token_route(router, OTHER_TENANT)
    users = [_stub_user(router, oid) for oid in (first, second)]
    members = [
        router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(json={"value": []})
        for oid in (first, second)
    ]
    await graph.lookup(ENTRA_TENANT_ID, "user", first)
    await graph.lookup(OTHER_TENANT, "user", second)
    assert (token_a.call_count, token_b.call_count) == (1, 1)
    assert (
        users[0].calls.last.request.headers["authorization"]
        == f"Bearer token-for-{ENTRA_TENANT_ID}"
    )
    assert (
        users[1].calls.last.request.headers["authorization"] == f"Bearer token-for-{OTHER_TENANT}"
    )
    assert (
        members[1].calls.last.request.headers["authorization"] == f"Bearer token-for-{OTHER_TENANT}"
    )


async def test_group_lookup_returns_the_group_or_none(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    found, missing = str(uuid.uuid4()), str(uuid.uuid4())
    _token_route(router)
    route = router.get(f"{GRAPH}/groups/{found}").respond(
        json={"id": found.upper(), "displayName": "Data Engineers"}
    )
    router.get(f"{GRAPH}/groups/{missing}").respond(404, json={"error": {}})
    assert await graph.group(ENTRA_TENANT_ID, found) == DirectoryGroup(found, "Data Engineers")
    assert await graph.group(ENTRA_TENANT_ID, missing) is None
    assert route.calls.last.request.url.params["$select"] == "id,displayName"


async def test_group_lookup_failure_is_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    router.get(f"{GRAPH}/groups/{oid}").respond(403)
    with pytest.raises(DirectoryUnavailable):
        await graph.group(ENTRA_TENANT_ID, oid)


def _app_settings(tmp_path: Path, **overrides: Any) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from dc_identity.settings import IdentitySettings

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = tmp_path / "jwt_signing_key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return IdentitySettings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
        jwt_signing_key_file=key_file,
        bootstrap_secret_file=tmp_path / "absent",
        **overrides,
    )


def _entra_auth(fake_issuer: Any) -> Any:
    from dc_auth.issuers import AuthSettings

    return AuthSettings(issuers=[fake_issuer.entra_issuer_config()])


def test_identity_refuses_to_start_with_an_entra_issuer_and_no_graph_directory(
    tmp_path: Path, fake_issuer: Any
) -> None:
    from dc_identity.main import create_identity_app

    with pytest.raises(RuntimeError, match="DC_GRAPH_CLIENT_ID"):
        create_identity_app(_app_settings(tmp_path), _entra_auth(fake_issuer))


def test_identity_starts_with_an_entra_issuer_when_a_graph_client_is_configured(
    tmp_path: Path, fake_issuer: Any
) -> None:
    from dc_identity.directory.cache import DirectoryCache
    from dc_identity.main import create_identity_app

    app = create_identity_app(
        _app_settings(tmp_path, graph_client_id="graph-client-id"), _entra_auth(fake_issuer)
    )
    assert isinstance(app.state.directory, DirectoryCache)


def test_identity_starts_without_any_entra_issuer_and_without_a_graph_client(
    tmp_path: Path,
) -> None:
    from dc_identity.directory import UnconfiguredDirectory
    from dc_identity.main import create_identity_app

    app = create_identity_app(_app_settings(tmp_path))
    assert isinstance(app.state.directory, UnconfiguredDirectory)


def test_an_injected_directory_is_wrapped_in_the_cache_unless_the_ttl_is_zero(
    tmp_path: Path, fake_issuer: Any
) -> None:
    from dc_identity.directory.cache import DirectoryCache
    from dc_identity.directory.fake import FakeDirectoryClient
    from dc_identity.main import create_identity_app

    fake = FakeDirectoryClient()
    cached = create_identity_app(
        _app_settings(tmp_path, directory_cache_ttl_s=5), _entra_auth(fake_issuer), fake
    )
    plain = create_identity_app(
        _app_settings(tmp_path, directory_cache_ttl_s=0), _entra_auth(fake_issuer), fake
    )
    assert isinstance(cached.state.directory, DirectoryCache)
    assert plain.state.directory is fake


def test_the_token_url_template_must_contain_the_tenant_placeholder(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _app_settings(tmp_path, graph_token_url_template="https://login.example.test/token")
    with pytest.raises(ValueError):
        _app_settings(tmp_path, directory_cache_ttl_s=-1)


# --- Directory search (quick 261119-ofa) ----------------------------------------------------------


def _search_route(router: respx.MockRouter, segment: str, payload: dict[str, Any]) -> respx.Route:
    return router.get(f"{GRAPH}/{segment}").respond(json=payload)


def _query(route: respx.Route) -> dict[str, str]:
    request = route.calls.last.request
    return dict(request.url.params)


async def test_a_user_search_asks_graph_with_the_exact_search_phrase_and_consistency_header(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    route = _search_route(
        router,
        "users",
        {
            "value": [
                {
                    "id": oid.upper(),
                    "displayName": "Alice A",
                    "mail": "alice@mail.test",
                    "userPrincipalName": "alice@upn.test",
                },
                {"id": str(uuid.uuid4()), "displayName": "Ali B", "mail": "ali@mail.test"},
            ]
        },
    )
    result = await graph.search(ENTRA_TENANT_ID, "user", "ali")
    assert _query(route) == {
        "$search": '"displayName:ali" OR "mail:ali" OR "userPrincipalName:ali"',
        "$top": "20",
        "$select": "id,displayName,mail,userPrincipalName",
    }
    assert route.calls.last.request.headers["ConsistencyLevel"] == "eventual"
    assert (
        route.calls.last.request.headers["Authorization"] == f"Bearer token-for-{ENTRA_TENANT_ID}"
    )
    assert result.truncated is False
    first, second = result.items
    assert first.object_id == oid  # lowercased
    assert first.display_name == "Alice A"
    assert first.secondary == "alice@upn.test"  # UPN wins over mail
    assert first.kind == "user"
    assert second.secondary == "ali@mail.test"  # falls back to mail


async def test_a_group_search_uses_display_name_and_mail_and_shows_mail_then_description(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    with_mail, with_description = str(uuid.uuid4()), str(uuid.uuid4())
    _token_route(router)
    route = _search_route(
        router,
        "groups",
        {
            "value": [
                {"id": with_mail, "displayName": "Eng", "mail": "eng@x.test", "description": "d"},
                {"id": with_description, "displayName": "Ops", "description": "Operations"},
            ]
        },
    )
    result = await graph.search(ENTRA_TENANT_ID, "group", "eng")
    assert _query(route) == {
        "$search": '"displayName:eng" OR "mail:eng"',
        "$top": "20",
        "$select": "id,displayName,mail,description",
    }
    assert [m.secondary for m in result.items] == ["eng@x.test", "Operations"]


async def test_a_service_principal_search_uses_display_name_and_shows_the_app_id(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid, app_id = str(uuid.uuid4()), str(uuid.uuid4())
    _token_route(router)
    route = _search_route(
        router, "servicePrincipals", {"value": [{"id": oid, "displayName": "Etl", "appId": app_id}]}
    )
    result = await graph.search(ENTRA_TENANT_ID, "service_principal", "etl")
    assert _query(route) == {
        "$search": '"displayName:etl"',
        "$top": "20",
        "$select": "id,displayName,appId",
    }
    assert route.calls.last.request.headers["ConsistencyLevel"] == "eventual"
    assert [(m.object_id, m.secondary) for m in result.items] == [(oid, app_id)]


async def test_a_next_link_marks_the_answer_truncated_and_is_never_followed(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    _token_route(router)
    route = _search_route(
        router,
        "users",
        {
            "value": [{"id": str(uuid.uuid4()), "displayName": f"U{i}"} for i in range(20)],
            "@odata.nextLink": f"{GRAPH}/users?$skiptoken=more",
        },
    )
    result = await graph.search(ENTRA_TENANT_ID, "user", "ab")
    assert result.truncated is True
    assert len(result.items) == 20
    assert route.call_count == 1


async def test_the_search_sanitizes_quotes_and_backslashes_before_graph_sees_them(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    _token_route(router)
    route = _search_route(router, "servicePrincipals", {"value": []})
    await graph.search(ENTRA_TENANT_ID, "service_principal", 'et"l\\ \x00ok')
    assert _query(route)["$search"] == '"displayName:etl ok"'


async def test_a_short_query_is_rejected_before_any_call(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    with pytest.raises(ValueError):
        await graph.search(ENTRA_TENANT_ID, "user", '"a"')
    assert router.calls.call_count == 0


@pytest.mark.parametrize(
    ("kind", "segment"),
    [("user", "users"), ("group", "groups"), ("service_principal", "servicePrincipals")],
)
async def test_a_guid_query_is_an_exact_lookup_by_path_in_any_case(
    router: respx.MockRouter, make_graph: Callable[..., Any], kind: str, segment: str
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    by_id = router.get(f"{GRAPH}/{segment}/{oid}").respond(json={"id": oid, "displayName": "Exact"})
    listing = router.get(f"{GRAPH}/{segment}").respond(json={"value": []})
    result = await graph.search(ENTRA_TENANT_ID, kind, oid.upper())  # type: ignore[arg-type]
    assert [m.display_name for m in result.items] == ["Exact"]
    assert by_id.call_count == 1
    assert "$search" not in str(by_id.calls.last.request.url)
    assert result.truncated is False
    if kind == "service_principal":
        assert _query(listing)["$filter"] == f"appId eq '{oid}'"
        assert listing.calls.last.request.headers["ConsistencyLevel"] == "eventual"
    else:
        assert listing.call_count == 0  # no $search call for a GUID


async def test_a_guid_the_directory_does_not_know_yields_no_item(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    router.get(f"{GRAPH}/users/{oid}").respond(
        404, json={"error": {"code": "Request_ResourceNotFound"}}
    )
    result = await graph.search(ENTRA_TENANT_ID, "user", oid)
    assert result.items == ()


async def test_a_service_principal_guid_matching_by_object_id_and_app_id_is_listed_once(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid, other = str(uuid.uuid4()), str(uuid.uuid4())
    _token_route(router)
    router.get(f"{GRAPH}/servicePrincipals/{oid}").respond(json={"id": oid, "displayName": "Both"})
    router.get(f"{GRAPH}/servicePrincipals").respond(
        json={"value": [{"id": oid, "displayName": "Both"}, {"id": other, "displayName": "App"}]}
    )
    result = await graph.search(ENTRA_TENANT_ID, "service_principal", oid)
    assert [m.object_id for m in result.items] == [oid, other]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_a_refused_or_failing_search_is_directory_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any], status: int
) -> None:
    graph, _clock = make_graph()
    _token_route(router)
    router.get(f"{GRAPH}/groups").respond(status)
    with pytest.raises(DirectoryUnavailable):
        await graph.search(ENTRA_TENANT_ID, "group", "eng")


async def test_a_404_on_a_list_call_a_transport_error_and_bad_json_are_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    _token_route(router)
    listing = router.get(f"{GRAPH}/users")
    listing.respond(404, json={})
    with pytest.raises(DirectoryUnavailable):
        await graph.search(ENTRA_TENANT_ID, "user", "ali")
    listing.mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(DirectoryUnavailable):
        await graph.search(ENTRA_TENANT_ID, "user", "ali")
    listing.mock(side_effect=None)
    listing.respond(200, content=b"not json")
    with pytest.raises(DirectoryUnavailable):
        await graph.search(ENTRA_TENANT_ID, "user", "ali")
    listing.respond(200, json={"nope": []})
    with pytest.raises(DirectoryUnavailable):
        await graph.search(ENTRA_TENANT_ID, "user", "ali")


async def test_a_tenant_that_is_not_configured_is_a_value_error_before_any_call(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    with pytest.raises(ValueError):
        await graph.search(UNKNOWN_TENANT, "user", "ali")
    assert router.calls.call_count == 0


async def test_the_query_never_appears_in_a_log_event(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    import structlog

    graph, _clock = make_graph()
    _token_route(router)
    router.get(f"{GRAPH}/users").respond(200, json={"value": []})
    router.get(f"{GRAPH}/groups").respond(500)
    with structlog.testing.capture_logs() as logs:
        await graph.search(ENTRA_TENANT_ID, "user", "needle-q")
        with pytest.raises(DirectoryUnavailable):
            await graph.search(ENTRA_TENANT_ID, "group", "needle-q")
    assert logs
    assert "needle-q" not in repr(logs)
    assert SECRET not in repr(logs)


# --- Group members (quick 261119-wp6) ---------------------------------------------------------

USER_T = "#microsoft.graph.user"
SP_T = "#microsoft.graph.servicePrincipal"


def _members_url(oid: str) -> str:
    return f"{GRAPH}/groups/{oid}/transitiveMembers"


def _member(kind: str, oid: str, **props: Any) -> dict[str, Any]:
    return {"@odata.type": kind, "id": oid, **props}


async def test_group_members_ask_graph_for_transitive_members_and_map_every_kind(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    group, user, nested, sp, device = (str(uuid.uuid4()) for _ in range(5))
    _token_route(router)
    route = router.get(_members_url(group)).respond(
        json={
            "@odata.count": 5,
            "value": [
                _member(
                    USER_T,
                    user.upper(),
                    displayName="Alice A",
                    mail="alice@mail.test",
                    userPrincipalName="alice@upn.test",
                    accountEnabled=False,
                ),
                _member(GROUP, nested, displayName="Nested", mail="nested@mail.test"),
                _member(SP_T, sp, displayName="Pipeline", appId="app-1", accountEnabled=True),
                _member("#microsoft.graph.device", device, displayName="Laptop"),
                {"@odata.type": USER_T, "id": "not-a-guid"},
            ],
        }
    )
    result = await graph.group_members(ENTRA_TENANT_ID, group)
    assert result is not None
    assert _query(route) == {
        "$select": "id,displayName,mail,userPrincipalName,appId,accountEnabled",
        "$top": "201",
        "$count": "true",
    }
    assert route.calls.last.request.headers["ConsistencyLevel"] == "eventual"
    assert result.total == 5
    assert result.truncated is False
    u, g, s, o = result.items
    assert (u.object_id, u.kind, u.secondary, u.account_enabled) == (
        user,
        "user",
        "alice@upn.test",  # UPN wins over mail
        False,
    )
    assert (g.kind, g.secondary, g.account_enabled) == ("group", "nested@mail.test", None)
    assert (s.kind, s.secondary, s.account_enabled) == ("service_principal", "app-1", True)
    assert (o.kind, o.secondary, o.display_name) == ("other", None, "Laptop")


async def test_group_members_follow_next_links_across_pages(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    group = str(uuid.uuid4())
    ids = [str(uuid.uuid4()) for _ in range(5)]
    _token_route(router)
    second = f"{GRAPH}/groups/{group}/transitiveMembers?$skiptoken=2"
    router.get(_members_url(group), params={"$count": "true"}).respond(
        json={
            "value": [_member(USER_T, i) for i in ids[:2]],
            "@odata.nextLink": second,
        }
    )
    router.get(second).respond(json={"value": [_member(USER_T, i) for i in ids[2:]]})
    result = await graph.group_members(ENTRA_TENANT_ID, group)
    assert result is not None
    assert [m.object_id for m in result.items] == ids
    assert result.truncated is False
    assert result.total is None


async def test_group_members_stop_at_the_limit_and_report_truncated(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    group = str(uuid.uuid4())
    ids = [str(uuid.uuid4()) for _ in range(6)]
    _token_route(router)
    route = router.get(_members_url(group)).respond(
        json={"@odata.count": 6, "value": [_member(USER_T, i) for i in ids]}
    )
    result = await graph.group_members(ENTRA_TENANT_ID, group, limit=4)
    assert result is not None
    assert [m.object_id for m in result.items] == ids[:4]
    assert result.truncated is True
    assert result.total == 6
    assert _query(route)["$top"] == "5"
    assert route.call_count == 1


async def test_group_members_at_the_cap_with_a_next_link_are_truncated_without_a_second_call(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    group = str(uuid.uuid4())
    ids = [str(uuid.uuid4()) for _ in range(3)]
    _token_route(router)
    route = router.get(_members_url(group)).respond(
        json={
            "value": [_member(USER_T, i) for i in ids],
            "@odata.nextLink": f"{GRAPH}/groups/{group}/transitiveMembers?$skiptoken=3",
        }
    )
    result = await graph.group_members(ENTRA_TENANT_ID, group, limit=3)
    assert result is not None
    assert len(result.items) == 3
    assert result.truncated is True
    assert route.call_count == 1


async def test_group_members_exactly_at_the_cap_without_more_are_not_truncated(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    group = str(uuid.uuid4())
    ids = [str(uuid.uuid4()) for _ in range(3)]
    _token_route(router)
    router.get(_members_url(group)).respond(json={"value": [_member(USER_T, i) for i in ids]})
    result = await graph.group_members(ENTRA_TENANT_ID, group, limit=3)
    assert result is not None
    assert len(result.items) == 3
    assert result.truncated is False


async def test_an_unknown_group_is_none_and_a_foreign_next_link_is_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    group = str(uuid.uuid4())
    _token_route(router)
    listing = router.get(_members_url(group))
    listing.respond(404, json={})
    assert await graph.group_members(ENTRA_TENANT_ID, group) is None
    listing.respond(
        200,
        json={
            "value": [_member(USER_T, str(uuid.uuid4()))],
            "@odata.nextLink": "https://evil.example.test/steal",
        },
    )
    with pytest.raises(DirectoryUnavailable):
        await graph.group_members(ENTRA_TENANT_ID, group)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_a_refused_or_failing_member_listing_is_directory_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any], status: int
) -> None:
    graph, _clock = make_graph()
    group = str(uuid.uuid4())
    _token_route(router)
    router.get(_members_url(group)).respond(status)
    with pytest.raises(DirectoryUnavailable):
        await graph.group_members(ENTRA_TENANT_ID, group)


async def test_member_listing_transport_error_bad_json_page_cap_and_bad_input(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph(max_pages=2)
    group = str(uuid.uuid4())
    _token_route(router)
    listing = router.get(_members_url(group))
    listing.mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(DirectoryUnavailable):
        await graph.group_members(ENTRA_TENANT_ID, group)
    listing.mock(side_effect=None)
    listing.respond(200, content=b"not json")
    with pytest.raises(DirectoryUnavailable):
        await graph.group_members(ENTRA_TENANT_ID, group)
    listing.respond(200, json={"nope": []})
    with pytest.raises(DirectoryUnavailable):
        await graph.group_members(ENTRA_TENANT_ID, group)
    # An endless chain of one-member pages hits the page cap before the member cap.
    listing.respond(
        200,
        json={
            "value": [_member(USER_T, str(uuid.uuid4()))],
            "@odata.nextLink": f"{GRAPH}/groups/{group}/transitiveMembers",
        },
    )
    with pytest.raises(DirectoryUnavailable):
        await graph.group_members(ENTRA_TENANT_ID, group)
    with pytest.raises(ValueError):
        await graph.group_members(ENTRA_TENANT_ID, "not-a-guid")
    with pytest.raises(ValueError):
        await graph.group_members(UNKNOWN_TENANT, group)
    with pytest.raises(ValueError):
        await graph.group_members(ENTRA_TENANT_ID, group, limit=0)


async def test_no_member_name_or_secret_appears_in_a_log_event(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    import structlog

    graph, _clock = make_graph()
    group = str(uuid.uuid4())
    _token_route(router)
    router.get(_members_url(group)).respond(
        json={"value": [_member(USER_T, str(uuid.uuid4()), displayName="Zed Zeppelin")]}
    )
    with structlog.testing.capture_logs() as logs:
        await graph.group_members(ENTRA_TENANT_ID, group)
    assert logs
    assert "Zed" not in repr(logs)
    assert SECRET not in repr(logs)


# --- Defensive answers: malformed Graph documents fail closed or are skipped ---------------------


async def test_concurrent_first_lookups_in_one_tenant_share_one_token_request(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

    async def slow_token(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"access_token": "shared", "expires_in": 3600})

    token = router.post(_token_url(ENTRA_TENANT_ID)).mock(side_effect=slow_token)
    for oid in (first, second):
        _stub_user(router, oid)
        router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(json={"value": []})
    entries = await asyncio.gather(
        graph.lookup(ENTRA_TENANT_ID, "user", first), graph.lookup(ENTRA_TENANT_ID, "user", second)
    )
    assert [entry.found for entry in entries] == [True, True]
    assert token.call_count == 1


@pytest.mark.parametrize("access_token", ["", 42, None])
async def test_a_token_response_with_an_empty_or_non_string_token_is_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any], access_token: object
) -> None:
    graph, _clock = make_graph()
    router.post(_token_url(ENTRA_TENANT_ID)).respond(
        json={"access_token": access_token, "expires_in": 3600}
    )
    with pytest.raises(DirectoryUnavailable, match="carried no access token"):
        await graph.lookup(ENTRA_TENANT_ID, "user", str(uuid.uuid4()))


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([{"id": "x"}], "unexpected document"),
        ({"id": "x", "displayName": "No Flag"}, "did not report accountEnabled"),
        ({"id": "x", "accountEnabled": "yes"}, "did not report accountEnabled"),
    ],
)
async def test_a_principal_document_graph_cannot_vouch_for_is_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any], document: object, message: str
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    router.get(f"{GRAPH}/users/{oid}").respond(json=document)
    with pytest.raises(DirectoryUnavailable, match=message):
        await graph.lookup(ENTRA_TENANT_ID, "user", oid)


@pytest.mark.parametrize(
    ("status", "document", "message"),
    [
        (404, {}, "lost the principal mid-lookup"),
        (200, {"value": {"not": "a list"}}, "without a value array"),
    ],
)
async def test_a_membership_page_that_vanishes_or_has_no_value_array_is_unavailable(
    router: respx.MockRouter,
    make_graph: Callable[..., Any],
    status: int,
    document: dict[str, Any],
    message: str,
) -> None:
    graph, _clock = make_graph()
    oid = str(uuid.uuid4())
    _token_route(router)
    _stub_user(router, oid)
    router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(status, json=document)
    with pytest.raises(DirectoryUnavailable, match=message):
        await graph.lookup(ENTRA_TENANT_ID, "user", oid)


async def test_group_entries_without_a_guid_id_are_left_out_of_the_group_set(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    oid, kept = str(uuid.uuid4()), str(uuid.uuid4())
    _token_route(router)
    _stub_user(router, oid)
    router.get(f"{GRAPH}/users/{oid}/transitiveMemberOf").respond(
        json={
            "value": [
                {"@odata.type": GROUP, "id": kept.upper()},
                {"@odata.type": GROUP, "id": "not-a-guid"},
                {"@odata.type": GROUP, "id": 7},
                {"@odata.type": GROUP},
                "not-a-dict",
            ]
        }
    )
    entry = await graph.lookup(ENTRA_TENANT_ID, "user", oid)
    assert entry.group_ids == frozenset({kept})


async def test_search_hits_that_are_not_objects_or_lack_a_guid_are_skipped(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    good = str(uuid.uuid4())
    _token_route(router)
    _search_route(
        router,
        "groups",
        {
            "value": [
                "not-a-dict",
                {"id": "not-a-guid", "displayName": "Bad"},
                {"displayName": "No Id"},
                {"id": good, "displayName": "Data Engineering"},
            ]
        },
    )
    result = await graph.search(ENTRA_TENANT_ID, "group", "data")
    assert [(m.object_id, m.display_name) for m in result.items] == [(good, "Data Engineering")]


async def test_a_member_listing_that_loses_the_group_after_the_first_page_is_unavailable(
    router: respx.MockRouter, make_graph: Callable[..., Any]
) -> None:
    graph, _clock = make_graph()
    group = str(uuid.uuid4())
    _token_route(router)
    listing = _members_url(group)
    router.get(listing, params={"$skiptoken": "p2"}).respond(404, json={})
    router.get(listing).respond(
        json={
            "value": ["not-a-dict", _member(USER_T, str(uuid.uuid4()), displayName="A")],
            "@odata.nextLink": f"{listing}?$skiptoken=p2",
        }
    )
    with pytest.raises(DirectoryUnavailable, match="lost the group mid-listing"):
        await graph.group_members(ENTRA_TENANT_ID, group)


async def test_closing_the_client_closes_its_http_pool(make_graph: Callable[..., Any]) -> None:
    graph, _clock = make_graph()
    await graph.aclose()
    with pytest.raises(RuntimeError):
        await graph.lookup(ENTRA_TENANT_ID, "user", str(uuid.uuid4()))
