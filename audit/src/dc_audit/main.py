"""Audit app factory (``uvicorn --factory dc_audit.main:create_audit_app``)."""

from __future__ import annotations

from dc_auth.about import about_router
from dc_auth.deps import auth_lifespan
from dc_auth.issuers import AuthSettings
from dc_auth.remote import remote_authorizer_factory
from dc_core.app import create_app
from dc_core.db import make_async_engine, make_session_factory
from fastapi import FastAPI

from dc_audit import api
from dc_audit.consumer import consumer_hook
from dc_audit.internal_api import router as internal_router
from dc_audit.migrate import VERSION_TABLE
from dc_audit.retention import retention_hook
from dc_audit.settings import AuditSettings


def create_audit_app(
    settings: AuditSettings | None = None, auth_settings: AuthSettings | None = None
) -> FastAPI:
    settings = settings or AuditSettings()
    auth_settings = auth_settings or AuthSettings()
    engine = make_async_engine(settings.sqlalchemy_url())
    session_factory = make_session_factory(engine)
    return create_app(
        version_route=True,  # GET /api/v1/<module>/version (public)
        service="audit",
        settings=settings,
        routers=[api.router],
        internal_routers=[
            internal_router,
            about_router(
                "audit",
                session_factory=session_factory,
                version_table=VERSION_TABLE,
                setting_defaults={"platform": {"audit_retention_days": settings.retention_days}},
            ),
        ],
        session_factory=session_factory,
        outbox_model=None,
        lifespan_hooks=[
            auth_lifespan(
                auth_settings, authorizer_factory=remote_authorizer_factory(auth_settings)
            ),
            consumer_hook(settings),
            retention_hook(settings),
        ],
    )
