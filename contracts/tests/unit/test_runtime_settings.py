"""Notification settings at runtime (quick 261120-0yd, D-22 to D-25): the stored values merged over
the env ones through the runtime client, and what the worker does with them."""

from __future__ import annotations

import httpx
import pytest
import respx
from dc_contracts.delivery import retry_delay
from dc_contracts.runtime_settings import (
    effective_notifications,
    worker_notifications,
)
from dc_contracts.settings import ContractsSettings
from dc_core.platform_settings import (
    BUILTIN_MAX_ATTEMPTS,
    BUILTIN_RETRY_SCHEDULE_S,
    BUILTIN_SUNSET_LEAD_DAYS,
    RuntimeSettingsClient,
    normalise_section,
)
from pydantic import SecretStr

pytestmark = pytest.mark.unit

BASE = "http://identity:8000"
URL = f"{BASE}/internal/v1/settings/runtime"
NOTIFIER_KEY = "dcp_testkey_notifier-secret-value"  # noqa: S105 - a test token, not a secret


def settings(**extra: object) -> ContractsSettings:
    return ContractsSettings(**extra)  # type: ignore[arg-type]


def serve(stored: dict[str, object]) -> respx.Route:
    return respx.get(URL).mock(
        return_value=httpx.Response(200, json={"sections": {"notifications": stored}})
    )


@respx.mock
async def test_tracer_a_stored_retry_schedule_reaches_retry_delay_through_the_notifier_key() -> (
    None
):
    """The tracer slice: store an override, read it as the notifier, and the retry follows it."""
    stored = normalise_section("notifications", {"retry_schedule_s": [15, 45], "max_attempts": 3})
    route = serve(stored)
    cfg = settings(mail_api_key=SecretStr(NOTIFIER_KEY))
    client = RuntimeSettingsClient(BASE)
    effective = await worker_notifications(client, cfg)
    assert route.calls.last.request.headers["authorization"] == f"Bearer {NOTIFIER_KEY}"
    assert effective.retry_schedule_s == (15, 45)
    assert effective.max_attempts == 3
    assert effective.allowed_hosts_known
    assert (
        retry_delay(1, effective.retry_schedule_s, effective.max_attempts, rand=lambda: 0.5) == 15
    )
    assert (
        retry_delay(2, effective.retry_schedule_s, effective.max_attempts, rand=lambda: 0.5) == 45
    )
    assert retry_delay(3, effective.retry_schedule_s, effective.max_attempts) is None


@respx.mock
async def test_nothing_stored_means_the_env_values() -> None:
    serve({})
    effective = await effective_notifications(RuntimeSettingsClient(BASE), "tok", settings())
    assert effective.templates == {}
    assert effective.retry_schedule_s == BUILTIN_RETRY_SCHEDULE_S
    assert effective.max_attempts == BUILTIN_MAX_ATTEMPTS
    assert effective.sunset_lead_days == BUILTIN_SUNSET_LEAD_DAYS
    assert effective.allowed_hosts == ()
    assert effective.allowed_hosts_known


@respx.mock
async def test_stored_values_override_env_values_field_by_field() -> None:
    serve(
        normalise_section(
            "notifications",
            {
                "sunset_lead_days": [3, 14],
                "webhook_allowed_hosts": ["*.example.org"],
                "email_templates": {"generic": {"subject": "{card} {odcs}"}},
            },
        )
    )
    cfg = settings(notify_retry_schedule_s=(5, 6), notify_max_attempts=9)
    effective = await effective_notifications(RuntimeSettingsClient(BASE), "tok", cfg)
    assert effective.sunset_lead_days == (3, 14)
    assert effective.allowed_hosts == ("*.example.org",)
    assert effective.templates == {"generic": {"subject": "{card} {odcs}"}}
    assert effective.retry_schedule_s == (5, 6)  # not stored: the env value stays
    assert effective.max_attempts == 9


@respx.mock
async def test_an_unreadable_section_never_read_is_unknown_for_the_host_list() -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    effective = await effective_notifications(RuntimeSettingsClient(BASE), "tok", settings())
    assert not effective.allowed_hosts_known
    assert effective.allowed_hosts == ()
    assert effective.retry_schedule_s == BUILTIN_RETRY_SCHEDULE_S


@respx.mock
async def test_the_last_good_value_survives_an_outage() -> None:
    now = [0.0]
    client = RuntimeSettingsClient(BASE, ttl_s=30, clock=lambda: now[0])
    route = serve({"webhook_allowed_hosts": ["hooks.example.com"]})
    first = await effective_notifications(client, "tok", settings())
    route.mock(side_effect=httpx.ConnectError("down"))
    now[0] = 100.0
    second = await effective_notifications(client, "tok", settings())
    assert first.allowed_hosts == second.allowed_hosts == ("hooks.example.com",)
    assert second.allowed_hosts_known


@respx.mock
async def test_a_stored_section_that_does_not_parse_fails_closed() -> None:
    serve({"max_attempts": 999})
    effective = await effective_notifications(RuntimeSettingsClient(BASE), "tok", settings())
    assert not effective.allowed_hosts_known


@respx.mock
async def test_without_a_key_or_client_the_env_applies_and_no_request_is_made() -> None:
    route = serve({"max_attempts": 2})
    with_client = await worker_notifications(RuntimeSettingsClient(BASE), settings())
    without_client = await effective_notifications(None, "tok", settings())
    assert with_client.max_attempts == without_client.max_attempts == BUILTIN_MAX_ATTEMPTS
    assert with_client.allowed_hosts_known
    assert not route.called


@respx.mock
async def test_a_change_applies_after_the_cache_expires() -> None:
    now = [0.0]
    client = RuntimeSettingsClient(BASE, ttl_s=30, clock=lambda: now[0])
    route = serve({"max_attempts": 2})
    assert (await effective_notifications(client, "tok", settings())).max_attempts == 2
    route.mock(
        return_value=httpx.Response(200, json={"sections": {"notifications": {"max_attempts": 4}}})
    )
    now[0] = 29.0
    assert (await effective_notifications(client, "tok", settings())).max_attempts == 2
    now[0] = 31.0
    assert (await effective_notifications(client, "tok", settings())).max_attempts == 4


def test_the_builtin_defaults_equal_what_contracts_settings_ships() -> None:
    cfg = settings()
    assert cfg.notify_retry_schedule_s == BUILTIN_RETRY_SCHEDULE_S
    assert cfg.notify_max_attempts == BUILTIN_MAX_ATTEMPTS
    assert cfg.sunset_lead_days == BUILTIN_SUNSET_LEAD_DAYS
