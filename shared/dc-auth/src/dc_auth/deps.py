"""FastAPI dependencies: authenticate, require (route policy), unit_of_work, auth lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

import httpx
import structlog
from asgi_correlation_id import correlation_id
from dc_core.app import LifespanHook
from dc_core.http import make_client
from dc_core.policy import PolicyMeta, mark_policy
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from fastapi import Depends, FastAPI, Request

from dc_auth.authorizer import (
    AuthorizerFactory,
    AuthorizerUnavailable,
    Decision,
    Outcome,
)
from dc_auth.issuers import AuthSettings, IssuerRegistry
from dc_auth.jwks import JwksCache
from dc_auth.opaque import OpaqueTokenResolver, RemoteOpaqueResolver
from dc_auth.principal import Principal, principal_to_actor
from dc_auth.verify import TokenVerifier, unauthenticated


def parse_bearer(request: Request) -> str:
    """The bearer token of a request (sync so non-async factories can share the parsing)."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise unauthenticated()
    return token


async def bearer_token(request: Request) -> str:
    return parse_bearer(request)


async def authenticate(request: Request) -> Principal:
    token = await bearer_token(request)
    principal: Principal = await request.app.state.verifier.verify(token)
    request.state.principal = principal
    structlog.contextvars.bind_contextvars(principal_id=principal.id)
    return principal


async def _no_target() -> UUID | None:
    return None


def require(
    permission: str,
    *,
    target: Any = None,
    allow_restricted: bool = False,
) -> Any:
    """Declare the route policy: authenticated, then allowed by the Authorizer.

    ``target`` is an optional FastAPI dependency returning the folder id the permission is
    checked against (``UUID | None``). The Authorizer decides 404 versus 403 (D-08).
    """
    target_dep = target or _no_target

    async def _policy(
        request: Request,
        principal: Annotated[Principal, Depends(authenticate)],
        folder_id: Annotated[UUID | None, Depends(target_dep)],
    ) -> Principal:
        if principal.restricted and not allow_restricted:
            raise ProblemError(
                403,
                "password-rotation-required",
                "Forbidden",
                "The password must be rotated before this resource can be used.",
            )
        authz = request.app.state.authorizer_factory(request)
        fresh = request.method not in ("GET", "HEAD", "OPTIONS")
        try:
            decision = await authz.check(principal, permission, folder_id, fresh=fresh)
        except AuthorizerUnavailable as exc:
            raise ProblemError(
                503,
                "service-unavailable",
                "Service Unavailable",
                "Authorization is temporarily unavailable.",
            ) from exc
        if decision.outcome is Outcome.NOT_FOUND:
            raise ProblemError(404, "not-found", "Not Found", "The resource was not found.")
        if decision.outcome is not Outcome.ALLOW:
            raise ProblemError(
                403, "forbidden", "Forbidden", "You are not permitted to perform this action."
            )
        request.state.decision = decision
        return principal

    mark_policy(_policy, PolicyMeta(permission, allow_restricted=allow_restricted))
    return Depends(_policy)


def current_decision(request: Request) -> Decision:
    decision: Decision = request.state.decision
    return decision


async def recorded_refusal(
    request: Request,
    permission: str,
    folder_id: UUID | None = None,
    *,
    principal: Principal | None = None,
) -> ProblemError:
    """The uniform 404 for a refusal derived from a decision's permissions, once it is recorded.

    A handler that refuses because a ``Decision.permissions`` set lacks ``permission`` (a
    published-only reader asking for a draft, an unpublished version or a draft dry-run) would
    otherwise refuse without any PDP check failing, so no denial would be written (AUD-04, D-172).
    This asks the Authorizer for ``permission`` at ``folder_id`` (default: the folder of the
    route's own decision) so the PDP records the denial with the real permission, and then
    returns the problem to raise. The answer is always the uniform 404, whatever the second
    check says (a grant that appeared between the two checks does not reverse the first
    decision, and a ``visible_via`` 403 would tell a probing caller more than the route does);
    an Authorizer that cannot answer (including a denial it could not record) is 503, fail
    closed, like every other check.
    """
    if principal is None:
        principal = request.state.principal
    if folder_id is None:
        facts = current_decision(request).folder
        folder_id = facts.id if facts is not None else None
    authz = request.app.state.authorizer_factory(request)
    try:
        await authz.check(principal, permission, folder_id)
    except AuthorizerUnavailable:
        return ProblemError(
            503,
            "service-unavailable",
            "Service Unavailable",
            "Authorization is temporarily unavailable.",
        )
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


async def unit_of_work(
    request: Request, principal: Annotated[Principal, Depends(authenticate)]
) -> AsyncIterator[UnitOfWork]:
    state = request.app.state
    uow = UnitOfWork(
        state.session_factory,
        actor=principal_to_actor(principal),
        outbox_model=state.outbox_model,
        service=state.service,
        request_id=correlation_id.get(),
    )
    async with uow:
        yield uow
        # Reached only when the handler did not raise: commit what it left pending, so a
        # forgotten commit() is not a silent no-op (a forgotten audit() trips the tripwire).
        await uow.commit_if_pending()


# Function scope (WR-02): the dependency's exit code, which commits or trips the audit tripwire,
# must run before the response is sent. With FastAPI's default request scope it runs after the
# response, so a handler that forgot commit() would already have answered 2xx.
UnitOfWorkDep = Annotated[UnitOfWork, Depends(unit_of_work, scope="function")]


def auth_lifespan(
    settings: AuthSettings,
    *,
    authorizer_factory: AuthorizerFactory,
    http_client: httpx.AsyncClient | None = None,
    opaque_resolver: OpaqueTokenResolver | None = None,
) -> LifespanHook:
    """Lifespan hook wiring verifier, JWKS cache, HTTP client and authorizer onto app.state.

    An injected ``http_client`` serves JWKS fetches and remote authorization and is not closed
    here; otherwise one is created with ``make_client()`` and closed on exit. Opaque ``dcp_``
    tokens go to ``opaque_resolver``; by default identity is asked on every request (D-71, D-72).
    """

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        owned = http_client is None
        http = http_client if http_client is not None else make_client()
        try:
            app.state.http = http
            app.state.auth_settings = settings
            opaque = opaque_resolver or RemoteOpaqueResolver(
                http,
                base_url=settings.identity_internal_url,
                timeout_s=settings.authz_timeout_s,
            )
            app.state.verifier = TokenVerifier(
                IssuerRegistry(settings.issuers), JwksCache(http), opaque=opaque
            )
            app.state.authorizer_factory = authorizer_factory
            yield
        finally:
            if owned:
                await http.aclose()

    return _hook
