"""Quick 261120-s0p (item 6): host normalisation and the retrying seed task, with no database."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from dc_contracts import webhook_host_seed as seed_module
from dc_contracts.settings import ContractsSettings
from dc_contracts.webhook_host_seed import WebhookHostSeed, normalise_host, run_seed
from pydantic import SecretStr

pytestmark = pytest.mark.unit

KEY = "dcp_testkey_notifier-secret-value"  # noqa: S105 - a test token, not a secret


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://Hooks.Example.COM/x?y=1", "hooks.example.com"),
        ("https://hooks.example.com./x", "hooks.example.com"),
        ("https://user:pw@hooks.example.com:8443/x", "hooks.example.com"),
        ("https://münchen.example.org/x", "xn--mnchen-3ya.example.org"),
        ("https://[2001:db8::1]/x", "2001:db8::1"),
        ("not a url", None),
        ("https:///path", None),
        ("", None),
        ("https://" + "a" * 254 + ".example.com/x", None),
        ("https://[::1/x", None),
        ("https://./x", None),
        ("https://" + ("a" * 60 + ".") * 4 + "example.com/x", None),
    ],
)
def test_the_host_is_normalised_like_the_allow_list_compares_it(url: str, host: str | None) -> None:
    assert normalise_host(url) == host


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self.invalidated = 0

    def invalidate(self) -> None:
        self.invalidated += 1


def _app(handler: Any, client: _Client) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            session_factory=_Session,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            auth_settings=SimpleNamespace(identity_internal_url="http://identity:8000/"),
            settings_client=client,
        )
    )


def _settings() -> ContractsSettings:
    return ContractsSettings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
        mail_api_key=SecretStr(KEY),
    )


async def test_the_seed_retries_every_failure_then_marks_done_and_drops_the_cached_list(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_hosts(session: object) -> list[str]:
        return ["hooks.example.com", "other.example.com"]

    waits: list[float] = []

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(seed_module, "configured_hosts", fake_hosts)
    monkeypatch.setattr(seed_module.asyncio, "sleep", fake_sleep)
    seen: list[httpx.Request] = []
    answers = iter([503, "boom", 403, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = next(answers)
        if answer == "boom":
            raise httpx.ConnectError("down", request=request)
        if answer == 200:
            return httpx.Response(200, json={"seeded": True, "added": 2, "skipped": 0})
        return httpx.Response(int(answer), json={})

    client = _Client()
    app = _app(handler, client)
    seed = WebhookHostSeed()
    await run_seed(app, _settings(), seed, retry_initial_s=1.0, retry_max_s=3.0)

    assert seed.done is True
    assert client.invalidated == 1
    assert waits == [1.0, 2.0, 3.0]  # exponential, capped
    assert len(seen) == 4
    for request in seen:
        assert str(request.url) == "http://identity:8000/internal/v1/settings/webhook-hosts/seed"
        assert request.headers["authorization"] == f"Bearer {KEY}"
    assert seen[-1].content == b'{"hosts":["hooks.example.com","other.example.com"]}'


async def test_a_seed_without_the_notifier_key_is_refused_loudly() -> None:
    settings = ContractsSettings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="notifier key"):
        await run_seed(_app(lambda r: httpx.Response(200), _Client()), settings, WebhookHostSeed())


def _seeded(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"seeded": False, "added": 0, "skipped": 0})


async def _no_hosts(session: object) -> list[str]:
    return []


async def test_a_seed_with_no_settings_client_still_marks_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seed_module, "configured_hosts", _no_hosts)
    app = _app(_seeded, _Client())
    app.state.settings_client = None
    seed = WebhookHostSeed()
    await run_seed(app, _settings(), seed)
    assert seed.done is True


async def test_cancelling_the_seed_stops_it_without_marking_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancelled(session: object) -> list[str]:
        raise asyncio.CancelledError

    monkeypatch.setattr(seed_module, "configured_hosts", cancelled)
    client = _Client()
    seed = WebhookHostSeed()
    with pytest.raises(asyncio.CancelledError):
        await run_seed(_app(_seeded, client), _settings(), seed)
    assert seed.done is False
    assert client.invalidated == 0
