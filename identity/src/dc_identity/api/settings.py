"""Platform settings routes: ``/api/v1/identity/settings`` (quick 261120-0yc, D-01 to D-17).

Only a Global admin reads or writes settings: ``settings:read`` and ``settings:write`` are ROOT
permissions, so every other caller (Global reader, Org admin, an editor, a PAT) is refused with
403, and a write needs an interactive sign-in (a PAT or key gets 403
``interactive-session-required``). ``GET /public`` (banner and the Entra pre-fill values) is the
one route any signed-in principal may call. Every effective write is one audited event
(``settings.updated`` or ``settings.imported``); the same values again is a 200 with no event.

The Entra overview (E2, E4) is view only: it is computed from ``DC_AUTH_ISSUERS`` and identity's
Graph settings, never stored, and carries no secret, no secret hint and no Graph body. The
connection test (E3) answers one fixed code and is not audited (it changes no state). About (P3)
asks the other four services with the caller's own bearer.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import httpx
from dc_auth.about import ServiceAboutOut, read_migration_head
from dc_auth.deps import UnitOfWorkDep, authenticate, parse_bearer, require
from dc_auth.principal import Principal
from dc_core.app import service_version
from dc_core.body import read_body_capped
from dc_core.http import make_client
from dc_core.platform_settings import (
    DEFAULT_EMAIL_BODY,
    DEFAULT_EMAIL_SUBJECTS,
    RUNTIME_SECTIONS,
    SECTION_ORDER,
    SECTIONS,
    Banner,
    SettingsImportError,
    TemplateError,
    build_export,
    parse_export,
    render_template,
    validate_template,
)
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from dc_odcs import SUPPORTED_API_VERSIONS, load_json
from fastapi import Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from dc_identity import platform_settings as store
from dc_identity.migrate import VERSION_TABLE
from dc_identity.platform_settings import SecurityContext, SettingsStore, StoredSection
from dc_identity.security_policy import env_defaults, request_policy

router = PolicyRouter(prefix="/api/v1/identity/settings")
internal_router = PolicyRouter(prefix="/internal/v1/settings")

IMPORT_MAX_BYTES = 256 * 1024
TEST_LIMIT = 6
TEST_WINDOW_S = 60.0
ABOUT_TIMEOUT_S = 3.0
EXPIRY_OK_TTL_S = 600.0
EXPIRY_FAIL_TTL_S = 60.0
_PRINCIPAL_ONLY: dict[int | str, dict[str, Any]] = {
    401: {"description": "No valid token"},
    403: {"description": "Only a Global admin may use settings"},
}


# --- DTOs ---------------------------------------------------------------------------------------


class IdentitySettingsSectionOut(BaseModel):
    section: str
    values: dict[str, Any]
    defaults: dict[str, Any]
    defaults_source: Literal["identity", "service", "built-in"]
    revision: int
    etag: str
    updated_at: datetime | None
    updated_by: str | None


class IdentitySettingsChoicesOut(BaseModel):
    entra_tenants: list[str]
    odcs_api_versions: list[str]
    microsoft_signin_available: bool


class IdentitySettingsOut(BaseModel):
    sections: list[IdentitySettingsSectionOut]
    etag: str
    choices: IdentitySettingsChoicesOut


class IdentityBannerOut(BaseModel):
    text: str
    severity: Literal["info", "warning"]
    starts_at: datetime | None
    ends_at: datetime | None


class IdentityPasswordRulesPublicOut(BaseModel):
    min_length: int
    require_upper: bool
    require_lower: bool
    require_digit: bool
    require_symbol: bool


class IdentitySettingsPublicOut(BaseModel):
    banner: IdentityBannerOut | None
    entra_default_tenant_id: str | None
    entra_default_owner_folder_id: str | None
    default_odcs_api_version: str
    require_lifecycle_reason: bool
    credential_max_ttl_days: int
    password_rules: IdentityPasswordRulesPublicOut


class IdentitySettingsExportOut(BaseModel):
    format: str
    format_version: int
    exported_at: str
    sections: dict[str, dict[str, Any]]


class IdentitySettingsChangeOut(BaseModel):
    section: str
    field: str
    before: Any
    after: Any


class IdentitySettingsImportOut(BaseModel):
    dry_run: bool
    changes: list[IdentitySettingsChangeOut]
    etag: str


class IdentityEntraTenantOut(BaseModel):
    tenant_id: str
    issuer: str
    audiences: list[str]


class IdentityEntraSecretExpiryOut(BaseModel):
    status: Literal["known", "unknown", "not-configured"]
    expires_at: datetime | None
    days_left: int | None
    reason: str | None


class IdentityEntraOverviewOut(BaseModel):
    tenants: list[IdentityEntraTenantOut]
    graph_client_id: str | None
    graph_configured: bool
    graph_secret_configured: bool
    signin_configured: bool
    secret_expiry: IdentityEntraSecretExpiryOut


class IdentityEntraTestOut(BaseModel):
    result: Literal[
        "ok",
        "not-configured",
        "token-refused",
        "graph-forbidden",
        "graph-throttled",
        "graph-unreachable",
        "graph-error",
    ]
    tenant_id: str | None
    checked_at: datetime


class IdentityAboutServiceOut(BaseModel):
    service: str
    status: Literal["ok", "unreachable"]
    version: str | None
    commit: str | None
    built_at: str | None
    migration_head: str | None
    database: Literal["ok", "unavailable", "none"] | None
    setting_defaults: dict[str, Any]


class IdentityAboutOut(BaseModel):
    services: list[IdentityAboutServiceOut]


# --- helpers ------------------------------------------------------------------------------------


def _store(request: Request) -> SettingsStore:
    found: SettingsStore = request.app.state.platform_settings
    return found


def _tenants(request: Request) -> tuple[str, ...]:
    ids: tuple[str, ...] = request.app.state.entra_tenant_ids
    return ids


SERVICE_DEFAULTS_TTL_S = 30.0
BUILTIN_ODCS_VERSION = "v3.2.0"


async def _service_defaults(request: Request) -> dict[str, dict[str, Any]]:
    """D-28, D-43: the settings defaults reported by the other services (their about, with the
    caller's own bearer), as ``{service: setting_defaults}``. A service that does not answer is
    absent. Cached for 30 s once all four answered; defaults are Helm/env values, not secrets. The
    last answers are also kept, so the public route can show the env defaults to a non-admin."""
    cache: tuple[float, dict[str, dict[str, Any]]] | None = getattr(
        request.app.state, "service_defaults_cache", None
    )
    now = time.monotonic()
    if cache is not None and now < cache[0]:
        return cache[1]
    settings = request.app.state.settings
    bearer = parse_bearer(request)
    async with make_client(timeout_s=ABOUT_TIMEOUT_S) as http:
        answers = await asyncio.gather(
            _ask(http, "contracts", settings.contracts_url, bearer),
            _ask(http, "mail", settings.mail_url, bearer),
            _ask(http, "validation", settings.validation_url, bearer),
            _ask(http, "audit", settings.audit_url, bearer),
        )
    found = {a.service: dict(a.setting_defaults) for a in answers if a.status == "ok"}
    if found:
        previous: dict[str, dict[str, Any]] = getattr(
            request.app.state, "last_service_defaults", {}
        )
        request.app.state.last_service_defaults = {**previous, **found}
    if len(found) == len(answers):
        request.app.state.service_defaults_cache = (now + SERVICE_DEFAULTS_TTL_S, found)
    return found


def _defaults_for(section: str, found: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The slice of the services' defaults that belongs to ``section``: ``{service: values}``."""
    return {
        service: values[section]
        for service, values in found.items()
        if isinstance(values.get(section), dict)
    }


_SERVICE_SECTIONS = ("notifications", "governance", "platform")


async def _section_out(
    request: Request, stored: StoredSection, found: dict[str, dict[str, Any]] | None = None
) -> IdentitySettingsSectionOut:
    if found is None:
        found = await _service_defaults(request) if stored.section in _SERVICE_SECTIONS else {}
    service_defaults = (
        _defaults_for(stored.section, found) if stored.section in _SERVICE_SECTIONS else None
    )
    return IdentitySettingsSectionOut(
        section=stored.section,
        values=stored.value,
        defaults=store.section_defaults(
            stored.section,
            entra_tenant_ids=_tenants(request),
            service_defaults=service_defaults,
            identity_defaults=env_defaults(request.app.state.settings),
        ),
        defaults_source=store.defaults_source(stored.section, service_defaults=service_defaults),
        revision=stored.revision,
        etag=stored.etag,
        updated_at=stored.updated_at,
        updated_by=stored.updated_by,
    )


def _ordered(stored: dict[str, StoredSection]) -> list[StoredSection]:
    names = [n for n in SECTION_ORDER if n in stored]
    return [stored[n] for n in names]


def _unknown_section() -> ProblemError:
    return ProblemError(
        404, "settings-section-not-found", "Not Found", "There is no such settings section."
    )


def _import_invalid(pointer: str, detail: str) -> ProblemError:
    return ProblemError(
        422,
        "settings-import-invalid",
        "Unprocessable Content",
        detail,
        errors=[{"pointer": pointer, "detail": detail}],
    )


# --- reads --------------------------------------------------------------------------------------


@router.get(
    "",
    response_model=IdentitySettingsOut,
    dependencies=[require("settings:read")],
    summary="List the settings sections",
    responses=_PRINCIPAL_ONLY,
)
async def list_settings(request: Request) -> IdentitySettingsOut:
    stored = await _store(request).all()
    found = await _service_defaults(request)
    return IdentitySettingsOut(
        sections=[await _section_out(request, s, found) for s in _ordered(stored)],
        etag=store.combined_etag(stored),
        choices=IdentitySettingsChoicesOut(
            entra_tenants=list(_tenants(request)),
            odcs_api_versions=sorted(SUPPORTED_API_VERSIONS),
            microsoft_signin_available=bool(_tenants(request)),
        ),
    )


@router.get(
    "/public",
    response_model=IdentitySettingsPublicOut,
    dependencies=[require("settings:read-public")],
    summary="Settings every signed-in user may see",
    responses={401: {"description": "No valid token"}},
)
async def public_settings(request: Request, response: Response) -> IdentitySettingsPublicOut:
    stored = await _store(request).all()
    banner_value = stored["platform"].value.get("banner")
    banner: IdentityBannerOut | None = None
    if isinstance(banner_value, dict):
        parsed = Banner.model_validate(banner_value)
        if parsed.active_at(datetime.now(UTC)):
            banner = IdentityBannerOut(
                text=parsed.text,
                severity=parsed.severity,
                starts_at=parsed.starts_at,
                ends_at=parsed.ends_at,
            )
    entra = stored["entra"]
    owner = entra.value.get("default_owner_folder_id")
    governance = stored["governance"].value
    # The env defaults of contracts are known to identity only through an admin's last About call;
    # without one the built-in defaults are shown (the owning service enforces its own anyway).
    contracts_env: dict[str, Any] = (
        getattr(request.app.state, "last_service_defaults", {})
        .get("contracts", {})
        .get("governance", {})
    )
    version = governance.get("default_odcs_api_version")
    if not isinstance(version, str):
        version = contracts_env.get("default_odcs_api_version") or BUILTIN_ODCS_VERSION
    reason = governance.get("require_lifecycle_reason")
    if not isinstance(reason, bool):
        reason = bool(contracts_env.get("require_lifecycle_reason", False))
    policy = await request_policy(request)
    rules = policy.password_rules
    response.headers["Cache-Control"] = "private, max-age=30"
    return IdentitySettingsPublicOut(
        banner=banner,
        entra_default_tenant_id=store.effective_default_tenant(entra, _tenants(request)),
        entra_default_owner_folder_id=owner if isinstance(owner, str) else None,
        default_odcs_api_version=version,
        require_lifecycle_reason=reason,
        credential_max_ttl_days=policy.credential_max_ttl_days,
        password_rules=IdentityPasswordRulesPublicOut(
            min_length=rules.min_length,
            require_upper=rules.require_upper,
            require_lower=rules.require_lower,
            require_digit=rules.require_digit,
            require_symbol=rules.require_symbol,
        ),
    )


@router.get(
    "/export",
    response_model=IdentitySettingsExportOut,
    dependencies=[require("settings:read")],
    summary="Export the stored settings as a file",
    responses=_PRINCIPAL_ONLY,
)
async def export_settings(request: Request, response: Response) -> dict[str, Any]:
    stored = await _store(request).all()
    now = datetime.now(UTC)
    document = build_export({name: s.value for name, s in stored.items()}, now=now)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="contractum-settings-{now:%Y-%m-%d}.json"'
    )
    response.headers["Cache-Control"] = "no-store"
    return document


