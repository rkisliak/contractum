"""Local login and password rotation (AUTH-01, AUTH-09, D-01..D-04).

Login is the only public identity route besides the JWKS document. The bootstrap branch compares
SHA-256 digests of the submitted password and the secret file contents in constant time and issues
a restricted session (scope ``password:rotate``, no refresh token). Rotation is an audited write.
Error bodies never echo a submitted password.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

import structlog
import uuid_utils.compat as uuid_compat
from dc_auth.deps import UnitOfWorkDep, require
from dc_auth.principal import Principal
from dc_core.actor import Actor
from dc_core.app import read_session
from dc_core.policy import PolicyRouter, public
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from fastapi import Body, Cookie, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.bootstrap import read_bootstrap_secret
from dc_identity.gate import sign_in_method_disabled
from dc_identity.models import BootstrapState, RefreshToken, User
from dc_identity.password_policy import check_password
from dc_identity.passwords import PasswordService
from dc_identity.security_policy import SecurityPolicy, request_policy
from dc_identity.sessions import (
    enforce_session_limit,
    issue_refresh_token,
    revoke_all_for_user,
    revoke_family_for_token,
    root_folder_id,
    rotate_refresh_token,
    session_actor,
    summarize_user_agent,
)
from dc_identity.settings import IdentitySettings
from dc_identity.text import NoNulStr
from dc_identity.throttle import LoginThrottler, throttle_keys
from dc_identity.tokens import IssuedToken, issue_access_token

_log = structlog.get_logger("dc_identity.auth")

router = PolicyRouter(prefix="/api/v1/identity/auth")

# Opt-in browser delivery of the refresh token (Phase 8, D-UI-08-12). A client that sends
# ``X-Token-Delivery: cookie`` gets the token as an HttpOnly cookie scoped to this router's path and
# never in the body; every other client keeps the body form unchanged.
REFRESH_COOKIE = "__Secure-dc_refresh"  # noqa: S105 - cookie name, not a credential
REFRESH_COOKIE_PATH = "/api/v1/identity/auth"
DELIVERY_HEADER = "X-Token-Delivery"

_SET_COOKIE_ISSUED = {
    "description": (
        "Only when the request sent `X-Token-Delivery: cookie`: "
        "`__Secure-dc_refresh=<token>; HttpOnly; Secure; SameSite=Strict; "
        "Path=/api/v1/identity/auth; Max-Age=<idle timeout, or the rest of the session if "
        "shorter>`. The body then omits `refresh_token`. Absent for every other client."
    ),
    "schema": {"type": "string"},
}
_SET_COOKIE_CLEARED = {
    "description": (
        "On the cookie path, `__Secure-dc_refresh` expired (`Max-Age=0`, same attributes) so a "
        "stale cookie cannot loop."
    ),
    "schema": {"type": "string"},
}
_CSRF_RESPONSE = {
    "description": (
        "`csrf-check-failed`: the refresh token was read from the cookie without "
        "`X-Token-Delivery: cookie`, or the request carried that header with a `Sec-Fetch-Site` "
        "other than `same-origin` or `none`"
    )
}
_DISABLED_OR_CSRF_RESPONSE = {
    "description": (
        "`sign-in-method-disabled`: a Global admin turned local sign-in off; or "
        "`csrf-check-failed`: the refresh token was read from the cookie without "
        "`X-Token-Delivery: cookie`, or the request carried that header with a `Sec-Fetch-Site` "
        "other than `same-origin` or `none`"
    )
}
_BOTH_SOURCES_RESPONSE = {
    "description": "Validation error, including a refresh token in both the body and the cookie"
}

RefreshCookie = Annotated[
    SecretStr | None,
    Cookie(
        alias=REFRESH_COOKIE,
        title="Refresh cookie",
        description=(
            "The refresh token for a browser that opted into cookie delivery. Send it only with "
            "`X-Token-Delivery: cookie` and never together with a body token."
        ),
    ),
]

DeliveryHeader = Annotated[
    Literal["cookie"] | None,
    Header(
        alias=DELIVERY_HEADER,
        description=(
            "Send `cookie` to receive the refresh token as an HttpOnly cookie instead of in the "
            "response body, and to authorise the cookie as the refresh credential."
        ),
        # The one accepted value as a parameter example: generated clients and scanners (OWASP ZAP
        # fills a header from it) then send `cookie`, not an empty value that answers 422.
        openapi_examples={
            "cookie": {
                "summary": "Cookie delivery",
                "description": "Deliver the refresh token as an HttpOnly cookie.",
                "value": "cookie",
            }
        },
    ),
]


class IdentityLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: NoNulStr = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=1024)


class IdentityTokenOut(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth token type, not a credential
    expires_in: int
    scope: str
    # Present only for full-scope sessions; restricted bootstrap sessions never get one.
    refresh_token: str | None = None


class IdentitySignInMethodsOut(BaseModel):
    local: bool
    microsoft: bool


class IdentityPasswordRulesOut(BaseModel):
    min_length: int
    require_upper: bool
    require_lower: bool
    require_digit: bool
    require_symbol: bool


class IdentityAuthPolicyOut(BaseModel):
    """What a sign-in screen may know before anyone is signed in."""

    methods: IdentitySignInMethodsOut
    password_rules: IdentityPasswordRulesOut


class IdentityRefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional: a browser that opted into cookie delivery sends the token as a cookie instead.
    refresh_token: SecretStr | None = Field(default=None, min_length=1, max_length=512)


# The body is optional (a cookie-delivery browser sends none), so its schema is
# `anyOf[model, null]`, which OpenAPI tooling cannot build a request from. The example gives
# generated clients and scanners (OWASP ZAP sends it) a well-formed body to start from.
RefreshBody = Annotated[
    IdentityRefreshIn | None,
    Body(
        openapi_examples={
            "body-token": {
                "summary": "Refresh token in the body",
                "description": "Every client that did not opt into cookie delivery.",
                "value": {"refresh_token": "the-refresh-token-from-login"},
            }
        },
    ),
]


class IdentityLogoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: SecretStr | None = Field(default=None, min_length=1, max_length=512)


class IdentityPasswordRotateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: SecretStr = Field(min_length=1, max_length=1024)
    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=1024)


def _settings(request: Request) -> IdentitySettings:
    settings: IdentitySettings = request.app.state.settings
    return settings


def _passwords(request: Request) -> PasswordService:
    passwords: PasswordService = request.app.state.passwords
    return passwords


def invalid_credentials() -> ProblemError:
    return ProblemError(401, "invalid-credentials", "Unauthorized", "Invalid username or password.")


def _out(issued: IssuedToken, refresh_token: str | None = None) -> IdentityTokenOut:
    return IdentityTokenOut(
        access_token=issued.token,
        expires_in=issued.expires_in,
        scope=issued.scope,
        refresh_token=refresh_token,
    )


def _set_refresh_cookie(response: Response, raw: str, ttl_s: int) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=ttl_s,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _delete_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _cleared_cookie_header() -> str:
    """The exact ``Set-Cookie`` value that expires the refresh cookie (for problem responses)."""
    scratch = Response()
    _delete_refresh_cookie(scratch)
    return scratch.headers["set-cookie"]


def _csrf_failed() -> ProblemError:
    return ProblemError(
        403,
        "csrf-check-failed",
        "Forbidden",
        "The request did not pass the cross-site request check.",
    )


def _check_cookie_request(
    request: Request, delivery: Literal["cookie"] | None, *, token_from_cookie: bool
) -> None:
    """CSRF layers that live in identity (D-UI-08-13; SameSite=Strict is the third).

    A token read from the cookie requires the custom ``X-Token-Delivery: cookie`` header, which a
    cross-origin script cannot send without a CORS preflight. Every request that carries the
    header, login included, must also pass the fetch-metadata check when the browser sent it.
    """
    if token_from_cookie and delivery != "cookie":
        raise _csrf_failed()
    if delivery == "cookie":
        site = request.headers.get("sec-fetch-site")
        if site is not None and site.strip().lower() not in {"same-origin", "none"}:
            raise _csrf_failed()


def _single_source(
    body_token: SecretStr | None, cookie_token: SecretStr | None
) -> tuple[str | None, bool]:
    """Return ``(raw token, came from the cookie)``; exactly one source may carry a token."""
    from_body = body_token.get_secret_value() if body_token is not None else None
    from_cookie = cookie_token.get_secret_value() if cookie_token is not None else None
    if from_cookie == "":
        from_cookie = None
    if from_body is not None and from_cookie is not None:
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "The request failed validation.",
            errors=[
                {
                    "pointer": "/refresh_token",
                    "detail": "Send the refresh token in the body or in the cookie, not both.",
                }
            ],
        )
    if from_cookie is not None:
        return from_cookie, True
    return from_body, False


async def _start_session(
    request: Request, user: User, policy: SecurityPolicy, *, access_jti: str
) -> str:
    """Issue the first refresh token of a new family (routine issuance, exempt per D-23).

    The token lives the effective idle timeout (D-39); the session start is now. In the same unit
    of work the user's oldest sessions beyond the effective ``max_sessions_per_user`` are ended
    (one ``user.sessions_evicted`` event), so a sign-in is never refused. The row records the
    device summary of the request's User-Agent (never the raw string) and the jti of the access
    token returned in the same response."""
    state = request.app.state
    uow = UnitOfWork(
        state.session_factory,
        actor=session_actor(user),
        outbox_model=state.outbox_model,
        service=state.service,
    )
    async with uow:
        await enforce_session_limit(
            uow,
            user.id,
            limit=policy.max_sessions_per_user,
            session_max_s=policy.session_max_s,
        )
        raw = await issue_refresh_token(
            uow.session,
            user,
            ttl_s=policy.idle_timeout_s,
            client_summary=summarize_user_agent(request.headers.get("user-agent")),
            access_jti=access_jti,
        )
        await uow.commit()
    return raw


async def _active_sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(
        RefreshToken.user_id == user_id,
        RefreshToken.rotated_at.is_(None),
        RefreshToken.revoked_at.is_(None),
        RefreshToken.expires_at > func.now(),
    )
    return int((await session.execute(stmt)).scalar_one())


async def _rehash(request: Request, user_id: uuid.UUID, password: str) -> None:
    """Upgrade a stored hash whose parameters drifted; an audited write like any other."""
    passwords = _passwords(request)
    state = request.app.state
    new_hash = await passwords.hash(password)
    uow = UnitOfWork(
        state.session_factory,
        actor=Actor(
            type="user",
            id=f"local:{user_id}",
            display="local user",
            via="password-login",
        ),
        outbox_model=state.outbox_model,
        service=state.service,
    )
    async with uow:
        user = (
            await uow.session.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one()
        user.password_hash = new_hash
        uow.audit(
            "user.password_rehashed",
            "user",
            user.id,
            before={"password_set": True},
            after={"password_set": True},
            scope_path=[await root_folder_id(uow.session)],
        )
        await uow.commit()


@router.post(
    "/login",
    dependencies=[public()],
    response_model_exclude_none=True,
    responses={
        200: {
            "description": "Signed in; the refresh token is in the body or, on opt-in, the cookie",
            "headers": {"Set-Cookie": _SET_COOKIE_ISSUED},
        },
        401: {"description": "Invalid credentials"},
        403: _DISABLED_OR_CSRF_RESPONSE,
        422: {"description": "Validation error"},
        503: {"description": "Bootstrap"},
    },
)
async def login(
    body: IdentityLoginIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(read_session)],
    delivery: DeliveryHeader = None,
) -> IdentityTokenOut:
    _check_cookie_request(request, delivery, token_from_cookie=False)
    settings = _settings(request)
    passwords = _passwords(request)
    keys = request.app.state.signing_keys
    username = body.username.strip().lower()
    password = body.password.get_secret_value()

    # S1 (D-37): a turned-off method is refused before the throttle, any lookup or any hashing, so
    # it is the same answer for every username and counts as no failure.
    policy = await request_policy(request)
    if not policy.local_signin:
        raise sign_in_method_disabled("Local")

    # D-04: throttle inside identity, before any user lookup or password hashing. Uvicorn resolves
    # request.client from X-Forwarded-For only for the gateway's address (compose --proxy-headers).
    throttler = LoginThrottler(request.app.state.session_factory, settings, policy)
    throttle = throttle_keys(username, request.client.host if request.client else "unknown")
    await throttler.ensure_not_locked(throttle)

    async def rejected() -> ProblemError:
        """Count the failure (own transaction, survives the 401); answer uniformly."""
        await throttler.record_failure(throttle)
        return invalid_credentials()

    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        await passwords.dummy_verify()
        raise await rejected()

    if user.password_hash is None:
        state = (
            await session.execute(select(BootstrapState).where(BootstrapState.id == 1))
        ).scalar_one_or_none()
        is_bootstrap_admin = username == settings.bootstrap_admin_username.strip().lower()
        if state is None or state.consumed_at is not None or not is_bootstrap_admin:
            await passwords.dummy_verify()
            raise await rejected()
        secret, problem = read_bootstrap_secret(settings.bootstrap_secret_file)
        if secret is None:
            _log.warning("bootstrap_login_unavailable", reason=problem)
            raise ProblemError(
                503,
                "bootstrap-unavailable",
                "Service Unavailable",
                "Bootstrap login is unavailable: the bootstrap secret has not been provisioned.",
            )
        if not await passwords.matches_secret(secret, password):
            raise await rejected()
        await throttler.reset(throttle[0])
        return _out(
            issue_access_token(
                keys,
                user=user,
                scope="password:rotate",
                ttl_s=settings.restricted_token_ttl_s,
                settings=settings,
            )
        )

    if not await passwords.verify(user.password_hash, password):
        raise await rejected()
    if passwords.needs_rehash(user.password_hash):
        await _rehash(request, user.id, password)
    await throttler.reset(throttle[0])
    jti = str(uuid_compat.uuid7())
    refresh_token = await _start_session(request, user, policy, access_jti=jti)
    issued = issue_access_token(
        keys,
        user=user,
        scope="api",
        ttl_s=settings.access_token_ttl_s,
        settings=settings,
        jti=jti,
    )
    if delivery == "cookie":
        _set_refresh_cookie(
            response, refresh_token, min(policy.idle_timeout_s, policy.session_max_s)
        )
        return _out(issued)
    return _out(issued, refresh_token)


@router.post(
    "/refresh",
    dependencies=[public()],
    response_model_exclude_none=True,
    responses={
        200: {
            "description": "Rotated; the next refresh token is in the body or, on opt-in, a cookie",
            "headers": {"Set-Cookie": _SET_COOKIE_ISSUED},
        },
        401: {
            "description": (
                "Invalid, expired or reused refresh token, or none sent (`invalid-credentials`)"
            ),
            "headers": {"Set-Cookie": _SET_COOKIE_CLEARED},
        },
        403: _DISABLED_OR_CSRF_RESPONSE,
        422: _BOTH_SOURCES_RESPONSE,
    },
)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshBody = None,
    delivery: DeliveryHeader = None,
    cookie_token: RefreshCookie = None,
) -> IdentityTokenOut:
    """Exchange a refresh token for a new access token and the next refresh token.

    The refresh token is the credential, so the route is public. It comes from the body or, for a
    browser that opted in, from the HttpOnly cookie (never both). Presenting a token that was
    already rotated revokes its whole family and answers 401.
    """
    raw, from_cookie = _single_source(body.refresh_token if body else None, cookie_token)
    _check_cookie_request(request, delivery, token_from_cookie=from_cookie)
    cookie_mode = delivery == "cookie"
    settings = _settings(request)
    if raw is None:
        raise ProblemError(
            401,
            "invalid-credentials",
            "Unauthorized",
            "Invalid or expired refresh token.",
            headers={"Set-Cookie": _cleared_cookie_header()},
        )
    policy = await request_policy(request)
    jti = str(uuid_compat.uuid7())
    try:
        if not policy.local_signin:  # S1: a refresh of a local session is refused like a login
            raise sign_in_method_disabled("Local")
        rotated = await rotate_refresh_token(
            request.app.state.session_factory,
            raw,
            settings=settings,
            idle_ttl_s=policy.idle_timeout_s,
            session_max_s=policy.session_max_s,
            access_jti=jti,
        )
    except ProblemError as exc:
        if exc.status in (401, 403) and (cookie_mode or from_cookie):
            exc.headers = {**(exc.headers or {}), "Set-Cookie": _cleared_cookie_header()}
        raise
    user, next_token = rotated.user, rotated.token
    issued = issue_access_token(
        request.app.state.signing_keys,
        user=user,
        scope="api",
        ttl_s=settings.access_token_ttl_s,
        settings=settings,
        jti=jti,
    )
    if cookie_mode:
        remaining = rotated.session_remaining_s
        max_age = (
            policy.idle_timeout_s if remaining is None else min(policy.idle_timeout_s, remaining)
        )
        _set_refresh_cookie(response, next_token, max(1, max_age))
        return _out(issued)
    return _out(issued, next_token)


@router.post(
    "/logout",
    dependencies=[public()],
    status_code=204,
    responses={
        204: {
            "description": "Session ended (also for unknown tokens and when no token was sent)",
            "headers": {"Set-Cookie": _SET_COOKIE_CLEARED},
        },
        403: _CSRF_RESPONSE,
        422: _BOTH_SOURCES_RESPONSE,
    },
)
async def logout(
    request: Request,
    body: IdentityLogoutIn | None = None,
    delivery: DeliveryHeader = None,
    cookie_token: RefreshCookie = None,
) -> Response:
    """Revoke the refresh-token family; answers 204 whether or not the token was known."""
    raw, from_cookie = _single_source(body.refresh_token if body else None, cookie_token)
    _check_cookie_request(request, delivery, token_from_cookie=from_cookie)
    if raw is not None:
        await revoke_family_for_token(
            request.app.state.session_factory, raw, settings=_settings(request)
        )
    out = Response(status_code=204)
    if delivery == "cookie" or from_cookie:
        _delete_refresh_cookie(out)
    return out


@router.get(
    "/policy",
    dependencies=[public()],
    response_model=IdentityAuthPolicyOut,
    summary="Sign-in methods and password rules (public)",
)
async def auth_policy(request: Request, response: Response) -> IdentityAuthPolicyOut:
    """The sign-in methods currently on and the rules a new password must meet.

    Public on purpose: the sign-in, rotate-password and change-password screens read it, one of
    them before anyone is signed in. It carries no secret, no tenant and no account detail.
    """
    policy = await request_policy(request)
    rules = policy.password_rules
    response.headers["Cache-Control"] = "no-store"
    return IdentityAuthPolicyOut(
        methods=IdentitySignInMethodsOut(
            local=policy.local_signin, microsoft=policy.microsoft_signin
        ),
        password_rules=IdentityPasswordRulesOut(
            min_length=rules.min_length,
            require_upper=rules.require_upper,
            require_lower=rules.require_lower,
            require_digit=rules.require_digit,
            require_symbol=rules.require_symbol,
        ),
    )


@router.post(
    "/password",
    status_code=204,
    responses={
        401: {"description": "Credentials invalid"},
        409: {"description": "Bootstrap already consumed"},
        422: {"description": "Password policy"},
    },
)
async def rotate_password(
    body: IdentityPasswordRotateIn,
    request: Request,
    principal: Annotated[Principal, require("password:rotate", allow_restricted=True)],
    uow: UnitOfWorkDep,
) -> Response:
    settings = _settings(request)
    passwords = _passwords(request)
    policy = await request_policy(request)
    try:
        user_id = uuid.UUID(principal.subject)
    except ValueError as exc:
        raise ProblemError(403, "forbidden", "Forbidden", "Not permitted.") from exc

    session = uow.session
    user = (
        await session.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    state = (
        await session.execute(
            select(BootstrapState).where(BootstrapState.id == 1).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or state is None:
        raise ProblemError(
            401,
            "unauthenticated",
            "Unauthorized",
            "Authentication required or credentials invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if principal.restricted:
        if state.consumed_at is not None:
            raise ProblemError(
                409,
                "bootstrap-consumed",
                "Conflict",
                "The bootstrap secret has already been used.",
            )
    else:
        current = body.current_password
        # WR-06: a stolen full-scope token must not be able to brute-force the current password.
        # Failed checks count on the same account and IP keys as logins; a locked key answers 429
        # before any hashing.
        throttler = LoginThrottler(request.app.state.session_factory, settings, policy)
        throttle = throttle_keys(
            user.username, request.client.host if request.client else "unknown"
        )
        await throttler.ensure_not_locked(throttle)
        if (
            current is None
            or user.password_hash is None
            or not await passwords.verify(user.password_hash, current.get_secret_value())
        ):
            if current is not None:
                await throttler.record_failure(throttle)
            raise invalid_credentials()
        await throttler.reset(throttle[0])

    secret, _ = read_bootstrap_secret(settings.bootstrap_secret_file)
    check_password(
        body.new_password.get_secret_value(),
        username=user.username,
        forbidden=[secret] if secret else [],
        rules=policy.password_rules,
    )
    new_hash = await passwords.hash(body.new_password.get_secret_value())

    before_must_rotate = user.must_rotate
    before_password_set = user.password_hash is not None
    user.password_hash = new_hash
    user.must_rotate = False
    if principal.restricted and state.consumed_at is None:
        # Only the bootstrap secret's own rotation consumes the one-time bootstrap (WR-06): a
        # full-scope principal changing its password must not disable the bootstrap login path.
        state.consumed_at = func.now()
        state.consumed_by = principal.id
    await session.flush()
    # Same transaction as the password change: no old session survives a rotation. D-23: the
    # revoke-all is recorded in this one event (active_sessions N -> 0), not as a second event.
    revoked = await revoke_all_for_user(session, user.id)
    before = {
        "must_rotate": before_must_rotate,
        "password_set": before_password_set,
        "active_sessions": revoked,
    }
    after = {
        "must_rotate": user.must_rotate,
        "password_set": True,
        "active_sessions": await _active_sessions(session, user.id),
    }
    uow.audit(
        "user.password_rotated",
        "user",
        user.id,
        before=before,
        after=after,
        scope_path=[await root_folder_id(session)],
    )
    await uow.commit()
    return Response(status_code=204)


__all__ = [
    "IdentityLoginIn",
    "IdentityLogoutIn",
    "IdentityPasswordRotateIn",
    "IdentityRefreshIn",
    "IdentityTokenOut",
    "router",
]
