"""The HTTP client factory: base URL policy, TLS, timeouts and the shared request hooks."""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import ssl
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from contractum import __version__
from contractum.errors import ConfigError, NetworkError, ServerError, WriteNotAllowed

CONNECT_TIMEOUT = 10.0
MAX_RETRIES = 3
MAX_RETRY_AFTER = 30.0
_RETRY_STATUS = frozenset({429, 502, 503, 504})
_IDEMPOTENT = frozenset({"GET", "HEAD"})
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_SECURE_SCHEME = "https"
_PLAIN_SCHEME = "http"


@dataclass(frozen=True)
class ConnectionSettings:
    """What the transport needs to reach one Contractum server."""

    url: str
    ca_bundle: Path | None = None
    allow_insecure_http: bool = False
    timeout: float = 30.0


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(url: str, allow_insecure_http: bool = False) -> str:
    """Return the base URL without a trailing slash, or raise ``ConfigError``.

    Only ``https`` is accepted, plus ``http`` for loopback hosts or when ``allow_insecure_http``
    is set. A URL with credentials, a query or a fragment is refused, because the bearer token is
    sent to whatever host this names.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in (_SECURE_SCHEME, _PLAIN_SCHEME) or not parts.netloc:
        raise ConfigError(
            "the Contractum URL must be an https URL with a host", hint="check CONTRACTUM_URL"
        )
    if "@" in parts.netloc:
        raise ConfigError("the Contractum URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ConfigError("the Contractum URL must not contain a query or a fragment")
    try:
        _ = parts.port
    except ValueError:
        raise ConfigError("the Contractum URL has an invalid port") from None
    host = (parts.hostname or "").lower()
    if not host:
        raise ConfigError("the Contractum URL must have a host")
    if scheme == _PLAIN_SCHEME and not (allow_insecure_http or _is_loopback(host)):
        raise ConfigError(
            "plain http is only allowed for localhost",
            hint="use an https URL, or pass --allow-insecure-http for a trusted network",
        )
    return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _verify(ca_bundle: Path | None) -> ssl.SSLContext | bool:
    """Return the TLS verification setting: always on, with an optional custom CA bundle."""
    if ca_bundle is None:
        return True
    try:
        context = ssl.create_default_context(cafile=str(ca_bundle))
    except (OSError, ssl.SSLError):
        raise ConfigError(
            "the CA bundle could not be read", hint="check CONTRACTUM_CA_BUNDLE or --ca-bundle"
        ) from None
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _make_guard(prefix: str) -> Callable[[httpx.Request], None]:
    """Return the request hook that refuses everything but reads (GET and HEAD)."""

    def guard(request: httpx.Request) -> None:
        if request.method in _IDEMPOTENT:
            return
        path = request.url.path
        relative = path[len(prefix) :] if prefix and path.startswith(prefix) else path
        raise WriteNotAllowed(f"{request.method} {relative} is not a consumer operation")

    return guard


def _refuse_redirect(response: httpx.Response) -> None:
    """Fail on a redirect: following it could carry the bearer token to another host."""
    if response.status_code in _REDIRECT_STATUS:
        response.close()
        raise ServerError(
            response.status_code,
            title="the server redirected the request",
            hint="check the Contractum URL (scheme, host and path)",
        )


def parse_deprecation(value: str | None) -> datetime | None:
    """Parse a ``Deprecation`` header (RFC 9745 ``@<unix seconds>``) into an aware UTC datetime."""
    if value is None:
        return None
    match = re.fullmatch(r"@(-?[0-9]{1,15})", value.strip())
    if match is None:
        return None
    try:
        return datetime.fromtimestamp(int(match.group(1)), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_sunset(value: str | None) -> datetime | None:
    """Parse a ``Sunset`` header (an HTTP-date) into an aware UTC datetime."""
    if value is None or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before retry ``attempt`` (0-based): Retry-After, capped, else backoff."""
    if retry_after is not None:
        text = retry_after.strip()
        if re.fullmatch(r"[0-9]{1,9}", text):
            return min(float(text), MAX_RETRY_AFTER)
        when = parse_sunset(text)
        if when is not None:
            return min(max((when - datetime.now(UTC)).total_seconds(), 0.0), MAX_RETRY_AFTER)
    return 0.5 * float(2**attempt) + secrets.SystemRandom().uniform(0, 0.25)