# --- import -------------------------------------------------------------------------------------


@router.post(
    "/import",
    response_model=IdentitySettingsImportOut,
    dependencies=[require("settings:write")],
    summary="Import settings (dry run by default)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}},
        },
    },
    responses={
        **_PRINCIPAL_ONLY,
        409: {"description": "The import would lock every admin out (signin-lockout-risk)"},
        412: {"description": "If-Match does not match the current combined ETag"},
        413: {"description": "The file is larger than 256 KiB"},
        422: {"description": "The file is not a valid Contractum settings export"},
        428: {"description": "Applying needs an If-Match header"},
    },
)
async def import_settings(
    request: Request,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
    dry_run: Annotated[bool, Query()] = True,
    if_match: Annotated[
        str | None,
        Header(description="The combined ETag of GET /settings; required to apply."),
    ] = None,
) -> IdentitySettingsImportOut:
    raw = await read_body_capped(request, IMPORT_MAX_BYTES, detail="The file is too large.")
    try:
        document = load_json(raw.decode("utf-8"))
    except ValueError, UnicodeDecodeError:  # JSON errors and NestingTooDeepError are ValueErrors
        raise _import_invalid("", "The file is not valid JSON.") from None
    try:
        incoming = parse_export(document)
    except SettingsImportError as exc:
        raise _import_invalid(exc.pointer, exc.message) from None
    tenants = _tenants(request)
    security = _security_context(request, principal)
    if dry_run:
        try:
            _, stored, changes = await store.plan_import(
                uow.session, incoming, entra_tenant_ids=tenants, security=security
            )
        except ProblemError as exc:
            raise _as_import_problem(exc) from None
        return IdentitySettingsImportOut(
            dry_run=True,
            changes=[IdentitySettingsChangeOut(**c) for c in changes],
            etag=store.combined_etag(stored),
        )
    try:
        changes = await store.apply_import(
            uow,
            _store(request),
            incoming,
            if_match=if_match,
            entra_tenant_ids=tenants,
            security=security,
        )
    except ProblemError as exc:
        raise _as_import_problem(exc) from None
    return IdentitySettingsImportOut(
        dry_run=False,
        changes=[IdentitySettingsChangeOut(**c) for c in changes],
        etag=store.combined_etag(await _store(request).all()),
    )


