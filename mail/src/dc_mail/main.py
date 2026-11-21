"""Mail app factory (``uvicorn --factory dc_mail.main:create_mail_app``).

One internal route, no database, no outbox, no queue. Callers are authenticated by ``dc_auth`` and
authorized by the identity PDP (``mail:send``) like every other ``/internal`` route (D-105).

The From name and address are the Global admin's stored sender (platform settings, section
``notifications``), resolved here with the request's own bearer and never taken from the caller;
without a readable stored value ``DC_SMTP_FROM`` is used (quick 261120-0yd, D-26).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email.utils import formataddr
from typing import Annotated

import httpx
import structlog
from asgi_correlation_id import correlation_id
from dc_auth.about import about_router
from dc_auth.authorizer import AuthorizerFactory
from dc_auth.deps import auth_lifespan, authenticate, parse_bearer, require
from dc_auth.issuers import AuthSettings
from dc_auth.principal import Principal
from dc_auth.remote import remote_authorizer_factory
from dc_core.app import LifespanHook, create_app
from dc_core.mail import MAX_MAIL_BODY_BYTES, MailSendIn, MailSendOut
from dc_core.platform_settings import RuntimeSettingsClient, Sender
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, FastAPI, Request
from pydantic import ValidationError

from dc_mail.settings import MailSettings
from dc_mail.smtp import SmtpUnavailable, build_message, send

_log = structlog.get_logger("dc_mail")

router = PolicyRouter(prefix="/internal/v1/mail")


def _invalid(errors: list[dict[str, str]]) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Unprocessable Content",
        "The request failed validation.",
        errors=errors,
    )


def _validation_problem(exc: ValidationError) -> ProblemError:
    """The platform 422: locations and validator messages only, never the submitted values."""
    errors: list[dict[str, str]] = []
    for err in exc.errors(include_input=False, include_context=False, include_url=False):
        if err["type"] == "extra_forbidden":
            pointer = ""  # the key name is caller input
        else:
            pointer = "/" + "/".join(
                str(p).replace("~", "~0").replace("/", "~1") for p in err["loc"]
            )
        errors.append({"pointer": pointer, "detail": str(err["msg"])})
    return _invalid(errors)


async def resolve_sender(request: Request, settings: MailSettings) -> tuple[str, str]:
    """The From header and its address, from the platform settings store (D-26).

    The service reads ``notifications.sender`` itself with the request's own bearer (the notifier
    key or a Global admin's token); a caller cannot choose it. Unreadable or invalid means the last
    good value (kept by the client), else ``DC_SMTP_FROM``. Never logs the address.
    """
    client: RuntimeSettingsClient = request.app.state.settings_client
    raw = await client.get("notifications", bearer=parse_bearer(request))
    sender = Sender()
    if raw is not None and isinstance(raw.get("sender"), dict):
        try:
            sender = Sender.model_validate(raw["sender"])
        except ValidationError:
            sender = Sender()
    address = sender.address or settings.smtp_from
    return (formataddr((sender.name, address)) if sender.name else address), address


def settings_client_hook(auth_settings: AuthSettings, settings: MailSettings) -> LifespanHook:
    """Lifespan hook: ``app.state.settings_client`` reads identity over the shared HTTP client."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings_client = RuntimeSettingsClient(
            auth_settings.identity_internal_url,
            ttl_s=settings.settings_ttl_s,
            client=app.state.http,
        )
        yield

    return _hook


async def _read_body_capped(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise _invalid([{"pointer": "", "detail": "The request body is too large."}])
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _invalid([{"pointer": "", "detail": "The request body is too large."}])
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/send",
    dependencies=[require("mail:send")],
    response_model=MailSendOut,
    responses={
        401: {"description": "No valid credential"},
        403: {"description": "The caller does not hold mail:send"},
        404: {"description": "Not found"},
        422: {"description": "The request failed validation"},
        503: {"description": "Mail delivery or authorization is temporarily unavailable"},
    },
)
async def send_mail(
    request: Request, principal: Annotated[Principal, Depends(authenticate)]
) -> MailSendOut:
    # The body is read only after the policy dependency ran: an unauthorized caller never makes
    # the service buffer or parse anything.
    settings: MailSettings = request.app.state.settings
    raw = await _read_body_capped(request, MAX_MAIL_BODY_BYTES)
    try:
        payload = MailSendIn.model_validate_json(raw)
    except ValidationError as exc:
        raise _validation_problem(exc) from None
    from_header, from_address = await resolve_sender(request, settings)
    message = build_message(
        settings,
        payload.to,
        payload.subject,
        payload.text,
        from_header=from_header,
        from_address=from_address,
    )
    recipient_count = len(payload.to)
    try:
        result = await send(settings, message, payload.to)
    except SmtpUnavailable as exc:
        _log.warning(
            "mail_send_failed",
            request_id=correlation_id.get(),
            principal_id=principal.id,
            recipient_count=recipient_count,
            accepted=0,
            refused=0,
            outcome=exc.code,
        )
        raise ProblemError(
            503,
            "service-unavailable",
            "Service Unavailable",
            "Mail delivery is temporarily unavailable.",
        ) from None
    outcome = "sent" if result.refused == 0 else "refused" if result.accepted == 0 else "partial"
    _log.info(
        "mail_sent",
        request_id=correlation_id.get(),
        principal_id=principal.id,
        recipient_count=recipient_count,
        accepted=result.accepted,
        refused=result.refused,
        deferred=result.deferred,
        outcome=outcome,
    )
    return result


def create_mail_app(
    settings: MailSettings | None = None,
    auth_settings: AuthSettings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    authorizer_factory: AuthorizerFactory | None = None,
) -> FastAPI:
    """``authorizer_factory`` defaults to identity's PDP; tests pass a stub."""
    settings = settings or MailSettings()
    auth_settings = auth_settings or AuthSettings()
    return create_app(
        service="mail",
        settings=settings,
        routers=[],
        internal_routers=[
            router,
            about_router(
                "mail",
                setting_defaults={
                    "notifications": {"sender": {"name": None, "address": settings.smtp_from}}
                },
            ),
        ],
        lifespan_hooks=[
            auth_lifespan(
                auth_settings,
                authorizer_factory=authorizer_factory or remote_authorizer_factory(auth_settings),
                http_client=http_client,
            ),
            settings_client_hook(auth_settings, settings),
        ],
    )
