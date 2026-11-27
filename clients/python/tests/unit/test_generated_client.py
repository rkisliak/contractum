"""The generated client, driven through contractum._api against a mocked server (respx)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import respx
from support.payloads import load_payload

from contractum import _api
from contractum._generated.client import AuthenticatedClient, Client
from contractum._generated.models.contract_diff_out import ContractDiffOut
from contractum._generated.models.contract_list_out import ContractListOut
from contractum._generated.models.contract_metadata_out import ContractMetadataOut
from contractum._generated.models.contract_validation_report_out import (
    ContractValidationReportOut,
)
from contractum._generated.models.contract_version_list_out import ContractVersionListOut
from contractum._generated.models.contract_version_metadata_out import ContractVersionMetadataOut
from contractum._generated.models.identity_auth_policy_out import IdentityAuthPolicyOut
from contractum._generated.models.identity_folder_list_out import IdentityFolderListOut
from contractum._generated.models.identity_me_out import IdentityMeOut
from contractum.errors import (
    AccessDeniedError,
    AuthError,
    ContractumError,
    NotFoundError,
    RateLimitedError,
    ServerError,
)

BASE = "https://contractum.example"
CONTRACTS = "/api/v1/contracts/contracts"
CID = uuid.UUID("3f2c1b9e-6a4d-4c58-9d0e-2b7a1c5e8f10")
PROBLEM = "application/problem+json"


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE) as client:
        yield client


def problem_response(
    status: int, slug: str, *, title: str = "Problem", detail: str | None = None, **headers: str
) -> httpx.Response:
    body: dict[str, Any] = {"type": f"urn:dc:problem:{slug}", "title": title, "status": status}
    if detail is not None:
        body["detail"] = detail
    return httpx.Response(status, json=body, headers={"content-type": PROBLEM, **headers})


def test_tracer_get_me_returns_typed_model_with_one_get(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    route = router.get("/api/v1/identity/me").mock(
        return_value=httpx.Response(200, json=load_payload("me"))
    )
    me = _api.get_me(http)
    assert route.call_count == 1
    assert router.calls.call_count == 1
    assert router.calls.last.request.method == "GET"
    assert isinstance(me, IdentityMeOut)
    assert me.username == "dana"
    assert me.groups[0].display_name == "Data engineers"


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("me", IdentityMeOut),
        ("contract_list", ContractListOut),
        ("contract", ContractMetadataOut),
        ("versions", ContractVersionListOut),
        ("version_metadata", ContractVersionMetadataOut),
        ("validation_report", ContractValidationReportOut),
        ("diff", ContractDiffOut),
        ("folders", IdentityFolderListOut),
        ("auth_policy", IdentityAuthPolicyOut),
    ],
)
def test_every_payload_fixture_deserialises_into_its_generated_model(
    name: str, model: type[Any]
) -> None:
    parsed = model.from_dict(load_payload(name))
    assert parsed.to_dict() is not None


def test_timestamps_with_z_and_nine_fraction_digits_parse_as_aware_utc() -> None:
    metadata = ContractVersionMetadataOut.from_dict(load_payload("version_metadata"))
    assert metadata.published_at is not None
    assert metadata.published_at.microsecond == 123456
    assert metadata.published_at.utcoffset() is not None
    assert metadata.deprecated_at is not None
    assert metadata.deprecated_at.microsecond == 123456
    assert metadata.sunset_date is not None
    assert metadata.sunset_date.isoformat() == "2027-03-01"
    contract = ContractMetadataOut.from_dict(load_payload("contract"))
    assert contract.updated_at.microsecond == 500000


def test_get_contract_and_list_contracts_send_the_documented_requests(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    listing = router.get(CONTRACTS).mock(
        return_value=httpx.Response(200, json=load_payload("contract_list"))
    )
    single = router.get(f"{CONTRACTS}/{CID}").mock(
        return_value=httpx.Response(200, json=load_payload("contract"))
    )
    folder = uuid.UUID("11111111-2222-4333-8444-555555555555")
    page = _api.list_contracts(
        http,
        folder_id=folder,
        include_subtree=True,
        odcs_id="orders-v1",
        name="Orders",
        domain="sales",
        status="active",
        tags=["pii", "core"],
        q="ord",
        limit=10,
        cursor="c1",
    )
    params = listing.calls.last.request.url.params
    assert params["folder_id"] == str(folder)
    assert params["include_subtree"] == "true"
    assert params["odcs_id"] == "orders-v1"
    assert params["limit"] == "10"
    assert params["cursor"] == "c1"
    assert params.get_list("tags") == ["pii", "core"]
    assert page.next_cursor == "opaque-cursor"
    assert page.items[0].odcs_id == "orders-v1"
    _api.list_contracts(http)
    assert dict(listing.calls.last.request.url.params) == {
        "include_subtree": "false",
        "limit": "50",
        "sort": "id",
    }
    contract = _api.get_contract(http, CID)
    assert single.call_count == 1
    assert contract.id == CID


def test_versions_metadata_report_and_diff(router: respx.MockRouter, http: httpx.Client) -> None:
    versions = router.get(f"{CONTRACTS}/{CID}/versions").mock(
        return_value=httpx.Response(200, json=load_payload("versions"))
    )
    metadata = router.get(f"{CONTRACTS}/{CID}/versions/1.2.0/metadata").mock(
        return_value=httpx.Response(200, json=load_payload("version_metadata"))
    )
    report = router.get(f"{CONTRACTS}/{CID}/versions/latest/validation-report").mock(
        return_value=httpx.Response(200, json=load_payload("validation_report"))
    )
    diff = router.get(f"{CONTRACTS}/{CID}/diff").mock(
        return_value=httpx.Response(200, json=load_payload("diff"))
    )
    assert _api.list_versions(http, CID).items[0].semver == "1.2.0"
    assert _api.get_version_metadata(http, CID, "1.2.0").etag == '"c0ffee"'
    assert _api.get_validation_report(http, CID, "latest").engine_version == "1.0.0"
    result = _api.get_diff(http, CID, "1.1.0", "1.2.0", limit=5, cursor="n1")
    assert result.required_bump == "minor"
    params = diff.calls.last.request.url.params
    assert (params["from"], params["to"], params["limit"], params["cursor"]) == (
        "1.1.0",
        "1.2.0",
        "5",
        "n1",
    )
    assert all(r.call_count == 1 for r in (versions, metadata, report, diff))


def test_identity_reads(router: respx.MockRouter, http: httpx.Client) -> None:
    folders = router.get("/api/v1/identity/folders").mock(
        return_value=httpx.Response(200, json=load_payload("folders"))
    )
    policy = router.get("/api/v1/identity/auth/policy").mock(
        return_value=httpx.Response(200, json=load_payload("auth_policy"))
    )
    found = _api.find_folder(http, "acme/payments/ingest")
    assert folders.calls.last.request.url.params["path"] == "acme/payments/ingest"
    assert found.items[0].slug == "ingest"
    assert _api.get_auth_policy(http).password_rules.min_length == 12
    assert policy.call_count == 1


def test_the_api_layer_offers_no_sign_in_or_session_call() -> None:
    for name in ("login", "refresh", "logout"):
        assert not hasattr(_api, name)


def test_a_generated_client_wrapping_an_httpx_client_works_too(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    router.get("/api/v1/identity/me").mock(
        return_value=httpx.Response(200, json=load_payload("me"))
    )
    authenticated = AuthenticatedClient(base_url=BASE, token="unused").set_httpx_client(http)
    plain = Client(base_url=BASE).set_httpx_client(http)
    assert _api.get_me(authenticated).username == "dana"
    assert _api.get_me(plain).username == "dana"


def test_a_404_problem_fixture_becomes_not_found(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    router.get(f"{CONTRACTS}/{CID}").mock(
        return_value=httpx.Response(
            404, json=load_payload("problem_404"), headers={"content-type": PROBLEM}
        )
    )
    with pytest.raises(NotFoundError, match=r"Not found \(HTTP 404, urn:dc:problem:not-found\)"):
        _api.get_contract(http, CID)


@pytest.mark.parametrize(
    ("status", "slug", "expected", "retry_after", "hint_part"),
    [
        (401, "unauthenticated", AuthError, None, "contractum login"),
        (401, "invalid-credentials", AuthError, None, "contractum login"),
        (403, "password-rotation-required", AuthError, None, "rotate the password"),
        (403, "principal-not-registered", AuthError, None, "administrator"),
        (403, "forbidden", AccessDeniedError, None, None),
        (404, "not-found", NotFoundError, None, None),
        (406, "not-acceptable", ServerError, None, None),
        (422, "validation-error", ServerError, None, None),
        (429, "too-many-attempts", RateLimitedError, 7.0, None),
        (500, "internal", ServerError, None, None),
        (503, "service-unavailable", ServerError, None, None),
    ],
)
def test_raise_for_problem_maps_status_and_slug_to_the_error_table(
    status: int,
    slug: str,
    expected: type[ContractumError],
    retry_after: float | None,
    hint_part: str | None,
) -> None:
    headers = {"retry-after": "7"} if retry_after is not None else {}
    response = problem_response(status, slug, title="Title here", detail="Detail here", **headers)
    with pytest.raises(expected) as caught:
        _api.raise_for_problem(response)
    error = caught.value
    assert type(error) is expected
    assert f"Title here (HTTP {status}, urn:dc:problem:{slug}): Detail here" in str(error)
    if hint_part is not None:
        assert error.hint is not None
        assert hint_part in error.hint
    if isinstance(error, RateLimitedError):
        assert error.retry_after == retry_after
    if isinstance(error, ServerError):
        assert (error.status, error.title, error.detail) == (status, "Title here", "Detail here")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="SECRET-BODY traceback", headers={"content-type": "text/html"}),
        httpx.Response(500, json={"title": "SECRET-BODY"}),
        httpx.Response(500, content=b"SECRET-BODY"),
        httpx.Response(500, content=b"{broken SECRET-BODY", headers={"content-type": PROBLEM}),
        httpx.Response(500, json=["SECRET-BODY"], headers={"content-type": PROBLEM}),
    ],
)
def test_a_non_problem_error_body_is_never_echoed(response: httpx.Response) -> None:
    with pytest.raises(ServerError) as caught:
        _api.raise_for_problem(response)
    assert "SECRET-BODY" not in str(caught.value)
    assert str(caught.value) == "server error (HTTP 500)"


def test_problem_fields_are_sanitised_capped_and_typed() -> None:
    nasty = "bad\x1b[31m red\x07\n" + "x" * 1000
    response = httpx.Response(
        502,
        json={
            "type": "https://evil.example/\x1b[2J",
            "title": nasty,
            "detail": 42,
            "status": 502,
        },
        headers={"content-type": "application/problem+json; charset=utf-8"},
    )
    with pytest.raises(ServerError) as caught:
        _api.raise_for_problem(response)
    error = caught.value
    assert error.problem_type is None
    assert error.detail is None
    assert error.title is not None
    assert "\x1b" not in error.title
    assert "\x07" not in error.title
    assert len(error.title) == _api.MAX_FIELD_CHARS
    assert "\x1b" not in str(error)


def test_blank_title_and_oversized_slug_fall_back_to_generic_text() -> None:
    long_slug = "a" * 80
    response = problem_response(500, long_slug, title="   ")
    with pytest.raises(ServerError) as caught:
        _api.raise_for_problem(response)
    assert str(caught.value) == "server error (HTTP 500)"


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("3", 3.0), ("0", 0.0), ("2.5", 2.5), ("soon", None), ("-1", None), ("999999", None)],
)
def test_retry_after_is_parsed_defensively(retry_after: str, expected: float | None) -> None:
    with pytest.raises(RateLimitedError) as caught:
        _api.raise_for_problem(
            problem_response(429, "too-many-attempts", **{"retry-after": retry_after})
        )
    assert caught.value.retry_after == expected


def test_rate_limit_without_retry_after_header() -> None:
    with pytest.raises(RateLimitedError) as caught:
        _api.raise_for_problem(problem_response(429, "too-many-attempts"))
    assert caught.value.retry_after is None


def test_raise_for_problem_never_returns_for_a_success_status() -> None:
    with pytest.raises(ServerError):
        _api.raise_for_problem(httpx.Response(200, json={}))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(202, json={}),
        httpx.Response(200, json=load_payload("token")),
    ],
)
def test_unreadable_or_unexpected_success_answers_become_server_errors(
    router: respx.MockRouter, http: httpx.Client, response: httpx.Response
) -> None:
    router.get("/api/v1/identity/me").mock(return_value=response)
    with pytest.raises(ServerError):
        _api.get_me(http)


def test_a_non_json_gateway_error_still_maps_by_status(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    router.get("/api/v1/identity/me").mock(
        return_value=httpx.Response(
            401, text="<html>nope</html>", headers={"content-type": "text/html"}
        )
    )
    with pytest.raises(AuthError) as caught:
        _api.get_me(http)
    assert "<html>" not in str(caught.value)


def test_allowed_operation_modules_are_ten_sorted_dotted_names() -> None:
    assert len(_api.ALLOWED_OPERATION_MODULES) == 10
    assert list(_api.ALLOWED_OPERATION_MODULES) == sorted(_api.ALLOWED_OPERATION_MODULES)
    assert all(n.startswith("contractum._generated.api.") for n in _api.ALLOWED_OPERATION_MODULES)
    assert _api.MAX_DOCUMENT_BYTES == 8 * 1024 * 1024


DOCUMENT = b"id: orders\nversion: 1.2.0\n"
VERSION_URL = f"{CONTRACTS}/{CID}/versions/1.2.0"


def document_response(**headers: str) -> httpx.Response:
    base = {"content-type": "application/yaml; charset=utf-8", "etag": '"c0ffee"'}
    return httpx.Response(200, content=DOCUMENT, headers={**base, **headers})


def test_fetch_version_returns_bytes_etag_media_type_and_raw_deprecation_headers(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    route = router.get(VERSION_URL).mock(
        return_value=document_response(
            deprecation="@1795000000", sunset="Mon, 01 Mar 2027 23:59:59 GMT"
        )
    )
    fetched = _api.fetch_version(http, CID, "1.2.0")
    assert fetched == _api.FetchedVersion(
        status=200,
        content=DOCUMENT,
        etag='"c0ffee"',
        media_type="application/yaml",
        deprecation="@1795000000",
        sunset="Mon, 01 Mar 2027 23:59:59 GMT",
    )
    assert "if-none-match" not in route.calls.last.request.headers


def test_fetch_version_without_optional_headers(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    router.get(VERSION_URL).mock(return_value=httpx.Response(200, content=DOCUMENT))
    fetched = _api.fetch_version(http, CID, "1.2.0")
    assert (fetched.etag, fetched.media_type, fetched.deprecation, fetched.sunset) == (
        None,
        None,
        None,
        None,
    )


def test_conditional_fetch_sends_if_none_match_on_that_request_only(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    route = router.get(VERSION_URL).mock(
        side_effect=[
            httpx.Response(304, headers={"etag": '"c0ffee"'}),
            document_response(),
        ]
    )
    not_modified = _api.fetch_version(http, CID, "1.2.0", if_none_match='"c0ffee"')
    assert route.calls.last.request.headers["if-none-match"] == '"c0ffee"'
    assert "if-none-match" not in http.headers
    assert (not_modified.status, not_modified.content, not_modified.etag) == (304, None, '"c0ffee"')
    again = _api.fetch_version(http, CID, "1.2.0")
    assert "if-none-match" not in route.calls.last.request.headers
    assert "if-none-match" not in http.headers
    assert again.content == DOCUMENT


def test_fetch_version_refuses_a_declared_oversized_document(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    router.get(VERSION_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"x",
            headers={"content-length": str(_api.MAX_DOCUMENT_BYTES + 1)},
        )
    )
    with pytest.raises(ServerError, match="larger than 8 MiB"):
        _api.fetch_version(http, CID, "1.2.0")


def test_fetch_version_refuses_a_streamed_oversized_document(
    router: respx.MockRouter, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_api, "MAX_DOCUMENT_BYTES", 1024 * 1024)
    chunk = b"y" * 65536

    def stream() -> Iterator[bytes]:
        for _ in range(40):
            yield chunk

    router.get(VERSION_URL).mock(return_value=httpx.Response(200, content=stream()))
    with pytest.raises(ServerError, match="larger than 1 MiB"):
        _api.fetch_version(http, CID, "1.2.0")


def test_fetch_version_accepts_a_document_of_exactly_the_cap(
    router: respx.MockRouter, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_api, "MAX_DOCUMENT_BYTES", len(DOCUMENT))
    router.get(VERSION_URL).mock(return_value=httpx.Response(200, content=DOCUMENT))
    assert _api.fetch_version(http, CID, "1.2.0").content == DOCUMENT


@pytest.mark.parametrize(
    ("make_response", "expected"),
    [
        (lambda: problem_response(404, "not-found", title="Not found"), NotFoundError),
        (lambda: problem_response(401, "unauthenticated"), AuthError),
        (lambda: httpx.Response(500, text="SECRET-BODY"), ServerError),
        (lambda: httpx.Response(204), ServerError),
        (lambda: httpx.Response(200 + 99, text="odd"), ServerError),
    ],
)
def test_fetch_version_maps_other_statuses(
    router: respx.MockRouter,
    http: httpx.Client,
    make_response: Callable[[], httpx.Response],
    expected: type[ContractumError],
) -> None:
    router.get(VERSION_URL).mock(return_value=make_response())
    with pytest.raises(expected) as caught:
        _api.fetch_version(http, CID, "1.2.0")
    assert "SECRET-BODY" not in str(caught.value)


def test_fetch_version_ignores_an_oversized_error_body(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    router.get(VERSION_URL).mock(
        return_value=httpx.Response(
            500,
            content=b"z" * (_api.MAX_PROBLEM_BYTES + 1),
            headers={"content-type": PROBLEM},
        )
    )
    with pytest.raises(ServerError) as caught:
        _api.fetch_version(http, CID, "1.2.0")
    assert str(caught.value) == "server error (HTTP 500)"


def test_fetch_version_accepts_a_generated_client(
    router: respx.MockRouter, http: httpx.Client
) -> None:
    router.get(VERSION_URL).mock(return_value=document_response())
    generated = Client(base_url=BASE).set_httpx_client(http)
    assert _api.fetch_version(generated, CID, "1.2.0").content == DOCUMENT