def _security_context(request: Request, principal: Principal) -> SecurityContext:
    """Who is saving and what the lockout rules need (D-37, D-44)."""
    return SecurityContext(
        actor=principal,
        entra_configured=bool(_tenants(request)),
        session_max_default=request.app.state.settings.session_max_s,
    )


def _as_import_problem(exc: ProblemError) -> ProblemError:
    """A 422 from identity's own checks during an import is a ``settings-import-invalid``."""
    if exc.status != 422:
        return exc
    errors = exc.extensions.get("errors") or [{"pointer": "", "detail": exc.detail or ""}]
    return ProblemError(
        422,
        "settings-import-invalid",
        "Unprocessable Content",
        exc.detail,
        errors=errors,
    )


# --- Entra overview and connection test ---------------------------------------------------------


class SecretExpiryCache:
    """E4: the secret expiry, 10 min after an answer and 60 s after a failure (D-11)."""

    def __init__(self) -> None:
        self._value: IdentityEntraSecretExpiryOut | None = None
        self._expires = 0.0

    async def get(self, request: Request) -> IdentityEntraSecretExpiryOut:
        now = time.monotonic()
        if self._value is not None and now < self._expires:
            return self._value
        value = await _read_expiry(request)
        self._value = value
        self._expires = now + (EXPIRY_OK_TTL_S if value.status == "known" else EXPIRY_FAIL_TTL_S)
        return value


