"""The SSRF matrix (SUB-07, D-97, T-04.1-28): what the URL guard refuses, at write and send time.

Both times run the same two functions: ``validate_webhook_url`` (shape) and ``check_resolved_host``
(every resolved address must be globally routable unless the hostname is allow-listed). The
resolver seam ``webhooks._resolve`` is replaced, so nothing here touches real DNS.
"""

from __future__ import annotations

import pytest
from dc_contracts import webhooks
from dc_contracts.settings import ContractsSettings
from dc_contracts.webhooks import InvalidWebhookUrl

pytestmark = pytest.mark.unit

PUBLIC = "8.8.8.8"


def settings(**overrides: object) -> ContractsSettings:
    return ContractsSettings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def resolves_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    async def fake(host: str, port: int) -> list[str]:
        return list(addresses)

    monkeypatch.setattr(webhooks, "_resolve", fake)


def resolver_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    monkeypatch.setattr(webhooks, "_resolve", fake)


async def guard(url: str, cfg: ContractsSettings) -> list[str]:
    """Write-time and send-time check in one: shape, then resolution."""
    parsed = webhooks.validate_webhook_url(url, cfg)
    return await webhooks.check_resolved_host(parsed.host, parsed.port, cfg)


FORBIDDEN = [
    "10.1.2.3",
    "10.0.0.0",
    "172.16.0.1",
    "172.20.0.5",
    "172.31.255.255",
    "192.168.1.1",
    "127.0.0.1",
    "127.1.2.3",
    "::1",
    "169.254.169.254",
    "169.254.0.1",
    "fe80::1",
    "fc00::1",
    "fd12:3456:789a::1",
    "::ffff:10.0.0.1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "0.0.0.0",  # noqa: S104 - the address under test, not a bind
    "::",
    "100.64.0.1",
    "224.0.0.1",
    "255.255.255.255",
    "not-an-address",
]


@pytest.mark.parametrize("address", FORBIDDEN)
def test_a_non_global_address_is_forbidden(address: str) -> None:
    assert webhooks.is_forbidden_address(address) is True


@pytest.mark.parametrize("address", [PUBLIC, "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_a_global_address_is_not_forbidden(address: str) -> None:
    assert webhooks.is_forbidden_address(address) is False


@pytest.mark.parametrize("address", FORBIDDEN)
async def test_a_host_resolving_to_a_non_global_address_is_blocked(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolves_to(monkeypatch, address)
    with pytest.raises(InvalidWebhookUrl) as caught:
        await guard("https://hooks.example.test/x", settings())
    assert caught.value.code == "ssrf-blocked"


async def test_one_private_address_among_public_ones_blocks_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, PUBLIC, "10.0.0.1")
    with pytest.raises(InvalidWebhookUrl) as caught:
        await guard("https://hooks.example.test/x", settings())
    assert caught.value.code == "ssrf-blocked"


async def test_a_host_resolving_only_to_a_global_address_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, PUBLIC)
    assert await guard("https://hooks.example.test/x", settings()) == [PUBLIC]


async def test_a_resolver_error_or_an_empty_answer_is_a_dns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_fails(monkeypatch)
    with pytest.raises(InvalidWebhookUrl) as failed:
        await guard("https://hooks.example.test/x", settings())
    assert failed.value.code == "dns-error"
    resolves_to(monkeypatch)
    with pytest.raises(InvalidWebhookUrl) as empty:
        await guard("https://hooks.example.test/x", settings())
    assert empty.value.code == "dns-error"


async def test_an_allow_listed_hostname_may_resolve_to_a_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "172.28.0.9")
    cfg = settings(webhook_allowed_private_hosts=["webhook-sink"])
    assert await guard("https://webhook-sink/x", cfg) == ["172.28.0.9"]
    with pytest.raises(InvalidWebhookUrl) as other:  # the allow-list is by hostname only
        await guard("https://other-sink/x", cfg)
    assert other.value.code == "ssrf-blocked"


