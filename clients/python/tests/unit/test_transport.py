"""The write fence, retry, URL and TLS policy, redirect refusal and log redaction."""

from __future__ import annotations

import logging
import ssl
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import certifi
import httpx
import pytest
import respx
from support.payloads import load_payload

from contractum import _transport
from contractum._transport import (
    ConnectionSettings,
    RetryTransport,
    build_http_client,
    install_log_redaction,
    parse_deprecation,
    parse_sunset,
    redact,
    validate_base_url,
)
from contractum.client import Client
from contractum.errors import (
    ConfigError,
    NetworkError,
    RateLimitedError,
    ServerError,
    WriteNotAllowed,
)

BASE = "https://contractum.example"
ME = "/api/v1/identity/me"
AUTH_PATHS = [
    "/api/v1/identity/auth/login",
    "/api/v1/identity/auth/refresh",
    "/api/v1/identity/auth/logout",
]
"""Paths an earlier pre-release posted to; the SDK now sends no POST at all."""


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def http(sleeps: list[float]) -> Iterator[httpx.Client]:
    with build_http_client(ConnectionSettings(url=BASE), auth=None, sleep=sleeps.append) as client:
        yield client


@pytest.fixture
def client(sleeps: list[float], canary_token: str) -> Iterator[Client]:
    with Client(BASE, token=canary_token, sleep=sleeps.append) as built:
        yield built


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


# --- the write fence ----------------------------------------------------------------------


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", [ME, "/api/v1/contracts/contracts", AUTH_PATHS[0]])
def test_write_methods_are_refused_before_any_request(
    http: httpx.Client, router: respx.MockRouter, method: str, path: str
) -> None:
    router.route().respond(200)
    with pytest.raises(WriteNotAllowed, match="not a consumer operation"):
        http.request(method, path)
    assert router.calls.call_count == 0


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/contracts/contracts",
        "/api/v1/identity/pats",
        "/api/v1/identity/auth/other",
        *AUTH_PATHS,
    ],
)
def test_post_to_any_path_is_refused_including_the_former_auth_paths(
    http: httpx.Client, router: respx.MockRouter, path: str
) -> None:
    router.route().respond(200)
    with pytest.raises(WriteNotAllowed, match=f"POST {path} is not a consumer operation"):
        http.post(path, json={})
    assert router.calls.call_count == 0


def test_get_and_head_are_allowed(http: httpx.Client, router: respx.MockRouter) -> None:
    router.route().respond(200)
    assert http.get(ME).status_code == 200
    assert http.head(ME).status_code == 200


def test_the_fence_understands_a_base_url_path_prefix(
    router: respx.MockRouter, sleeps: list[float]
) -> None:
    settings = ConnectionSettings(url=f"{BASE}/gateway/")
    with build_http_client(settings, auth=None, sleep=sleeps.append) as http:
        router.get("/gateway/api/v1/identity/me").respond(200, json={})
        assert http.get(ME).status_code == 200
        with pytest.raises(WriteNotAllowed, match="POST /api/v1/contracts/contracts is not"):
            http.post("/api/v1/contracts/contracts", json={})


def test_the_transport_module_keeps_no_post_allow_list() -> None:
    assert not hasattr(_transport, "ALLOWED_POST")


# --- retry ----------------------------------------------------------------------------------


def test_503_with_retry_after_is_retried_once_with_that_wait(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    route = router.get(ME).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "2"}),
            httpx.Response(200, json=load_payload("me")),
        ]
    )
    assert client.whoami().username == "dana"
    assert route.call_count == 2
    assert sleeps == [2.0]


def test_four_503_answers_make_four_requests_then_a_server_error(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    route = router.get(ME).respond(503)
    with pytest.raises(ServerError) as raised:
        client.whoami()
    assert raised.value.status == 503
    assert route.call_count == 4
    assert len(sleeps) == 3


def test_backoff_doubles_with_a_small_jitter(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    router.get(ME).respond(502)
    with pytest.raises(ServerError):
        client.whoami()
    for attempt, waited in enumerate(sleeps):
        assert 0.5 * 2**attempt <= waited < 0.5 * 2**attempt + 0.25


def test_429_is_retried_and_ends_as_rate_limited(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    route = router.get(ME).respond(429, headers={"Retry-After": "1"})
    with pytest.raises(RateLimitedError):
        client.whoami()
    assert route.call_count == 4
    assert sleeps == [1.0, 1.0, 1.0]


def test_retry_after_is_capped_at_30_seconds(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    router.get(ME).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "86400"}),
            httpx.Response(200, json=load_payload("me")),
        ]
    )
    client.whoami()
    assert sleeps == [30.0]


def test_retry_after_may_be_an_http_date(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    router.get(ME).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": soon}),
            httpx.Response(200, json=load_payload("me")),
        ]
    )
    client.whoami()
    assert 10 < sleeps[0] <= 20


def test_a_retry_after_date_in_the_past_waits_zero(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    past = format_datetime(datetime.now(UTC) - timedelta(hours=1), usegmt=True)
    router.get(ME).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": past}),
            httpx.Response(200, json=load_payload("me")),
        ]
    )
    client.whoami()
    assert sleeps == [0.0]


