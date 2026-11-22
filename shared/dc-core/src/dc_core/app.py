"""Service factory: ``create_app`` builds every service with the same pipeline.

Middleware order (outermost first): SecurityHeadersMiddleware (nosniff and a default
``Cache-Control: no-store``, except the service's own openapi.json), CorrelationIdMiddleware,
EncodedPathGuard, routing.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import APIRouter, FastAPI, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send

from dc_core.events import JetStreamPublisher
from dc_core.logging import configure_logging
from dc_core.openapi import build_openapi
from dc_core.policy import PolicyRouter, public
from dc_core.problems import ProblemError, install_problem_handlers, problem_response
from dc_core.relay import OutboxRelay
from dc_core.security_headers import SecurityHeadersMiddleware
from dc_core.settings import CoreSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LifespanHook = Callable[[FastAPI], AbstractAsyncContextManager[None]]

_FORBIDDEN_ENCODINGS = (b"%2f", b"%5c", b"%00", b"%3b")


class EncodedPathGuard:
    """Reject raw paths containing an encoded slash, backslash, NUL or semicolon.

    Per D-18 the gateway no longer filters these before routing (Traefik's entrypoint filter
    cannot render problem+json), and per CLAUDE.md no security control may live only in the
    replaceable gateway, so every service enforces it itself. The guard is a pure ASGI
    middleware that runs before routing and before authentication. It inspects only the path
    (``raw_path`` excludes the query string, so ``?path=a%2Fb`` is unaffected) and never
    echoes the path back.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Fresh per-request log context (tasks can be reused by some servers and by tests).
        structlog.contextvars.clear_contextvars()
        raw: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
        lowered = raw.lower()
        if any(token in lowered for token in _FORBIDDEN_ENCODINGS):
            response = problem_response(
                400,
                "invalid-path",
                "Invalid request path",
                "The request path contains a character that is not permitted.",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _health_router() -> APIRouter:
    router = PolicyRouter()

    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    async def readyz(request: Request) -> dict[str, str]:
        factory = request.app.state.session_factory
        if factory is None:
            return {"status": "ok"}
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            raise ProblemError(
                503, "service-unavailable", "Service Unavailable", "The service is not ready."
            ) from exc
        return {"status": "ok"}

    router.add_api_route(
        "/healthz", healthz, methods=["GET"], dependencies=[public()], include_in_schema=False
    )
    router.add_api_route(
        "/readyz", readyz, methods=["GET"], dependencies=[public()], include_in_schema=False
    )
    return router


class ServiceVersionOut(BaseModel):
    """What build of a service is running: the platform version, git commit and build time."""

    service: str
    version: str
    commit: str
    built_at: str


#: Build arguments the service image bakes in (infra/docker/service.Dockerfile); "dev" otherwise.
#: DC_VERSION is the platform version the Release workflow builds (one for every image).
BUILD_VERSION_ENV = "DC_VERSION"
BUILD_COMMIT_ENV = "DC_GIT_SHA"
BUILD_TIME_ENV = "DC_BUILD_TIME"
DEV = "dev"


def platform_version() -> str:
    """The platform version of this build (``DC_VERSION``), or "dev" for a local build."""
    return os.environ.get(BUILD_VERSION_ENV) or DEV


def service_version(service: str) -> ServiceVersionOut:
    """The running build of ``service``: platform version, commit and build time."""
    return ServiceVersionOut(
        service=service,
        version=platform_version(),
        commit=os.environ.get(BUILD_COMMIT_ENV) or DEV,
        built_at=os.environ.get(BUILD_TIME_ENV) or DEV,
    )


def _version_router(service: str) -> APIRouter:
    """``GET /api/v1/<service>/version``: public and cacheable, so the UI can show the build."""
    router = PolicyRouter(prefix=f"/api/v1/{service}")
    info = service_version(service)

    async def get_service_version(response: Response) -> ServiceVersionOut:
        response.headers["Cache-Control"] = "public, max-age=60"
        return info

    router.add_api_route(
        "/version",
        get_service_version,
        methods=["GET"],
        dependencies=[public()],
        response_model=ServiceVersionOut,
        summary="Service build version",
        description=(
            "The running build of this service: the platform version (the same for every "
            'component of a release), git commit and build time ("dev" for a local build). '
            "Public, no token; cacheable for 60 seconds."
        ),
    )
    return router


def _relay_hook(
    service: str,
    settings: CoreSettings,
    session_factory: async_sessionmaker[AsyncSession],
    outbox_model: type,
) -> LifespanHook:
    """Start the outbox relay for the app's lifetime (only when NATS is configured)."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        publisher = JetStreamPublisher(
            settings.nats_url or "",
            user=settings.nats_user,
            password=settings.nats_password.get_secret_value() if settings.nats_password else None,
        )
        relay = OutboxRelay(session_factory, outbox_model, service=service, publisher=publisher)
        task = asyncio.create_task(relay.run(), name=f"outbox-relay-{service}")
        app.state.outbox_relay = relay
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await publisher.close()

    return _hook


def create_app(
    *,
    service: str,
    settings: CoreSettings,
    routers: Sequence[APIRouter],
    internal_routers: Sequence[APIRouter] = (),
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    outbox_model: type | None = None,
    lifespan_hooks: Sequence[LifespanHook] = (),
    version_route: bool = False,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(service, settings.log_level)
        try:
            async with AsyncExitStack() as stack:
                for hook in lifespan_hooks:
                    await stack.enter_async_context(hook(app))
                if settings.nats_url and outbox_model is not None and session_factory is not None:
                    relay_hook = _relay_hook(service, settings, session_factory, outbox_model)
                    await stack.enter_async_context(relay_hook(app))
                yield
        finally:
            engine: Any = session_factory.kw.get("bind") if session_factory is not None else None
            if engine is not None:
                await engine.dispose()

    openapi_url = f"/api/v1/{service}/openapi.json"
    app = FastAPI(
        title=f"contractum {service}",
        version=platform_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.outbox_model = outbox_model

    # add_middleware: the last one added is the outermost.
    app.add_middleware(EncodedPathGuard)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, cacheable_paths=frozenset({openapi_url}))
    install_problem_handlers(app)

    for router in routers:
        app.include_router(router)
    if version_route:
        app.include_router(_version_router(service))
    for router in internal_routers:
        app.include_router(router, include_in_schema=False)
    app.include_router(_health_router())

    def _openapi() -> dict[str, Any]:
        # Built once, from the complete route table (D-179): tags, security, problem responses.
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app, service=service)
        return app.openapi_schema

    app.openapi = _openapi  # type: ignore[method-assign]
    return app


async def read_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Session with no actor: any write raises (reads only)."""
    factory = request.app.state.session_factory
    async with factory() as session:
        session.sync_session.info["read_only"] = True
        yield session
