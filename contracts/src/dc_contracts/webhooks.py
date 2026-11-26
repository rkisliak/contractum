"""The webhook channel's secret and URL half (SUB-07, D-98, D-107).

* ``generate_secret`` makes a Standard Webhooks ``whsec_`` secret (base64 of 32 random bytes).
* ``SecretBox`` seals and opens it with Fernet; the key is the ``webhook_secret_key`` Docker secret.
* ``validate_webhook_url`` and ``check_resolved_host`` are the SSRF guard, run when a URL is stored
  and again at every send: https only by default, a host, no userinfo, a bounded length, and every
  address the host resolves to must be globally routable (``is_forbidden_address``) unless the
  hostname is on the explicit allow-list. At send time the request is also pinned
  (``pinned_request_parts``): it connects to the validated address while ``Host`` and the TLS SNI
  and certificate check keep the original hostname, so a DNS answer that changes between validation
  and connect cannot redirect it (T-04.1-29). Redirects are never followed.

The delivery half (D-98, Standard Webhooks): ``sign`` and ``signature_header`` produce the
``webhook-signature`` value, ``build_body`` the exact bytes that are signed and sent, and
``send_webhook`` makes one POST and reports a ``DeliveryOutcome`` whose error is a code from a fixed
vocabulary, never a response body, a URL or a secret.

Stdlib, ``cryptography`` and the httpx client handed in by the caller only. No function here logs or
returns a submitted URL: callers answer with a fixed message (D-97, T-04.1-03).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from pydantic import SecretStr

SECRET_PREFIX = "whsec_"  # noqa: S105 - a public format prefix, not a secret
MAX_URL_LENGTH = 2048


class WebhookUrlSettings(Protocol):
    """The settings the URL guard reads (``ContractsSettings`` satisfies it)."""

    @property
    def webhook_require_https(self) -> bool: ...

    @property
    def webhook_allowed_private_hosts(self) -> frozenset[str]: ...

    @property
    def webhook_timeout_s(self) -> float: ...


def generate_secret() -> str:
    """A new signing secret: ``whsec_`` plus the base64 of 32 random bytes (Pattern 10)."""
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(32)).decode("ascii")


class SecretBox:
    """Fernet sealing of the signing secrets stored in ``card_webhook_secret``."""

    def __init__(self, key: SecretStr) -> None:
        self._fernet = Fernet(key.get_secret_value())

    def seal(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def open(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")


class InvalidWebhookUrl(Exception):  # noqa: N818 - the name is part of the plan's interface
    """A webhook URL that may not be stored. ``code`` is a fixed word, never the URL."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ParsedWebhookUrl:
    scheme: str
    host: str
    port: int
    path_qs: str


def _has_forbidden_char(value: str) -> bool:
    return any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def validate_webhook_url(url: str, settings: WebhookUrlSettings) -> ParsedWebhookUrl:
    """Check the shape of ``url``; raises :class:`InvalidWebhookUrl` with a fixed code."""
    if len(url) > MAX_URL_LENGTH:
        raise InvalidWebhookUrl("too-long")
    if _has_forbidden_char(url):
        raise InvalidWebhookUrl("forbidden-character")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise InvalidWebhookUrl("malformed") from None
    scheme = parts.scheme.lower()
    allowed = {"https"} if settings.webhook_require_https else {"https", "http"}
    if scheme not in allowed:
        raise InvalidWebhookUrl("scheme")
    if not host:
        raise InvalidWebhookUrl("no-host")
    if "@" in parts.netloc:
        raise InvalidWebhookUrl("userinfo")
    path_qs = parts.path + (f"?{parts.query}" if parts.query else "")
    return ParsedWebhookUrl(
        scheme=scheme,
        host=host.lower(),
        port=port if port is not None else (443 if scheme == "https" else 80),
        path_qs=path_qs,
    )


async def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to. The single resolver seam tests replace."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    # Keep the resolver's own order (RFC 6724 puts the preferred family first), without repeats.
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_forbidden_address(address: str) -> bool:
    """Must a webhook never be sent to ``address``? True for anything that is not globally routable.

    That covers private (10/8, 172.16/12, 192.168/16), loopback, link-local (169.254.169.254
    included), unique-local fc00::/7, unspecified (0.0.0.0, ::), shared address space and reserved
    ranges (``is_global`` is false for all of them), and multicast, which ``is_global`` reports as
    true and is refused explicitly. An IPv4-mapped IPv6 address (``::ffff:10.0.0.1``) is judged by
    its IPv4 part, and an address that does not parse is forbidden.
    """
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            address.split("%", 1)[0]
        )
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


async def check_resolved_host(host: str, port: int, settings: WebhookUrlSettings) -> list[str]:
    """Resolve ``host`` and require every address to be global, unless the host is allow-listed.

    Returns the addresses. Raises ``InvalidWebhookUrl("dns-error")`` when the name does not
    resolve (or does not answer within ``webhook_timeout_s``) and
    ``InvalidWebhookUrl("ssrf-blocked")`` for a non-global address.
    """
    try:
        # Bounded: an authoritative server that never answers must not pin the caller (WR-02).
        async with asyncio.timeout(settings.webhook_timeout_s):
            addresses = await _resolve(host, port)
    except OSError, UnicodeError, TimeoutError:
        raise InvalidWebhookUrl("dns-error") from None
    if not addresses:
        raise InvalidWebhookUrl("dns-error")
    if host.lower() in settings.webhook_allowed_private_hosts:
        return addresses
    if any(is_forbidden_address(address) for address in addresses):
        raise InvalidWebhookUrl("ssrf-blocked")
    return addresses