class RetryTransport(httpx.BaseTransport):
    """Retry reads on 429, 502, 503, 504 and connect errors; never retry any other method.

    At most ``MAX_RETRIES`` retries, waiting for ``Retry-After`` (capped at 30 seconds) or an
    exponential backoff with jitter. A connection failure that persists becomes ``NetworkError``.
    """

    def __init__(
        self, inner: httpx.BaseTransport, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._inner = inner
        self._sleep = sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        retryable = request.method in _IDEMPOTENT
        attempt = 0
        while True:
            try:
                response = self._inner.handle_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if not retryable or attempt >= MAX_RETRIES:
                    raise _network_error(exc) from None
                self._sleep(_retry_delay(attempt, None))
                attempt += 1
                continue
            except httpx.TransportError as exc:
                raise _network_error(exc) from None
            if retryable and response.status_code in _RETRY_STATUS and attempt < MAX_RETRIES:
                delay = _retry_delay(attempt, response.headers.get("retry-after"))
                response.close()
                self._sleep(delay)
                attempt += 1
                continue
            return response

    def close(self) -> None:
        self._inner.close()


def _network_error(exc: httpx.TransportError) -> NetworkError:
    """Build a ``NetworkError`` from the exception type only, never from its text."""
    if "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return NetworkError(
            "the server certificate could not be verified",
            hint="pass --ca-bundle or set CONTRACTUM_CA_BUNDLE (or SSL_CERT_FILE) for your CA",
        )
    return NetworkError(f"could not reach the server ({type(exc).__name__})")


def _inner_transport(base_url: str, verify: ssl.SSLContext | bool) -> httpx.HTTPTransport:
    """Return the real transport, routed through the environment proxy when one applies."""
    parts = urlsplit(base_url)
    proxy = urllib.request.getproxies().get(parts.scheme)
    if proxy and not urllib.request.proxy_bypass(parts.hostname or ""):
        return httpx.HTTPTransport(verify=verify, trust_env=True, proxy=proxy)
    return httpx.HTTPTransport(verify=verify, trust_env=True)


_REDACTED = "[REDACTED]"
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {_REDACTED}"),
    (re.compile(r"dcp_[A-Za-z0-9_-]+"), f"dcp_{_REDACTED}"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), _REDACTED),
    (
        re.compile(
            r'(\\?"(?:access_token|refresh_token|id_token|token|password|client_secret)\\?"'
            r'\s*:\s*\\?")[^"\\]*(\\?")'
        ),
        rf"\g<1>{_REDACTED}\g<2>",
    ),
)
_REDACTED_LOGGERS = ("httpx", "httpcore", "contractum")
_redaction_installed = False


def redact(text: str) -> str:
    """Replace bearer values, ``dcp_`` tokens, JWTs and secret JSON fields in ``text``."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _in_redacted_namespace(name: str) -> bool:
    return any(name == root or name.startswith(root + ".") for root in _REDACTED_LOGGERS)


def install_log_redaction() -> None:
    """Scrub secrets from the log records of httpx, httpcore and contractum (idempotent).

    Done when a record is created, so the child loggers of these namespaces are covered too.
    """
    global _redaction_installed  # noqa: PLW0603 - one process-wide installation flag
    if _redaction_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if _in_redacted_namespace(record.name):
            try:
                message = record.getMessage()
            except (TypeError, ValueError):
                message = str(record.msg)
            record.msg = redact(message)
            record.args = None
        return record

    logging.setLogRecordFactory(factory)
    _redaction_installed = True


def build_http_client(
    settings: ConnectionSettings,
    *,
    auth: httpx.Auth | None,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Client:
    """Build the one ``httpx.Client`` every Contractum call goes through."""
    base_url = validate_base_url(settings.url, settings.allow_insecure_http)
    prefix = urlsplit(base_url).path
    transport = RetryTransport(_inner_transport(base_url, _verify(settings.ca_bundle)), sleep=sleep)
    return httpx.Client(
        base_url=base_url,
        auth=auth,
        transport=transport,
        follow_redirects=False,
        timeout=httpx.Timeout(settings.timeout, connect=CONNECT_TIMEOUT),
        headers={"User-Agent": f"contractum/{__version__}"},
        event_hooks={"request": [_make_guard(prefix)], "response": [_refuse_redirect]},
    )
