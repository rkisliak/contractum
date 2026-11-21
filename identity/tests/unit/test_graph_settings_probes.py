"""Graph probes behind Admin > Settings: connection test and secret expiry (quick 261120-0yc).

Graph is stubbed with respx. The expiry call must keep only ``endDateTime`` of each credential:
the fake answers carry a ``hint``, a ``keyId`` and a ``displayName`` that must never come back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from dc_auth.testing import ENTRA_TENANT_ID
from dc_core.http import make_client
from dc_identity.directory import UnconfiguredDirectory
from dc_identity.directory.graph import GraphDirectoryClient, GraphTokenProvider

pytestmark = pytest.mark.unit

GRAPH = "https://graph.example.test/v1.0"
TOKEN_URL = f"https://login.example.test/{ENTRA_TENANT_ID}/oauth2/v2.0/token"
APP_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SECRET = "super-secret-client-value-0123456789"  # noqa: S105
OTHER = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def _graph(
    tmp_path: Path, *, secret: str | None = SECRET, client_id: str = APP_ID
) -> GraphDirectoryClient:
    secret_file = tmp_path / "graph_secret"
    if secret is not None:
        secret_file.write_text(secret + "\n", encoding="utf-8")
    http = make_client(timeout_s=2.0)
    tokens = GraphTokenProvider(
        http,
        token_url_template="https://login.example.test/{tenant_id}/oauth2/v2.0/token",
        client_id=client_id,
        secret_file=secret_file,
        scope="https://graph.microsoft.com/.default",
    )
    return GraphDirectoryClient(
        http, tokens, base_url=GRAPH, max_pages=5, tenants=frozenset({ENTRA_TENANT_ID})
    )


def _token(router: respx.MockRouter) -> None:
    router.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})


async def test_connection_ok_asks_the_service_principal_filter(
    router: respx.MockRouter, tmp_path: Path
) -> None:
    _token(router)
    call = router.get(f"{GRAPH}/servicePrincipals").respond(json={"value": [{"id": "x"}]})
    assert await _graph(tmp_path).check_connection(ENTRA_TENANT_ID) == "ok"
    params = call.calls.last.request.url.params
    assert params["$filter"] == f"appId eq '{APP_ID}'"
    assert params["$select"] == "id"
    assert params["$top"] == "1"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "graph-forbidden"),
        (401, "graph-forbidden"),
        (429, "graph-throttled"),
        (500, "graph-error"),
        (404, "graph-error"),
    ],
)
async def test_connection_maps_graph_statuses_to_fixed_codes(
    router: respx.MockRouter, tmp_path: Path, status: int, expected: str
) -> None:
    _token(router)
    router.get(f"{GRAPH}/servicePrincipals").respond(status, json={"error": SECRET})
    assert await _graph(tmp_path).check_connection(ENTRA_TENANT_ID) == expected


async def test_connection_codes_for_token_and_transport_failures(
    router: respx.MockRouter, tmp_path: Path
) -> None:
    router.post(TOKEN_URL).respond(401, json={"error": "invalid_client"})
    assert await _graph(tmp_path).check_connection(ENTRA_TENANT_ID) == "token-refused"
    router.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    assert await _graph(tmp_path).check_connection(ENTRA_TENANT_ID) == "graph-unreachable"
    _token(router)
    router.get(f"{GRAPH}/servicePrincipals").mock(side_effect=httpx.ConnectError("down"))
    assert await _graph(tmp_path).check_connection(ENTRA_TENANT_ID) == "graph-unreachable"
    router.get(f"{GRAPH}/servicePrincipals").respond(200, text="not json")
    assert await _graph(tmp_path).check_connection(ENTRA_TENANT_ID) == "graph-error"


async def test_connection_without_a_secret_or_for_an_unconfigured_tenant_is_not_configured(
    router: respx.MockRouter, tmp_path: Path
) -> None:
    assert await _graph(tmp_path, secret=None).check_connection(ENTRA_TENANT_ID) == "not-configured"
    assert await _graph(tmp_path).check_connection(OTHER) == "not-configured"
    assert await UnconfiguredDirectory().check_connection(ENTRA_TENANT_ID) == "not-configured"


async def test_only_the_end_dates_of_the_secrets_are_kept(
    router: respx.MockRouter, tmp_path: Path
) -> None:
    _token(router)
    call = router.get(f"{GRAPH}/applications(appId='{APP_ID}')").respond(
        json={
            "id": "obj",
            "passwordCredentials": [
                {
                    "keyId": "key-1",
                    "displayName": "prod secret",
                    "hint": "abc",
                    "endDateTime": "2027-01-01T00:00:00Z",
                },
                {"keyId": "key-2", "hint": "def", "endDateTime": "2026-12-31T10:30:00+00:00"},
                {"hint": "ghi", "endDateTime": "garbage"},
                {"hint": "naive", "endDateTime": "2027-06-01T00:00:00"},
                {"hint": "jkl"},
                "not-a-dict",
            ],
        }
    )
    ends = await _graph(tmp_path).application_secret_expiries(ENTRA_TENANT_ID, APP_ID)
    assert ends == [
        datetime(2027, 1, 1, tzinfo=UTC),
        datetime(2026, 12, 31, 10, 30, tzinfo=UTC),
    ]
    assert call.calls.last.request.url.params["$select"] == "passwordCredentials"
    assert all(isinstance(e, datetime) for e in ends)


@pytest.mark.parametrize("status", [403, 404, 429, 500])
async def test_a_refusal_or_not_found_makes_the_expiry_unknown(
    router: respx.MockRouter, tmp_path: Path, status: int
) -> None:
    _token(router)
    router.get(f"{GRAPH}/applications(appId='{APP_ID}')").respond(status, json={})
    assert await _graph(tmp_path).application_secret_expiries(ENTRA_TENANT_ID, APP_ID) is None


async def test_the_expiry_is_unknown_for_a_bad_app_id_tenant_or_body(
    router: respx.MockRouter, tmp_path: Path
) -> None:
    _token(router)
    graph = _graph(tmp_path)
    assert await graph.application_secret_expiries(ENTRA_TENANT_ID, "bad'id") is None
    assert await graph.application_secret_expiries(OTHER, APP_ID) is None
    router.get(f"{GRAPH}/applications(appId='{APP_ID}')").respond(json={"id": "x"})
    assert await graph.application_secret_expiries(ENTRA_TENANT_ID, APP_ID) is None
    assert (
        await UnconfiguredDirectory().application_secret_expiries(ENTRA_TENANT_ID, APP_ID) is None
    )


async def test_an_empty_secret_file_or_a_client_id_that_is_not_an_app_id_is_not_configured(
    router: respx.MockRouter, tmp_path: Path
) -> None:
    token = router.post(TOKEN_URL).respond(json={"access_token": "tok", "expires_in": 3600})
    assert await _graph(tmp_path, secret="").check_connection(ENTRA_TENANT_ID) == "not-configured"
    assert (
        await _graph(tmp_path, client_id="not/an app id").check_connection(ENTRA_TENANT_ID)
        == "not-configured"
    )
    assert token.call_count == 0


async def test_an_unusable_token_answer_is_reported_as_token_refused(
    router: respx.MockRouter, tmp_path: Path
) -> None:
    router.post(TOKEN_URL).respond(json={"expires_in": 3600})
    assert await _graph(tmp_path).check_connection(ENTRA_TENANT_ID) == "token-refused"
