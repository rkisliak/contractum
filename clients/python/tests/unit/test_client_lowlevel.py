"""The typed low-level Client methods, token providers and bearer refresh, against respx."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import SecretStr
from support.payloads import load_payload

from contractum.auth import resolve_token_provider
from contractum.auth.tokens import BearerAuth, StaticToken, TokenProvider
from contractum.client import Client
from contractum.config import Settings
from contractum.errors import (
    AuthError,
    ConfigError,
    NotFoundError,
    ServerError,
    UsageError,
)
from contractum.models import (
    AuthPolicy,
    ContractDiff,
    ContractPage,
    ContractSummary,
    FetchResult,
    Folder,
    StoredValidationReport,
    VersionInfo,
)

BASE = "https://contractum.example"
CONTRACTS = "/api/v1/contracts/contracts"
CID = "3f2c1b9e-6a4d-4c58-9d0e-2b7a1c5e8f10"
DOCUMENT = b"apiVersion: v3.0.2\nkind: DataContract\nid: orders-v1\n"
PROBLEM = "application/problem+json"


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def client(canary_token: str) -> Iterator[Client]:
    with Client(BASE, token=canary_token, sleep=lambda seconds: None) as built:
        yield built


def _last(route: respx.Route) -> httpx.Request:
    return route.calls.last.request


# --- constructor and factories --------------------------------------------------------------


def test_exactly_one_credential_is_required(canary_token: str) -> None:
    with pytest.raises(ConfigError):
        Client(BASE)
    with pytest.raises(ConfigError):
        Client(BASE, token=canary_token, token_provider=StaticToken(canary_token))


def test_an_empty_or_unsendable_token_is_refused() -> None:
    for bad in ("", "   ", "two words", "line\nbreak", "café"):
        with pytest.raises(ConfigError):
            StaticToken(bad)
    with pytest.raises(ConfigError):
        StaticToken("x" * 20_000)


def test_a_token_is_stripped_of_surrounding_whitespace(canary_token: str) -> None:
    assert StaticToken(f" {canary_token}\n").get_token() == canary_token


def test_from_settings_uses_the_env_token_and_the_settings(
    router: respx.MockRouter, canary_token: str
) -> None:
    route = router.get("/api/v1/identity/me").respond(200, json=load_payload("me"))
    settings = Settings(profile="default", url=BASE, env_token=SecretStr(canary_token))
    with Client.from_settings(settings) as built:
        built.whoami()
    assert _last(route).headers["authorization"] == f"Bearer {canary_token}"


def test_from_settings_without_credentials_hints_at_login() -> None:
    with pytest.raises(AuthError) as raised:
        Client.from_settings(Settings(profile="default", url=BASE))
    assert raised.value.hint is not None
    assert "contractum login" in raised.value.hint


def test_from_env_reads_the_environment(router: respx.MockRouter, canary_token: str) -> None:
    route = router.get("/api/v1/identity/me").respond(200, json=load_payload("me"))
    environ = {"CONTRACTUM_URL": BASE, "CONTRACTUM_TOKEN": canary_token}
    with Client.from_env(env=environ) as built:
        built.whoami()
    assert route.call_count == 1


def test_from_env_flags_beat_the_environment(router: respx.MockRouter, canary_token: str) -> None:
    route = router.get("/api/v1/identity/me").respond(200, json=load_payload("me"))
    environ = {"CONTRACTUM_URL": "https://wrong.example", "CONTRACTUM_TOKEN": canary_token}
    with Client.from_env(url=BASE, env=environ) as built:
        built.whoami()
    assert route.call_count == 1


# --- listing and metadata -------------------------------------------------------------------


def test_list_contracts_passes_every_filter_as_a_query_parameter(
    client: Client, router: respx.MockRouter
) -> None:
    route = router.get(CONTRACTS).respond(200, json=load_payload("contract_list"))
    page = client.list_contracts(
        folder_id="11111111-2222-4333-8444-555555555555",
        include_subtree=True,
        odcs_id="orders-v1",
        name="Orders",
        domain="sales",
        status="active",
        tags=["pii", "core"],
        q="ord",
        limit=25,
        cursor="abc",
    )
    params = _last(route).url.params
    assert params["folder_id"] == "11111111-2222-4333-8444-555555555555"
    assert params["include_subtree"] == "true"
    assert params["odcs_id"] == "orders-v1"
    assert params["name"] == "Orders"
    assert params["domain"] == "sales"
    assert params["status"] == "active"
    assert params.get_list("tags") == ["pii", "core"]
    assert params["q"] == "ord"
    assert params["limit"] == "25"
    assert params["cursor"] == "abc"
    assert isinstance(page, ContractPage)
    assert isinstance(page.items[0], ContractSummary)
    assert page.next_cursor == "opaque-cursor"
    assert page.items[0].latest_version is not None
    assert page.items[0].latest_version.semver == "1.2.0"


@pytest.mark.parametrize(
    ("asked", "sent"), [(0, "1"), (-5, "1"), (1, "1"), (100, "100"), (900, "100")]
)
def test_list_limit_is_clamped_to_1_through_100(
    client: Client, router: respx.MockRouter, asked: int, sent: str
) -> None:
    route = router.get(CONTRACTS).respond(200, json=load_payload("contract_list"))
    client.list_contracts(limit=asked)
    assert _last(route).url.params["limit"] == sent


def test_a_folder_id_that_is_not_a_uuid_is_a_usage_error(client: Client) -> None:
    with pytest.raises(UsageError):
        client.list_contracts(folder_id="acme/payments")


def test_iter_contracts_follows_next_cursor(client: Client, router: respx.MockRouter) -> None:
    first = load_payload("contract_list")
    second = copy.deepcopy(first)
    second["items"][0]["name"] = "Second"
    second["next_cursor"] = None
    route = router.get(CONTRACTS).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    names = [item.name for item in client.iter_contracts(domain="sales", page_size=1)]
    assert names == ["Orders", "Second"]
    assert route.call_count == 2
    assert _last(route).url.params["cursor"] == "opaque-cursor"
    assert _last(route).url.params["domain"] == "sales"


def test_iter_contracts_stops_when_the_server_repeats_a_cursor(
    client: Client, router: respx.MockRouter
) -> None:
    router.get(CONTRACTS).respond(200, json=load_payload("contract_list"))
    with pytest.raises(ServerError, match="repeated"):
        list(client.iter_contracts())


def test_get_contract_accepts_a_uuid_object_or_string(
    client: Client, router: respx.MockRouter
) -> None:
    router.get(f"{CONTRACTS}/{CID}").respond(200, json=load_payload("contract"))
    assert client.get_contract(CID).odcs_id == "orders-v1"
    assert client.get_contract(UUID(CID)).id == UUID(CID)
    with pytest.raises(UsageError):
        client.get_contract("orders-v1")


def test_get_contract_maps_404_to_not_found(client: Client, router: respx.MockRouter) -> None:
    router.get(f"{CONTRACTS}/{CID}").respond(
        404, json=load_payload("problem_404"), headers={"content-type": PROBLEM}
    )
    with pytest.raises(NotFoundError):
        client.get_contract(CID)


def test_list_versions_keeps_the_server_order(client: Client, router: respx.MockRouter) -> None:
    payload = load_payload("versions")
    older = copy.deepcopy(payload["items"][0])
    older["semver"] = "1.0.0"
    older["status"] = "older"
    newer = copy.deepcopy(payload["items"][0])
    newer["semver"] = "2.0.0"
    newer["status"] = "preview"
    router.get(f"{CONTRACTS}/{CID}/versions").respond(200, json={"items": [newer, older]})
    versions = client.list_versions(CID)
    assert [v.semver for v in versions] == ["2.0.0", "1.0.0"]
    assert all(isinstance(v, VersionInfo) for v in versions)


def test_version_metadata_carries_the_lifecycle_fields(
    client: Client, router: respx.MockRouter
) -> None:
    route = router.get(f"{CONTRACTS}/{CID}/versions/1.2.0/metadata").respond(
        200, json=load_payload("version_metadata")
    )
    info = client.version_metadata(CID, "1.2.0")
    assert _last(route).method == "GET"
    assert info.content_sha256 == "c0ffee" * 10 + "c0ff"
    assert info.etag == '"c0ffee"'
    assert info.deprecated is True
    assert info.deprecated_at == datetime(2026, 11, 20, 8, 0, 0, 123456, tzinfo=UTC)
    assert info.sunset_date == date(2027, 3, 1)
    assert info.status == "latest"


@pytest.mark.parametrize("ref", ["latest", "1.2.0", "2.0.0-rc.1", "1.0.0+build.5"])
def test_valid_refs_are_accepted(client: Client, router: respx.MockRouter, ref: str) -> None:
    router.get(url__regex=rf"{CONTRACTS}/{CID}/versions/.*/metadata").respond(
        200, json=load_payload("version_metadata")
    )
    assert client.version_metadata(CID, ref).semver == "1.2.0"


@pytest.mark.parametrize("ref", ["preview", "1.2", "../x", "1.2.0/metadata", "", "v1.2.0"])
def test_invalid_refs_are_usage_errors(client: Client, ref: str) -> None:
    with pytest.raises(UsageError):
        client.version_metadata(CID, ref)


# --- fetch ----------------------------------------------------------------------------------


def test_fetch_document_returns_bytes_and_parsed_lifecycle_headers(
    client: Client, router: respx.MockRouter
) -> None:
    route = router.get(f"{CONTRACTS}/{CID}/versions/1.2.0").respond(
        200,
        content=DOCUMENT,
        headers={
            "content-type": "application/yaml; charset=utf-8",
            "etag": '"c0ffee"',
            "deprecation": "@1767225600",
            "sunset": "Mon, 01 Mar 2027 23:59:59 GMT",
        },
    )
    result = client.fetch_document(CID, "1.2.0")
    assert isinstance(result, FetchResult)
    assert result.content == DOCUMENT
    assert result.status == 200
    assert result.not_modified is False
    assert result.etag == '"c0ffee"'
    assert result.media_type == "application/yaml"
    assert result.deprecation == datetime(2026, 1, 1, tzinfo=UTC)
    assert result.sunset == datetime(2027, 3, 1, 23, 59, 59, tzinfo=UTC)
    assert "if-none-match" not in _last(route).headers


def test_fetch_document_revalidates_with_if_none_match(
    client: Client, router: respx.MockRouter
) -> None:
    route = router.get(f"{CONTRACTS}/{CID}/versions/latest").respond(
        304, headers={"etag": '"c0ffee"'}
    )
    result = client.fetch_document(CID, if_none_match='"c0ffee"')
    assert _last(route).headers["if-none-match"] == '"c0ffee"'
    assert result.not_modified is True
    assert result.content is None
    assert result.deprecation is None
    assert result.sunset is None


def test_fetch_document_ignores_malformed_lifecycle_headers(
    client: Client, router: respx.MockRouter
) -> None:
    router.get(f"{CONTRACTS}/{CID}/versions/latest").respond(
        200, content=DOCUMENT, headers={"deprecation": "true", "sunset": "someday"}
    )
    result = client.fetch_document(CID)
    assert result.deprecation is None
    assert result.sunset is None


def test_validation_report_and_diff_are_typed(client: Client, router: respx.MockRouter) -> None:
    router.get(f"{CONTRACTS}/{CID}/versions/1.2.0/validation-report").respond(
        200, json=load_payload("validation_report")
    )
    router.get(f"{CONTRACTS}/{CID}/diff").respond(200, json=load_payload("diff"))
    report = client.validation_report(CID, "1.2.0")
    diff = client.diff(CID, "1.1.0", "1.2.0", limit=9000)
    assert isinstance(report, StoredValidationReport)
    assert report.report["outcome"] == "passed"
    assert isinstance(diff, ContractDiff)
    assert diff.from_.ref == "1.1.0"
    assert diff.to.semver == "1.2.0"
    assert diff.required_bump == "minor"
    assert diff.counts == {"info": 1}


def test_diff_sends_its_refs_and_a_capped_limit(client: Client, router: respx.MockRouter) -> None:
    route = router.get(f"{CONTRACTS}/{CID}/diff").respond(200, json=load_payload("diff"))
    client.diff(CID, "1.1.0", "latest", limit=9000, cursor="c")
    params = _last(route).url.params
    assert params["from"] == "1.1.0"
    assert params["to"] == "latest"
    assert params["limit"] == "500"
    assert params["cursor"] == "c"


# --- folders and policy ---------------------------------------------------------------------


def test_find_folder_returns_the_matching_folder(client: Client, router: respx.MockRouter) -> None:
    route = router.get("/api/v1/identity/folders").respond(200, json=load_payload("folders"))
    folder = client.find_folder("/acme/payments/ingest/")
    assert _last(route).url.params["path"] == "acme/payments/ingest"
    assert isinstance(folder, Folder)
    assert folder.kind == "team"
    assert folder.parent_id is None


def test_find_folder_returns_none_when_nothing_matches(
    client: Client, router: respx.MockRouter
) -> None:
    router.get("/api/v1/identity/folders").respond(200, json={"items": [], "next_cursor": None})
    assert client.find_folder("acme/none") is None
    router.get("/api/v1/identity/folders").respond(200, json=load_payload("folders"))
    assert client.find_folder("acme/other") is None


def test_find_folder_treats_a_404_as_missing(client: Client, router: respx.MockRouter) -> None:
    router.get("/api/v1/identity/folders").respond(
        404, json=load_payload("problem_404"), headers={"content-type": PROBLEM}
    )
    assert client.find_folder("acme/none") is None


def test_find_folder_rejects_an_empty_path(client: Client) -> None:
    with pytest.raises(UsageError):
        client.find_folder(" / ")


def test_auth_policy_is_sent_without_a_token_and_reuses_one_client(
    client: Client, router: respx.MockRouter
) -> None:
    route = router.get("/api/v1/identity/auth/policy").respond(
        200, json=load_payload("auth_policy")
    )
    policy = client.auth_policy()
    client.auth_policy()
    assert "authorization" not in _last(route).headers
    assert route.call_count == 2
    assert isinstance(policy, AuthPolicy)
    assert policy.methods.local is True
    assert policy.password_rules.min_length == 12
    client.close()
    client.close()


def test_auth_policy_uses_an_injected_client(router: respx.MockRouter, canary_token: str) -> None:
    router.get("/api/v1/identity/auth/policy").respond(200, json=load_payload("auth_policy"))
    with httpx.Client(base_url=BASE) as http:
        built = Client(BASE, token=canary_token, http_client=http)
        assert built.auth_policy().methods.microsoft is False


# --- bearer refresh -------------------------------------------------------------------------


class _Rotating:
    kind = "test"

    def __init__(self, replacement: str | None) -> None:
        self.replacement = replacement
        self.refreshes = 0

    def get_token(self) -> str:
        return "old-token"

    def refresh(self) -> str | None:
        self.refreshes += 1
        return self.replacement


def test_a_401_triggers_one_refresh_and_one_replay(router: respx.MockRouter) -> None:
    provider: TokenProvider = _Rotating("new-token")
    route = router.get("/api/v1/identity/me").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=load_payload("me"))]
    )
    with Client(BASE, token_provider=provider) as built:
        built.whoami()
    assert [call.request.headers["authorization"] for call in route.calls] == [
        "Bearer old-token",
        "Bearer new-token",
    ]


def test_a_second_401_after_the_replay_is_an_auth_error(router: respx.MockRouter) -> None:
    provider = _Rotating("new-token")
    route = router.get("/api/v1/identity/me").respond(401)
    with Client(BASE, token_provider=provider) as built, pytest.raises(AuthError):
        built.whoami()
    assert route.call_count == 2
    assert provider.refreshes == 1


def test_a_401_without_a_replacement_is_not_replayed(
    router: respx.MockRouter, canary_token: str
) -> None:
    route = router.get("/api/v1/identity/me").respond(401)
    with Client(BASE, token=canary_token) as built, pytest.raises(AuthError):
        built.whoami()
    assert route.call_count == 1


def test_bearer_auth_repr_names_the_kind_only(canary_token: str) -> None:
    auth = BearerAuth(StaticToken(canary_token, kind="env"))
    assert repr(auth) == "BearerAuth(kind='env')"


def test_the_static_token_never_refreshes(canary_token: str) -> None:
    assert StaticToken(canary_token).refresh() is None


# --- credentials ----------------------------------------------------------------------------


def test_the_env_token_is_a_static_provider_and_nothing_is_written(
    tmp_path: Path, canary_token: str
) -> None:
    settings = Settings(profile="default", url=BASE, env_token=SecretStr(canary_token))
    provider = resolve_token_provider(settings)
    assert isinstance(provider, StaticToken)
    assert provider.kind == "env"
    assert provider.get_token() == canary_token
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "cache").exists()


def test_without_an_env_token_the_error_hints_at_login_or_the_variable() -> None:
    with pytest.raises(AuthError) as raised:
        resolve_token_provider(Settings(profile="default", url=BASE))
    assert raised.value.hint is not None
    assert "contractum login" in raised.value.hint
    assert "CONTRACTUM_TOKEN" in raised.value.hint


def test_the_client_has_no_write_method() -> None:
    public = {name for name in dir(Client) if not name.startswith("_")}
    forbidden = {"create", "update", "delete", "publish", "post", "put", "patch", "upload"}
    assert not {name for name in public if name.split("_")[0] in forbidden}


def test_every_server_call_goes_through_the_api_module(
    client: Client, router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    from contractum import _api

    original = _api.get_me

    def spy(transport: Any) -> Any:
        calls.append("get_me")
        return original(transport)

    monkeypatch.setattr(_api, "get_me", spy)
    router.get("/api/v1/identity/me").respond(200, json=load_payload("me"))
    client.whoami()
    assert calls == ["get_me"]
