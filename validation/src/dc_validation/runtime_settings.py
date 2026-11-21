"""Governance settings as validation uses them at runtime (quick 261120-0ye, D-34).

Identity owns the stored overrides (platform settings, section ``governance``); the Helm/env value
``DC_MANDATORY_WARN_DAYS`` of ``ValidationSettings`` stays the default. ``effective_warn_days``
reads the stored grace period through the shared ``RuntimeSettingsClient`` with the caller's own
bearer (30 s cache, last good value when identity cannot answer). A value that was never read, or
that does not parse, falls back to the env value. Migrations never use this: they run with no
caller and keep reading the env value.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from dc_auth.issuers import AuthSettings
from dc_core.app import LifespanHook
from dc_core.platform_settings import GovernanceSettings, RuntimeSettingsClient
from fastapi import FastAPI
from pydantic import ValidationError

from dc_validation.settings import ValidationSettings

_log = structlog.get_logger("dc_validation.runtime_settings")

SECTION = "governance"


async def effective_warn_days(
    client: RuntimeSettingsClient | None, bearer: str | None, settings: ValidationSettings
) -> int:
    """The grace period in force: the stored value, else ``settings.mandatory_warn_days``."""
    if client is None or bearer is None:
        return int(settings.mandatory_warn_days)
    raw = await client.get(SECTION, bearer=bearer)
    if raw is None:
        return int(settings.mandatory_warn_days)
    try:
        stored = GovernanceSettings.model_validate(raw)
    except ValidationError:
        _log.warning("settings_invalid", section=SECTION)
        return int(settings.mandatory_warn_days)
    if stored.mandatory_warn_days is None:
        return int(settings.mandatory_warn_days)
    return int(stored.mandatory_warn_days)


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


__all__ = ["effective_warn_days", "settings_client_hook"]