async def _read_expiry(request: Request) -> IdentityEntraSecretExpiryOut:
    client_id = request.app.state.settings.graph_client_id
    tenants = _tenants(request)
    if not client_id or not tenants:
        return IdentityEntraSecretExpiryOut(
            status="not-configured", expires_at=None, days_left=None, reason=None
        )
    default = store.effective_default_tenant(await _store(request).section("entra"), tenants)
    order = ([default] if default else []) + [t for t in tenants if t != default]
    for tenant in order:
        try:
            ends = await request.app.state.directory.application_secret_expiries(tenant, client_id)
        except Exception:  # noqa: BLE001 - the overview never fails because Graph did
            ends = None
        if ends:
            latest = max(ends).astimezone(UTC)
            days = (latest - datetime.now(UTC)).days
            return IdentityEntraSecretExpiryOut(
                status="known", expires_at=latest, days_left=days, reason=None
            )
    return IdentityEntraSecretExpiryOut(
        status="unknown",
        expires_at=None,
        days_left=None,
        reason=(
            "Microsoft Graph did not report the client secret's expiry (the Graph app may live "
            "in a tenant that is not configured, or lack the permission)."
        ),
    )


@router.get(
    "/entra/overview",
    response_model=IdentityEntraOverviewOut,
    dependencies=[require("settings:read")],
    summary="Entra sign-in and Graph overview (view only)",
    responses=_PRINCIPAL_ONLY,
)
async def entra_overview(request: Request) -> IdentityEntraOverviewOut:
    settings = request.app.state.settings
    auth_settings = request.app.state.auth_settings
    tenants = [
        IdentityEntraTenantOut(
            tenant_id=cfg.tenant_id, issuer=cfg.issuer, audiences=list(cfg.audiences)
        )
        for cfg in auth_settings.issuers
        if cfg.kind == "entra" and cfg.tenant_id
    ]
    try:
        secret_present = settings.graph_client_secret_file.stat().st_size > 0
    except OSError:
        secret_present = False
    expiry = await request.app.state.secret_expiry.get(request)
    return IdentityEntraOverviewOut(
        tenants=tenants,
        graph_client_id=settings.graph_client_id,
        graph_configured=bool(settings.graph_client_id),
        graph_secret_configured=secret_present,
        signin_configured=bool(tenants),
        secret_expiry=expiry,
    )