def test_a_malformed_retry_after_falls_back_to_the_backoff(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    router.get(ME).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "soon"}),
            httpx.Response(200, json=load_payload("me")),
        ]
    )
    client.whoami()
    assert 0.5 <= sleeps[0] < 0.75


def _counting_transport(
    sleeps: list[float], answer: Callable[[httpx.Request], httpx.Response]
) -> tuple[RetryTransport, list[httpx.Request]]:
    """A RetryTransport around a mock; the guard is bypassed to test the retry policy alone."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    return RetryTransport(httpx.MockTransport(handler), sleep=sleeps.append), seen


def test_a_non_read_request_is_never_retried(sleeps: list[float]) -> None:
    transport, seen = _counting_transport(sleeps, lambda _: httpx.Response(503))
    response = transport.handle_request(httpx.Request("POST", f"{BASE}/x"))
    assert response.status_code == 503
    assert len(seen) == 1
    assert sleeps == []


def test_a_non_read_connect_error_is_not_retried(sleeps: list[float]) -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport, seen = _counting_transport(sleeps, refuse)
    with pytest.raises(NetworkError):
        transport.handle_request(httpx.Request("POST", f"{BASE}/x"))
    assert len(seen) == 1
    assert sleeps == []


def test_a_get_connect_error_is_retried(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    route = router.get(ME).mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.ConnectTimeout("slow"),
            httpx.Response(200, json=load_payload("me")),
        ]
    )
    assert client.whoami().username == "dana"
    assert route.call_count == 3
    assert len(sleeps) == 2


def test_a_persistent_connect_error_becomes_a_network_error_without_its_text(
    client: Client, router: respx.MockRouter
) -> None:
    route = router.get(ME).mock(side_effect=httpx.ConnectError("secret-host-detail"))
    with pytest.raises(NetworkError) as raised:
        client.whoami()
    assert route.call_count == 4
    assert "secret-host-detail" not in str(raised.value)
    assert "ConnectError" in str(raised.value)


def test_other_transport_errors_are_not_retried(
    client: Client, router: respx.MockRouter, sleeps: list[float]
) -> None:
    route = router.get(ME).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(NetworkError, match="ReadTimeout"):
        client.whoami()
    assert route.call_count == 1
    assert sleeps == []


def test_a_certificate_failure_gets_a_ca_bundle_hint(
    client: Client, router: respx.MockRouter
) -> None:
    router.get(ME).mock(
        side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get issuer")
    )
    with pytest.raises(NetworkError) as raised:
        client.whoami()
    assert raised.value.hint is not None
    assert "ca-bundle" in raised.value.hint


def test_close_closes_the_inner_transport() -> None:
    closed: list[bool] = []

    class Inner(httpx.BaseTransport):
        def close(self) -> None:
            closed.append(True)

    RetryTransport(Inner()).close()
    assert closed == [True]


# --- URL and TLS policy ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://10.0.0.5:8080",
        "ftp://example.com",
        "example.com",
        "https://",
        "",
        "https://user:pw@example.com",
        "https://user@example.com",
        "https://example.com?x=1",
        "https://example.com#frag",
        "https://example.com:notaport",
        "https://:8080",
    ],
)
def test_unsafe_or_malformed_urls_are_refused(url: str) -> None:
    with pytest.raises(ConfigError):
        validate_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost:8080",
        "http://127.0.0.1:1",
        "http://127.8.8.8",
        "http://[::1]:9000",
        "http://api.localhost",
    ],
)
def test_loopback_http_urls_pass_on_any_port(url: str) -> None:
    assert validate_base_url(url) == url


def test_http_off_loopback_needs_the_explicit_opt_in() -> None:
    assert validate_base_url("http://10.0.0.5:8080", allow_insecure_http=True) == (
        "http://10.0.0.5:8080"
    )


def test_https_urls_are_normalised_without_a_trailing_slash() -> None:
    assert (
        validate_base_url("  HTTPS://Contractum.example/gw/  ") == "https://Contractum.example/gw"
    )


def test_the_http_error_names_the_way_out() -> None:
    with pytest.raises(ConfigError) as raised:
        validate_base_url("http://example.com")
    assert raised.value.hint is not None
    assert "--allow-insecure-http" in raised.value.hint


def test_a_redirect_is_not_followed_and_is_reported(
    client: Client, router: respx.MockRouter
) -> None:
    router.get(ME).respond(302, headers={"Location": "https://other.example/api/v1/identity/me"})
    other = respx.mock(base_url="https://other.example", assert_all_called=False)
    with other as other_router:
        target = other_router.get(ME).respond(200, json=load_payload("me"))
        with pytest.raises(ServerError) as raised:
            client.whoami()
    assert not target.called
    assert raised.value.status == 302
    assert raised.value.hint is not None


def test_the_client_never_follows_redirects_and_verifies_tls() -> None:
    http = build_http_client(ConnectionSettings(url=BASE), auth=None)
    assert http.follow_redirects is False
    assert http.trust_env is True
    assert http.headers["user-agent"].startswith("contractum/")
    http.close()


def test_a_ca_bundle_gives_a_strict_context() -> None:
    context = _transport._verify(Path(certifi.where()))
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_without_a_ca_bundle_the_default_trust_store_is_used() -> None:
    assert _transport._verify(None) is True


def test_an_unreadable_ca_bundle_is_a_config_error(tmp_path: Path) -> None:
    broken = tmp_path / "bundle.pem"
    broken.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(ConfigError, match="CA bundle"):
        _transport._verify(broken)
    with pytest.raises(ConfigError, match="CA bundle"):
        _transport._verify(tmp_path / "missing.pem")


def test_the_environment_proxy_is_used_unless_the_host_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_transport(**kwargs: Any) -> httpx.BaseTransport:
        seen.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200))

    monkeypatch.setattr(_transport.httpx, "HTTPTransport", fake_transport)
    monkeypatch.setattr(
        _transport.urllib.request, "getproxies", lambda: {"https": "http://proxy.example:3128"}
    )
    monkeypatch.setattr(_transport.urllib.request, "proxy_bypass", lambda host: False)
    _transport._inner_transport(BASE, True)
    monkeypatch.setattr(_transport.urllib.request, "proxy_bypass", lambda host: True)
    _transport._inner_transport(BASE, True)
    monkeypatch.setattr(_transport.urllib.request, "getproxies", lambda: {})
    _transport._inner_transport(BASE, True)
    assert seen[0]["proxy"] == "http://proxy.example:3128"
    assert "proxy" not in seen[1]
    assert "proxy" not in seen[2]


# --- log redaction --------------------------------------------------------------------------


@pytest.fixture
def redaction(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install the redaction on a clean slate and restore the record factory afterwards."""
    original = logging.getLogRecordFactory()
    monkeypatch.setattr(_transport, "_redaction_installed", False)
    install_log_redaction()
    yield
    logging.setLogRecordFactory(original)


