"""Contracts app factory (``uvicorn --factory dc_contracts.main:create_contracts_app``)."""

from __future__ import annotations

import httpx
from dc_auth.about import about_router
from dc_auth.deps import auth_lifespan
from dc_auth.issuers import AuthSettings
from dc_auth.remote import remote_authorizer_factory
from dc_core.access import access_log_hook
from dc_core.app import create_app
from dc_core.db import make_async_engine, make_session_factory
from dc_core.platform_settings import notifications_defaults
from fastapi import FastAPI

from dc_contracts.api.attachments import router as attachments_router
from dc_contracts.api.cards import router as cards_router
from dc_contracts.api.contracts import router as contracts_router
from dc_contracts.api.drafts import router as drafts_router
from dc_contracts.api.folder_usage import internal_router as folder_usage_router
from dc_contracts.api.governance import router as governance_router
from dc_contracts.api.settings import TestEmailLimiter
from dc_contracts.api.settings import router as settings_router
from dc_contracts.api.templates import router as templates_router
from dc_contracts.api.versions import router as versions_router
from dc_contracts.delivery import notifier_hook
from dc_contracts.migrate import VERSION_TABLE
from dc_contracts.models import OutboxEvent
from dc_contracts.notice_consumer import notice_consumer_hook
from dc_contracts.runtime_settings import settings_client_hook
from dc_contracts.scheduler import scheduler_hook
from dc_contracts.settings import ContractsSettings
from dc_contracts.validation_client import validation_hook
from dc_contracts.webhook_host_seed import webhook_host_seed_hook


def create_contracts_app(
    settings: ContractsSettings | None = None,
    auth_settings: AuthSettings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    validation_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Every decision goes through identity (``RemoteAuthorizer``); failures fail closed (503).

    ``http_client`` is for tests: it serves JWKS fetches and the authorization calls.
    ``validation_http_client`` is for tests too: it carries the publish gate's calls to the
    validation service. The real gate is always installed (VAL-07): a missing or unreachable
    validation service makes publish answer 503, never pass.
    """
    settings = settings or ContractsSettings()
    auth_settings = auth_settings or AuthSettings()
    engine = make_async_engine(settings.sqlalchemy_url())
    session_factory = make_session_factory(engine)
    app = create_app(
        version_route=True,  # GET /api/v1/<module>/version (public)
        service="contracts",
        settings=settings,
        routers=[
            contracts_router,
            drafts_router,
            versions_router,
            templates_router,
            attachments_router,
            cards_router,
            governance_router,
            settings_router,
        ],
        internal_routers=[
            folder_usage_router,
            about_router(
                "contracts",
                session_factory=session_factory,
                version_table=VERSION_TABLE,
                setting_defaults={
                    "notifications": notifications_defaults(
                        retry_schedule_s=settings.notify_retry_schedule_s,
                        max_attempts=settings.notify_max_attempts,
                        sunset_lead_days=settings.sunset_lead_days,
                    ),
                    "governance": {
                        "default_odcs_api_version": settings.default_odcs_api_version,
                        "require_lifecycle_reason": settings.require_lifecycle_reason,
                        "odcs_api_versions": sorted(settings.supported_odcs_versions),
                    },
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
            webhook_host_seed_hook(settings),
            validation_hook(settings, http_client=validation_http_client),
            notice_consumer_hook(settings),
            notifier_hook(settings),
            scheduler_hook(settings),
            access_log_hook(settings),
        ],
    )
    app.state.test_email_limiter = TestEmailLimiter()
    return app