# --- delivery half: signing, the body and one POST ---------------------------------------------

USER_AGENT = "contractum-webhooks/1"
ENVELOPE_SCHEMA_VERSION = 1


class WebhookSendSettings(Protocol):
    """The setting ``send_webhook`` reads (``ContractsSettings`` satisfies it)."""

    @property
    def webhook_timeout_s(self) -> float: ...


class NoticeLike(Protocol):
    """The notice fields ``build_body`` reads (``CardNotice`` satisfies it)."""

    @property
    def id(self) -> uuid.UUID: ...

    @property
    def card_id(self) -> uuid.UUID: ...

    @property
    def event_type(self) -> str: ...

    @property
    def occurred_at(self) -> datetime: ...

    @property
    def payload(self) -> Mapping[str, Any]: ...


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """The Standard Webhooks ``v1`` signature: ``v1,`` + base64 HMAC-SHA256 of ``id.ts.body``.

    ``secret`` is the ``whsec_`` string; the key is the base64-decoded part after the prefix.
    """
    key = base64.b64decode(secret.removeprefix(SECRET_PREFIX), validate=True)
    mac = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode("ascii")


def signature_header(
    signing_secrets: Sequence[str], msg_id: str, timestamp: int, body: bytes
) -> str:
    """One space-separated ``v1`` entry per secret (the current one, then the overlap one)."""
    return " ".join(sign(secret, msg_id, timestamp, body) for secret in signing_secrets)


def _utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_body(notice: NoticeLike, card_name: str) -> bytes:
    """The envelope ``{type, timestamp, data}`` as the exact bytes that are signed and sent.

    ``timestamp`` is the event time. ``data`` is the notice payload (identifiers, SemVer and event
    fields) plus the fixed ``schema_version``, ``notice_id`` and ``card``; it never carries
    contacts, members, document content or another card's data.
    """
    data: dict[str, Any] = {
        **notice.payload,
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "notice_id": str(notice.id),
        "card": {"id": str(notice.card_id), "name": card_name},
    }
    envelope = {
        "type": notice.event_type,
        "timestamp": _utc_iso(notice.occurred_at),
        "data": data,
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class DeliveryOutcome:
    """The result of one POST. ``error`` is a fixed code: ``http-<status>``, ``timeout``,
    ``connect-error``, ``tls-error`` or ``invalid-url``; never a body, URL or secret. The worker
    adds ``ssrf-blocked`` and ``dns-error`` before any connection is made."""

    ok: bool
    status: int | None
    error: str | None


def _is_tls_failure(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ssl.SSLError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def _bracketed(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _target_url(target: ParsedWebhookUrl) -> str:
    return f"{target.scheme}://{_bracketed(target.host)}:{target.port}{target.path_qs or '/'}"


def pinned_request_parts(
    target: ParsedWebhookUrl, address: str
) -> tuple[str, dict[str, str], dict[str, str]]:
    """The URL, headers and httpx extensions that pin a request to the validated ``address``.

    The URL carries the IP, so the socket cannot go anywhere else. ``Host`` keeps the original
    hostname (with the port when it is not the scheme's default), and for https the
    ``sni_hostname`` extension makes httpcore send that name as the TLS SNI and verify the
    certificate against it, exactly as if it had connected by name (A10 spike).
    """
    url = f"{target.scheme}://{_bracketed(address)}:{target.port}{target.path_qs or '/'}"
    default_port = 443 if target.scheme == "https" else 80
    host = _bracketed(target.host)
    headers = {"Host": host if target.port == default_port else f"{host}:{target.port}"}
    extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
    return url, headers, extensions


async def send_webhook(
    client: httpx.AsyncClient,
    target: ParsedWebhookUrl,
    signing_secrets: Sequence[str],
    delivery_id: uuid.UUID | str,
    body: bytes,
    settings: WebhookSendSettings,
    address: str | None = None,
) -> DeliveryOutcome:
    """POST ``body`` once, signed. ``webhook-id`` is stable per delivery, the timestamp is this
    attempt's. The response body is never read; redirects are not followed (a 3xx is a failure).

    With ``address`` (a validated, globally routable IP) the request is pinned to it: see
    :func:`pinned_request_parts`. Without it the request connects by name (allow-listed hosts).
    """
    msg_id = f"msg_{delivery_id}"
    timestamp = int(time.time())
    headers = {
        "content-type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signature_header(signing_secrets, msg_id, timestamp, body),
        "user-agent": USER_AGENT,
    }
    url = _target_url(target)
    extensions: dict[str, str] = {}
    if address is not None:
        url, pinned_headers, extensions = pinned_request_parts(target, address)
        headers.update(pinned_headers)
    try:
        # One wall-clock bound for the whole exchange, besides httpx's per-operation timeouts.
        async with asyncio.timeout(settings.webhook_timeout_s):
            async with client.stream(
                "POST",
                url,
                content=body,
                headers=headers,
                extensions=extensions,
                timeout=settings.webhook_timeout_s,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                # The body is never read: close the response right here, so the timeout scope
                # holds an explicit await point and the connection is released at once.
                await response.aclose()
    except TimeoutError, httpx.TimeoutException:
        return DeliveryOutcome(False, None, "timeout")
    except httpx.InvalidURL:
        return DeliveryOutcome(False, None, "invalid-url")
    except httpx.TransportError as exc:
        if _is_tls_failure(exc):
            return DeliveryOutcome(False, None, "tls-error")
        return DeliveryOutcome(False, None, "connect-error")
    if 200 <= status < 300:
        return DeliveryOutcome(True, status, None)
    return DeliveryOutcome(False, status, f"http-{status}")