def test_log_records_of_the_three_namespaces_are_scrubbed(
    redaction: None,
    caplog: pytest.LogCaptureFixture,
    canary_token: str,
    assert_no_secret: Callable[..., None],
) -> None:
    caplog.set_level(logging.DEBUG)
    body = '{"refresh_token": "rt-secret-value", "password": "hunter2", "client_secret": "cs"}'
    escaped = r"b'{\"access_token\": \"at-secret\"}'"
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
    logging.getLogger("httpx").info("headers Authorization: Bearer %s", canary_token)
    logging.getLogger("httpcore.http11").debug("send %s and %s", canary_token, body)
    logging.getLogger("contractum.auth").warning("%s %s %s", escaped, jwt, "plain text")
    text = caplog.text
    assert_no_secret(
        text,
        canary_token,
        "rt-secret-value",
        "hunter2",
        "at-secret",
        jwt,
        "c2lnbmF0dXJl",
        'client_secret": "cs',
    )
    assert "Bearer [REDACTED]" in text
    assert '"refresh_token": "[REDACTED]"' in text
    assert "plain text" in text


def test_other_loggers_are_left_alone(redaction: None, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger("unrelated").info("Bearer abc.def is not scrubbed here")
    assert "Bearer abc.def" in caplog.text


def test_a_record_that_cannot_be_formatted_is_still_emitted(
    redaction: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger("httpx").info("bad %d", "not a number")
    assert "bad %d" in caplog.text


def test_install_is_idempotent(redaction: None) -> None:
    factory = logging.getLogRecordFactory()
    install_log_redaction()
    install_log_redaction()
    assert logging.getLogRecordFactory() is factory


def test_redact_keeps_ordinary_text() -> None:
    assert redact("GET /api/v1/identity/me 200") == "GET /api/v1/identity/me 200"
    assert redact('"token_type": "Bearer"') == '"token_type": "Bearer"'


# --- header parsers -------------------------------------------------------------------------


def test_parse_deprecation_reads_a_structured_date() -> None:
    assert parse_deprecation("@1767225600") == datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_sunset_reads_an_http_date() -> None:
    parsed = parse_sunset("Mon, 01 Mar 2027 23:59:59 GMT")
    assert parsed == datetime(2027, 3, 1, 23, 59, 59, tzinfo=UTC)


def test_parse_sunset_treats_a_zone_less_date_as_utc() -> None:
    parsed = parse_sunset("Mon, 01 Mar 2027 23:59:59 -0000")
    assert parsed is not None
    assert parsed.tzinfo is not None


@pytest.mark.parametrize("value", [None, "", "1767225600", "@abc", "@", "@99999999999999", "now"])
def test_parse_deprecation_returns_none_for_malformed_input(value: str | None) -> None:
    assert parse_deprecation(value) is None


@pytest.mark.parametrize("value", [None, "", "   ", "tomorrow", "Mon, 99 Foo 2027 99:99:99 GMT"])
def test_parse_sunset_returns_none_for_malformed_input(value: str | None) -> None:
    assert parse_sunset(value) is None