class TestRateLimiter:
    """E3: at most ``TEST_LIMIT`` connection tests per principal per minute, in process (D-12)."""

    __test__ = False  # not a pytest class

    def __init__(self, limit: int = TEST_LIMIT, window_s: float = TEST_WINDOW_S) -> None:
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> int | None:
        """``None`` when allowed (and counted), else the seconds until the next slot."""
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, int(self._window - (now - hits[0])) + 1)
        hits.append(now)
        return None


@router.post(
    "/entra/test",
    response_model=IdentityEntraTestOut,
    dependencies=[require("settings:write")],
    summary="Test the Microsoft Graph connection",
    responses={
        **_PRINCIPAL_ONLY,
        429: {"description": "At most 6 connection tests per minute"},
    },
)
async def entra_test(
    request: Request, principal: Annotated[Principal, Depends(authenticate)]
) -> IdentityEntraTestOut:
    wait = request.app.state.test_limiter.check(principal.id)
    if wait is not None:
        raise ProblemError(
            429,
            "too-many-attempts",
            "Too Many Requests",
            "Too many connection tests; try again shortly.",
            headers={"Retry-After": str(wait)},
        )
    tenants = _tenants(request)
    tenant = None
    if tenants:
        default = store.effective_default_tenant(await _store(request).section("entra"), tenants)
        tenant = default or tenants[0]
    result: Any = "not-configured"
    if tenant is not None:
        try:
            result = await request.app.state.directory.check_connection(tenant)
        except Exception:  # noqa: BLE001 - one fixed code, never the failure
            result = "graph-error"
    return IdentityEntraTestOut(result=result, tenant_id=tenant, checked_at=datetime.now(UTC))


# --- about --------------------------------------------------------------------------------------


