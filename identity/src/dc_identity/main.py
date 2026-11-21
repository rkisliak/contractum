"""Identity app factory (``uvicorn --factory dc_identity.main:create_identity_app``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from dc_auth.about import about_router
from dc_auth.authz_wire import check_context
from dc_auth.deps import auth_lifespan
from dc_auth.issuers import AuthSettings, IssuerConfig, IssuerRegistry
from dc_core.app import LifespanHook, create_app
from dc_core.db import make_async_engine, make_session_factory
from dc_core.http import make_client
from fastapi import FastAPI, Request

from dc_identity.api.auth import router as auth_router
from dc_identity.api.credentials import router as credentials_router
from dc_identity.api.entra import router as entra_router
from dc_identity.api.folders import router as folders_router
from dc_identity.api.grants import router as grants_router
from dc_identity.api.groups import router as groups_router
from dc_identity.api.me import router as me_router
from dc_identity.api.principals import router as principals_router
from dc_identity.api.service_accounts import router as service_accounts_router
from dc_identity.api.sessions import me_router as me_sessions_router
from dc_identity.api.sessions import users_router as user_sessions_router
from dc_identity.api.settings import SecretExpiryCache, TestRateLimiter
from dc_identity.api.settings import internal_router as settings_internal_router
from dc_identity.api.settings import router as settings_router
from dc_identity.api.users import router as users_router
from dc_identity.api.wellknown import router as wellknown_router
from dc_identity.authz import LocalAuthorizer
from dc_identity.bootstrap import bootstrap_hook
from dc_identity.credentials import CredentialUsageRecorder, LocalOpaqueResolver
from dc_identity.directory import DirectoryClient, UnconfiguredDirectory
from dc_identity.directory.cache import DirectoryCache
from dc_identity.directory.graph import GraphDirectoryClient, GraphTokenProvider
from dc_identity.folder_usage import FolderUsagePort, folder_usage_hook
from dc_identity.gate import PrincipalGate
from dc_identity.internal_api import router as internal_router
from dc_identity.keys import RECOMMENDED_KEY_BITS, SigningKeys
from dc_identity.migrate import VERSION_TABLE
from dc_identity.models import OutboxEvent
from dc_identity.passwords import PasswordService
from dc_identity.platform_settings import SettingsStore
from dc_identity.settings import IdentitySettings

LOCAL_NAMESPACE = "local"

_log = structlog.get_logger("dc_identity.main")


def _with_local_issuer(
    auth_settings: AuthSettings, settings: IdentitySettings, keys: SigningKeys
) -> AuthSettings:
    """Identity trusts its own tokens without calling itself over HTTP (static JWKS)."""
    issuers: list[IssuerConfig] = []
    found = False
    for cfg in auth_settings.issuers:
        if cfg.issuer == settings.local_issuer:
            cfg = cfg.model_copy(update={"static_jwks": keys.jwks()})
            found = True
        issuers.append(cfg)
    if not found:
        issuers.append(
            IssuerConfig(
                issuer=settings.local_issuer,
                jwks_uri=f"{settings.local_issuer.rstrip('/')}/.well-known/jwks.json",
                audiences=(settings.token_audience,),
                namespace=LOCAL_NAMESPACE,
                static_jwks=keys.jwks(),
            )
        )
    return auth_settings.model_copy(update={"issuers": issuers})


def _build_directory(
    settings: IdentitySettings, auth_settings: AuthSettings, directory: DirectoryClient | None
) -> tuple[DirectoryClient, GraphDirectoryClient | None]:
    """The directory identity asks about Entra principals, wrapped in the cache when enabled.

    An injected directory (tests) is used as given. Otherwise a configured ``DC_GRAPH_CLIENT_ID``
    builds the Graph client for the configured Entra tenants. An Entra issuer with neither fails
    at boot (D-62) rather than answering 503 on every request.
    """
    registry = IssuerRegistry(auth_settings.issuers)
    graph: GraphDirectoryClient | None = None
    if directory is None and settings.graph_client_id:
        http = make_client(timeout_s=settings.graph_timeout_s)
        graph = GraphDirectoryClient(
            http,
            GraphTokenProvider(
                http,
                token_url_template=settings.graph_token_url_template,
                client_id=settings.graph_client_id,
                secret_file=settings.graph_client_secret_file,
                scope=settings.graph_scope,
            ),
            base_url=settings.graph_base_url,
            max_pages=settings.graph_max_pages,
            tenants=frozenset(registry.entra_tenant_ids),
        )
        directory = graph
    elif directory is None:
        if any(cfg.kind == "entra" for cfg in auth_settings.issuers):
            raise RuntimeError(
                "an Entra issuer is configured but no Graph directory: set DC_GRAPH_CLIENT_ID"
            )
        return UnconfiguredDirectory(), None  # nothing to cache: every call fails closed
    if settings.directory_cache_ttl_s > 0:
        directory = DirectoryCache(
            directory,
            ttl_s=settings.directory_cache_ttl_s,
            maxsize=settings.directory_cache_size,
        )
    return directory, graph


def _graph_lifespan(graph: GraphDirectoryClient | None) -> LifespanHook:
    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if graph is not None:
                await graph.aclose()

    return _hook


def _key_size_hook(bits: int) -> LifespanHook:
    """Warn at start (logging is configured by the lifespan, so not at construction) when the
    signing key is below the recommended size. A deployed 2048-bit key keeps working; the chart
    README documents the rotation."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        if bits < RECOMMENDED_KEY_BITS:
            _log.warning(
                "signing_key_below_recommended_size", bits=bits, recommended=RECOMMENDED_KEY_BITS
            )
        yield

    return _hook