async def test_the_allow_list_match_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "172.28.0.9")
    cfg = settings(webhook_allowed_private_hosts=["WebHook-Sink"])
    assert await guard("https://WEBHOOK-SINK/x", cfg) == ["172.28.0.9"]
    assert await guard("https://webhook-sink/x", cfg) == ["172.28.0.9"]


async def test_the_allow_list_does_not_excuse_a_url_that_fails_the_shape_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "172.28.0.9")
    cfg = settings(webhook_allowed_private_hosts=["webhook-sink"])
    with pytest.raises(InvalidWebhookUrl):
        await guard("https://user:pw@webhook-sink/x", cfg)
    with pytest.raises(InvalidWebhookUrl):
        await guard("http://webhook-sink/x", cfg)  # https is still required


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://hooks.example.test/x", "scheme"),
        ("ftp://hooks.example.test/x", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("gopher://hooks.example.test/x", "scheme"),
        ("//hooks.example.test/x", "scheme"),
        ("https://u:p@hooks.example.test/x", "userinfo"),
        ("https://u@hooks.example.test/x", "userinfo"),
        ("https:///x", "no-host"),
        ("https://", "no-host"),
        ("https://hooks.example.test/" + "a" * 2040, "too-long"),
        ("https://hooks.example.test/a b", "forbidden-character"),
        ("https://hooks.example.test/a\nHost: evil", "forbidden-character"),
        ("https://hooks.example.test/a\tb", "forbidden-character"),
        ("https://hooks.example.test:notaport/x", "malformed"),
        ("https://hooks.example.test:99999/x", "malformed"),
    ],
)
def test_a_url_that_fails_the_shape_rules_is_invalid(url: str, code: str) -> None:
    assert len(url) > 2048 or code != "too-long"
    with pytest.raises(InvalidWebhookUrl) as caught:
        webhooks.validate_webhook_url(url, settings())
    assert caught.value.code == code


def test_the_exact_length_limit_is_accepted_and_one_more_is_not() -> None:
    prefix = "https://hooks.example.test/"
    ok = prefix + "a" * (webhooks.MAX_URL_LENGTH - len(prefix))
    assert len(ok) == webhooks.MAX_URL_LENGTH
    assert webhooks.validate_webhook_url(ok, settings()).host == "hooks.example.test"
    with pytest.raises(InvalidWebhookUrl) as caught:
        webhooks.validate_webhook_url(ok + "a", settings())
    assert caught.value.code == "too-long"


def test_plain_http_is_accepted_only_when_https_is_not_required() -> None:
    url = "http://hooks.example.test/x"
    with pytest.raises(InvalidWebhookUrl):
        webhooks.validate_webhook_url(url, settings())
    parsed = webhooks.validate_webhook_url(url, settings(webhook_require_https=False))
    assert (parsed.scheme, parsed.host, parsed.port) == ("http", "hooks.example.test", 80)


def test_the_guard_error_never_carries_the_url() -> None:
    secret_url = "ftp://hooks.example.test/hooks/token-abc"
    with pytest.raises(InvalidWebhookUrl) as caught:
        webhooks.validate_webhook_url(secret_url, settings())
    assert "token-abc" not in str(caught.value)
    assert "token-abc" not in repr(caught.value)


async def test_the_resolver_keeps_the_order_of_the_answer_without_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    v6 = "2606:2800:220:1:248:1893:25c8:1946"
    infos = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (v6, 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (v6, 443, 0, 0)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: infos)
    assert await webhooks._resolve("example.test", 443) == [v6, "93.184.216.34"]


async def test_a_resolver_that_never_answers_is_a_dns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def hangs(host: str, port: int) -> list[str]:
        await asyncio.sleep(3600)
        return []

    monkeypatch.setattr(webhooks, "_resolve", hangs)
    with pytest.raises(InvalidWebhookUrl) as caught:
        await guard("https://hooks.example.test/x", settings(webhook_timeout_s=0.05))
    assert caught.value.code == "dns-error"