async def _ask(
    http: httpx.AsyncClient, service: str, base_url: str, bearer: str
) -> IdentityAboutServiceOut:
    try:
        response = await http.get(
            f"{base_url.rstrip('/')}/internal/v1/about",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if response.status_code != 200:
            raise ValueError
        about = ServiceAboutOut.model_validate(response.json())
    except httpx.HTTPError, ValueError, TypeError:
        return IdentityAboutServiceOut(
            service=service,
            status="unreachable",
            version=None,
            commit=None,
            built_at=None,
            migration_head=None,
            database=None,
            setting_defaults={},
        )
    return _about_out(about)


def _about_out(about: ServiceAboutOut) -> IdentityAboutServiceOut:
    return IdentityAboutServiceOut(
        service=about.service,
        status="ok",
        version=about.version,
        commit=about.commit,
        built_at=about.built_at,
        migration_head=about.migration_head,
        database=about.database,
        setting_defaults=about.setting_defaults,
    )


@router.get(
    "/about",
    response_model=IdentityAboutOut,
    dependencies=[require("settings:read")],
    summary="Version, build and migration state of every service",
    responses=_PRINCIPAL_ONLY,
)
async def about(request: Request) -> IdentityAboutOut:
    settings = request.app.state.settings
    bearer = parse_bearer(request)
    info = service_version("identity")
    database, head = await read_migration_head(request.app.state.session_factory, VERSION_TABLE)
    own = _about_out(
        ServiceAboutOut(
            service="identity",
            version=info.version,
            commit=info.commit,
            built_at=info.built_at,
            database=database,
            migration_head=head,
            setting_defaults={},
        )
    )
    others = (
        ("contracts", settings.contracts_url),
        ("validation", settings.validation_url),
        ("audit", settings.audit_url),
        ("mail", settings.mail_url),
    )
    async with make_client(timeout_s=ABOUT_TIMEOUT_S) as http:
        answers = await asyncio.gather(*(_ask(http, name, url, bearer) for name, url in others))
    return IdentityAboutOut(services=[own, *answers])


# --- notification template preview (quick 261120-0yd, D-21) --------------------------------------

EventTypeName = Literal[
    "contract.version.published",
    "contract.version.breaking_published",
    "contract.latest.changed",
    "contract.version.deprecated",
    "contract.version.sunset_approaching",
    "contract.version.unpublish_blocked",
    "card.subscription.removed",
    "generic",
]
_BREAKING_EVENT = "contract.version.breaking_published"
PREVIEW_FIELD_MAX = 20_000


class IdentityNotificationPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: EventTypeName
    subject: str | None = Field(default=None, max_length=PREVIEW_FIELD_MAX)
    body: str | None = Field(default=None, max_length=PREVIEW_FIELD_MAX)


class IdentityNotificationPreviewOut(BaseModel):
    subject: str
    body: str


def _preview_fields(event_type: str, subject: str) -> dict[str, str]:
    """The fixed sample a template is previewed with: ODCS id ``orders``, semver ``1.2.0``, the
    card "Analytics team", a sunset 30 days ahead, two detail lines and (breaking type only) a
    two-line breaking block."""
    today = datetime.now(UTC).date()
    breaking = (
        "\n\nBreaking changes for the versions your card pins:\n"
        "Pinned version 1.0.0: 2 breaking change(s)\n"
        "  property-removed /schema/0/properties/0 name orders_id -> -"
        if event_type == _BREAKING_EVENT
        else ""
    )
    return {
        "odcs": "orders",
        "semver": "1.2.0",
        "card": "Analytics team",
        "event": event_type,
        "status": "preview",
        "sunset_date": (today + timedelta(days=30)).isoformat(),
        "lead_days": "30",
        "deprecated_at": today.isoformat(),
        "reason": "Replaced by 2.0.0",
        "previous_latest": "1.1.0",
        "subject": subject,
        "details": "Card: Analytics team\nVersion: 1.2.0",
        "breaking_changes": breaking,
        "contract_id": "11111111-2222-4333-8444-555555555555",
        "notice_id": "66666666-7777-4888-8999-000000000000",
        "occurred_at": f"{today.isoformat()}T09:30:00+00:00",
    }


def _template_problem(pointer: str, exc: TemplateError) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Unprocessable Content",
        str(exc),
        errors=[{"pointer": pointer, "detail": str(exc)}],
    )