def create_identity_app(
    settings: IdentitySettings | None = None,
    auth_settings: AuthSettings | None = None,
    directory: DirectoryClient | None = None,
    folder_usage: FolderUsagePort | None = None,
) -> FastAPI:
    settings = settings or IdentitySettings()
    keys = SigningKeys.load(settings)
    auth_settings = _with_local_issuer(auth_settings or AuthSettings(), settings, keys)
    engine = make_async_engine(settings.sqlalchemy_url())
    session_factory = make_session_factory(engine)
    directory, graph = _build_directory(settings, auth_settings, directory)
    usage = CredentialUsageRecorder(session_factory)
    platform_settings = SettingsStore(session_factory)

    async def microsoft_signin_enabled() -> bool:
        """S1: the stored setting (5 s cache) that can turn Microsoft sign-in off."""
        stored = (await platform_settings.section("security")).value
        return stored.get("microsoft_signin_enabled") is not False

    principal_gate = PrincipalGate(
        session_factory, directory, usage=usage, microsoft_signin=microsoft_signin_enabled
    )

    def authorizer_factory(request: Request) -> LocalAuthorizer:
        # D-173: identity's own routes (and require("authz:check") on the internal API) record
        # service identity and the matched route template on a denial.
        return LocalAuthorizer(
            request.app.state.session_factory,
            request.app.state.principal_gate,
            context=check_context(request),
        )

    app = create_app(
        version_route=True,  # GET /api/v1/<module>/version (public)
        service="identity",
        settings=settings,
        routers=[
            auth_router,
            credentials_router,
            entra_router,
            folders_router,
            grants_router,
            groups_router,
            me_router,
            me_sessions_router,
            principals_router,
            service_accounts_router,
            settings_router,
            users_router,
            user_sessions_router,
            wellknown_router,
        ],
        internal_routers=[
            internal_router,
            settings_internal_router,
            about_router("identity", session_factory=session_factory, version_table=VERSION_TABLE),
        ],
        session_factory=session_factory,
        outbox_model=OutboxEvent,
        lifespan_hooks=[
            auth_lifespan(
                auth_settings,
                authorizer_factory=authorizer_factory,
                opaque_resolver=LocalOpaqueResolver(session_factory),
            ),
            _key_size_hook(keys.key_size),
            bootstrap_hook(settings),
            _graph_lifespan(graph),
            usage.lifespan(settings.credential_usage_flush_s),
            folder_usage_hook(settings, port=folder_usage),
        ],
    )
    app.state.signing_keys = keys
    app.state.directory = directory
    app.state.entra_tenant_ids = IssuerRegistry(auth_settings.issuers).entra_tenant_ids
    app.state.principal_gate = principal_gate
    app.state.credential_usage = usage
    app.state.passwords = PasswordService()
    app.state.auth_settings = auth_settings
    app.state.platform_settings = platform_settings
    app.state.secret_expiry = SecretExpiryCache()
    app.state.test_limiter = TestRateLimiter()
    return app
