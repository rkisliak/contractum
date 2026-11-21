"""Notification settings as contracts uses them at runtime (quick 261120-0yd, D-22).

Identity owns the stored overrides (platform settings, section ``notifications``); the Helm/env
values of ``ContractsSettings`` stay the defaults. ``effective_notifications`` merges the stored
values over the env ones through the shared ``RuntimeSettingsClient`` (30 s cache, last good value
when identity cannot answer), so a change applies within about 30 seconds and with no redeploy.

Two callers read it with two bearers: the delivery worker and the sunset scheduler use the notifier
key (``mail_api_key``); a request path forwards the caller's own bearer.

``allowed_hosts_known`` is the fail-closed flag of the webhook host list (D-25): it is False when
the section has never been read successfully, so a caller must not judge a host then. Since quick
261120-s0p the list is mandatory: an empty list allows no webhook host. With no notifier key at
all the worker makes no read, the deployment is treated as not configured, and the env value (an
empty list) applies, so such a deployment allows no webhook host.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog
from dc_auth.issuers import AuthSettings
from dc_core.app import LifespanHook
from dc_core.platform_settings import (
    GovernanceSettings,
    NotificationsSettings,
    RuntimeSettingsClient,
)
from fastapi import FastAPI
from pydantic import ValidationError

from dc_contracts.settings import ContractsSettings

_log = structlog.get_logger("dc_contracts.runtime_settings")

SECTION = "notifications"


@dataclass(frozen=True)
class EffectiveNotifications:
    """The notification settings in force: stored overrides over the env defaults."""

    templates: Mapping[str, Mapping[str, str]]
    retry_schedule_s: tuple[int, ...]
    max_attempts: int
    sunset_lead_days: tuple[int, ...]
    allowed_hosts: tuple[str, ...]
    allowed_hosts_known: bool


def env_notifications(settings: ContractsSettings, *, hosts_known: bool) -> EffectiveNotifications:
    """What the env alone says (no stored template, an empty allowed-hosts list)."""
    return EffectiveNotifications(
        templates={},
        retry_schedule_s=tuple(settings.notify_retry_schedule_s),
        max_attempts=settings.notify_max_attempts,
        sunset_lead_days=tuple(settings.sunset_lead_days),
        allowed_hosts=(),
        allowed_hosts_known=hosts_known,
    )


async def effective_notifications(
    client: RuntimeSettingsClient | None, bearer: str | None, settings: ContractsSettings
) -> EffectiveNotifications:
    """Stored values merged over ``settings``. Never raises.

    ``bearer`` None (no notifier key) or no client: the env values, host list treated as not
    configured. A section that was never read, or that does not parse: the env values with
    ``allowed_hosts_known`` False.
    """
    if client is None or bearer is None:
        return env_notifications(settings, hosts_known=True)
    raw = await client.get(SECTION, bearer=bearer)
    if raw is None:
        return env_notifications(settings, hosts_known=False)
    try:
        stored = NotificationsSettings.model_validate(raw)
    except ValidationError:
        _log.warning("settings_invalid", section=SECTION)
        return env_notifications(settings, hosts_known=False)
    templates = {
        event: template.model_dump(exclude_none=True)
        for event, template in (stored.email_templates or {}).items()
    }
    return EffectiveNotifications(
        templates=templates,
        retry_schedule_s=tuple(stored.retry_schedule_s or settings.notify_retry_schedule_s),
        max_attempts=stored.max_attempts or settings.notify_max_attempts,
        sunset_lead_days=tuple(stored.sunset_lead_days or settings.sunset_lead_days),
        allowed_hosts=tuple(stored.webhook_allowed_hosts or ()),
        allowed_hosts_known=True,
    )


async def worker_notifications(
    client: RuntimeSettingsClient | None, settings: ContractsSettings
) -> EffectiveNotifications:
    """``effective_notifications`` for a background worker: the notifier key is the bearer."""
    key = settings.mail_api_key
    return await effective_notifications(
        client, key.get_secret_value() if key is not None else None, settings
    )


GOVERNANCE_SECTION = "governance"


@dataclass(frozen=True)
class EffectiveGovernance:
    """G2 and G3 in force: the default ODCS apiVersion and whether a lifecycle reason is needed."""

    default_odcs_api_version: str
    require_lifecycle_reason: bool


def env_governance(settings: ContractsSettings) -> EffectiveGovernance:
    return EffectiveGovernance(
        default_odcs_api_version=settings.default_odcs_api_version,
        require_lifecycle_reason=settings.require_lifecycle_reason,
    )


async def effective_governance(
    client: RuntimeSettingsClient | None, bearer: str | None, settings: ContractsSettings
) -> EffectiveGovernance:
    """Stored governance values over the env ones, with the caller's bearer. Never raises.

    A stored apiVersion that this deployment does not support (``supported_odcs_versions``) is
    ignored in favour of the env default, so a new contract never starts with a version the
    ingest gate would refuse. A section never read, or one that does not parse: the env values.
    """
    fallback = env_governance(settings)
    if client is None or bearer is None:
        return fallback
    raw = await client.get(GOVERNANCE_SECTION, bearer=bearer)
    if raw is None:
        return fallback
    try:
        stored = GovernanceSettings.model_validate(raw)
    except ValidationError:
        _log.warning("settings_invalid", section=GOVERNANCE_SECTION)
        return fallback
    version = stored.default_odcs_api_version
    if version is None or version not in settings.supported_odcs_versions:
        version = fallback.default_odcs_api_version
    reason = stored.require_lifecycle_reason
    return EffectiveGovernance(
        default_odcs_api_version=version,
        require_lifecycle_reason=fallback.require_lifecycle_reason if reason is None else reason,
    )


def settings_client_hook(auth_settings: AuthSettings) -> LifespanHook:
    """Lifespan hook: ``app.state.settings_client`` reads identity over the shared HTTP client.

    Must run after ``auth_lifespan`` (it puts ``app.state.http`` in place).
    """

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings_client = RuntimeSettingsClient(
            auth_settings.identity_internal_url, client=app.state.http
        )
        yield

    return _hook


__all__ = [
    "EffectiveGovernance",
    "EffectiveNotifications",
    "effective_governance",
    "effective_notifications",
    "env_governance",
    "env_notifications",
    "settings_client_hook",
    "worker_notifications",
]