@router.post(
    "/notifications/preview",
    response_model=IdentityNotificationPreviewOut,
    dependencies=[require("settings:read")],
    summary="Preview an email template with sample values",
    responses={
        **_PRINCIPAL_ONLY,
        422: {"description": "The template is not valid; the error carries a JSON pointer"},
    },
)
async def preview_notification(
    body: IdentityNotificationPreviewIn,
) -> IdentityNotificationPreviewOut:
    """Renders with the shared renderer; not audited (it changes no state)."""
    subject_template = (
        body.subject if body.subject is not None else DEFAULT_EMAIL_SUBJECTS[body.event_type]
    )
    body_template = body.body if body.body is not None else DEFAULT_EMAIL_BODY
    try:
        subject_template = validate_template(subject_template, "subject")
    except TemplateError as exc:
        raise _template_problem("/subject", exc) from None
    try:
        body_template = validate_template(body_template, "body")
    except TemplateError as exc:
        raise _template_problem("/body", exc) from None
    fields = _preview_fields(body.event_type, "")
    subject = render_template(subject_template, fields, "subject")
    fields["subject"] = subject
    return IdentityNotificationPreviewOut(
        subject=subject, body=render_template(body_template, fields, "body")
    )


# --- writes (after the fixed routes, so "/{section}" never shadows them) -------------------------


@router.put(
    "/{section}",
    response_model=IdentitySettingsSectionOut,
    dependencies=[require("settings:write")],
    summary="Replace one settings section",
    responses={
        **_PRINCIPAL_ONLY,
        404: {"description": "There is no such settings section"},
        409: {
            "description": (
                "The security section would switch off the sign-in method the acting admin uses, "
                "or the last way for any admin to sign in (signin-lockout-risk)"
            )
        },
        412: {"description": "If-Match does not match the section's current ETag"},
        422: {
            "description": (
                "A value is invalid; the error carries a JSON pointer "
                "(signin-lockout-risk when both sign-in methods would be off)"
            )
        },
        428: {"description": "The request carries no If-Match header"},
    },
)
async def put_section(
    section: str,
    body: dict[str, Any],
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
    if_match: Annotated[
        str | None,
        Header(description="The section's current ETag from GET /settings (required)."),
    ] = None,
) -> IdentitySettingsSectionOut:
    if section not in SECTIONS:
        raise _unknown_section()
    updated = await store.update_section(
        uow,
        _store(request),
        section,
        body,
        if_match=if_match,
        entra_tenant_ids=_tenants(request),
        security=_security_context(request, principal),
    )
    response.headers["ETag"] = updated.etag
    return await _section_out(request, updated)


# --- internal: the runtime sections other services read -----------------------------------------


class IdentityRuntimeSettingsOut(BaseModel):
    sections: dict[str, dict[str, Any]]


@internal_router.get(
    "/runtime",
    response_model=IdentityRuntimeSettingsOut,
    dependencies=[require("settings:read-runtime")],
    summary="Runtime settings sections (internal)",
)
async def runtime_settings(
    request: Request, sections: Annotated[str, Query(min_length=1, max_length=200)]
) -> IdentityRuntimeSettingsOut:
    names = [n for n in sections.split(",") if n]
    if not names or len(set(names)) != len(names) or any(n not in RUNTIME_SECTIONS for n in names):
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "Unknown or non-runtime settings section.",
            errors=[{"pointer": "/query/sections", "detail": "not a runtime section"}],
        )
    stored = await _store(request).all()
    return IdentityRuntimeSettingsOut(sections={n: stored[n].value for n in names})


class IdentityWebhookHostSeedIn(BaseModel):
    """The hosts of the webhook channels that exist (internal; never shown to a user)."""

    model_config = ConfigDict(extra="forbid")

    hosts: list[Annotated[str, Field(max_length=253)]] = Field(max_length=1000)


class IdentityWebhookHostSeedOut(BaseModel):
    seeded: bool
    added: int
    skipped: int


@internal_router.post(
    "/webhook-hosts/seed",
    response_model=IdentityWebhookHostSeedOut,
    dependencies=[require("settings:seed-webhook-hosts")],
    summary="Seed the webhook allow-list once (internal)",
    responses={403: {"description": "The caller may not seed the webhook allow-list"}},
)
async def seed_webhook_hosts(
    body: IdentityWebhookHostSeedIn, request: Request, uow: UnitOfWorkDep
) -> IdentityWebhookHostSeedOut:
    result = await store.seed_webhook_hosts(uow, _store(request), body.hosts)
    return IdentityWebhookHostSeedOut(
        seeded=result.seeded, added=result.added, skipped=result.skipped
    )


__all__ = ["internal_router", "router"]
