"""Validation app factory (``uvicorn --factory dc_validation.main:create_validation_app``).

Rule-set authoring routes, the internal validation-run route (Phase 5) and the internal rule
impact routes (Phase 6: dry-run and drift) are the business routes; the app owns its database
and outbox and is fail-closed (every decision goes through identity via ``RemoteAuthorizer``).
"""

from __future__ import annotations

import httpx
from dc_auth.about import about_router
from dc_auth.deps import auth_lifespan
from dc_auth.issuers import AuthSettings
from dc_auth.remote import remote_authorizer_factory
from dc_core.app import create_app
from dc_core.db import make_async_engine, make_session_factory
from fastapi import FastAPI

from dc_validation.api.folder_usage import internal_router as folder_usage_router
from dc_validation.api.impact import internal_router as impact_router
from dc_validation.api.rulesets import router as rulesets_router
from dc_validation.api.runs import internal_router
from dc_validation.api.runs import router as runs_router
from dc_validation.migrate import VERSION_TABLE
from dc_validation.models import OutboxEvent
from dc_validation.runtime_settings import settings_client_hook
from dc_validation.sandbox import sandbox_hook
from dc_validation.settings import ValidationSettings


def create_validation_app(
    settings: ValidationSettings | None = None,
    auth_settings: AuthSettings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Every decision goes through identity (``RemoteAuthorizer``); failures fail closed (503).

    ``http_client`` is for tests: it serves JWKS fetches and the authorization calls.
    """
    settings = settings or ValidationSettings()
    auth_settings = auth_settings or AuthSettings()
    engine = make_async_engine(settings.sqlalchemy_url())
    session_factory = make_session_factory(engine)
    return create_app(
        version_route=True,  # GET /api/v1/<module>/version (public)
        service="validation",
        settings=settings,
        routers=[rulesets_router, runs_router],
        internal_routers=[
            internal_router,
            impact_router,
            folder_usage_router,
            about_router(
                "validation",
                session_factory=session_factory,
                version_table=VERSION_TABLE,
                setting_defaults={
                    "governance": {"mandatory_warn_days": settings.mandatory_warn_days}
                },
            ),
        ],
        session_factory=session_factory,
        outbox_model=OutboxEvent,
        lifespan_hooks=[
            auth_lifespan(
                auth_settings,
                authorizer_factory=remote_authorizer_factory(auth_settings),
                http_client=http_client,
            ),
            settings_client_hook(auth_settings),
            sandbox_hook(settings),
        ],
    )
