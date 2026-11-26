"""RemoteAuthorizer: how every non-identity service asks identity for decisions.

The caller's bearer token is forwarded; identity derives the principal from it, never from the
request body (no confused deputy). Any failure to get a decision fails closed (503), except
identity's explicit authentication answers (AUTH-03): its 401 and its two known 403 problems
(``principal-not-registered``, ``interactive-session-required``) are answers, not outages.
"""

from collections.abc import Sequence
from uuid import UUID

import httpx
import structlog
from asgi_correlation_id import correlation_id
from fastapi import Request
from pydantic import SecretStr, ValidationError

from dc_auth.authorizer import (
    Authorizer,
    AuthorizerFactory,
    AuthorizerUnavailable,
    Decision,
    FolderScope,
)
from dc_auth.authz_wire import (
    MAX_RESOLVE_NAME_LENGTH,
    MAX_RESOLVE_NAMES,
    AccessibleFoldersIn,
    AccessibleFoldersOut,
    AuthzCheckIn,
    AuthzCheckOut,
    AuthzGranteesOut,
    CheckContext,
    GranteeRef,
    MemberCheckIn,
    MemberCheckOut,
    PrincipalResolveIn,
    PrincipalResolveOut,
    RefState,
    check_context,
    passthrough_problem,
)
from dc_auth.deps import parse_bearer
from dc_auth.issuers import AuthSettings
from dc_auth.principal import Principal
from dc_auth.verify import unauthenticated

_log = structlog.get_logger("dc_auth.remote")
_CHECK_PATH = "/internal/v1/authz/check"
_FOLDERS_PATH = "/internal/v1/authz/accessible-folders"
_GRANTEES_PATH = "/internal/v1/authz/grantees"
_MEMBERS_PATH = "/internal/v1/authz/members/check"
_RESOLVE_PATH = "/internal/v1/authz/principals/resolve"


class RemoteAuthorizer:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        bearer: SecretStr,
        timeout_s: float,
        context: CheckContext | None = None,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._bearer = bearer
        self._timeout_s = timeout_s
        self._context = context

    async def _post(self, path: str, body: str) -> bytes:
        headers = {
            "Authorization": f"Bearer {self._bearer.get_secret_value()}",
            "Content-Type": "application/json",
        }
        # D-173: identity's correlation middleware adopts the caller's id, so a denial row
        # carries the request id of the request that caused it.
        request_id = correlation_id.get()
        if request_id:
            headers["X-Request-ID"] = request_id
        try:
            response = await self._http.post(
                f"{self._base_url}{path}",
                content=body,
                headers=headers,
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as exc:
            _log.warning("authz_unreachable", path=path, error=type(exc).__name__)
            raise AuthorizerUnavailable(type(exc).__name__) from exc
        if response.status_code == 401:
            raise unauthenticated()
        problem = passthrough_problem(response.status_code, response.content)
        if problem is not None:
            raise problem
        if response.status_code != 200:
            _log.warning("authz_bad_status", path=path, status=response.status_code)
            raise AuthorizerUnavailable(f"identity answered {response.status_code}")
        return response.content

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        # `principal` is deliberately not sent: identity derives it from the forwarded token.
        context = self._context
        payload = AuthzCheckIn(
            permission=permission,
            folder_id=folder_id,
            fresh=fresh,
            service=context.service if context else None,
            route=context.route if context else None,
        )
        raw = await self._post(_CHECK_PATH, payload.model_dump_json())
        try:
            out = AuthzCheckOut.model_validate_json(raw)
        except ValidationError as exc:
            raise AuthorizerUnavailable("invalid authorization response") from exc
        return Decision(
            outcome=out.outcome, folder=out.folder, permissions=frozenset(out.permissions)
        )

    async def accessible_folders(self, principal: Principal, permission: str) -> FolderScope:
        raw = await self._post(
            _FOLDERS_PATH, AccessibleFoldersIn(permission=permission).model_dump_json()
        )
        try:
            out = AccessibleFoldersOut.model_validate_json(raw)
        except ValidationError as exc:
            raise AuthorizerUnavailable("invalid authorization response") from exc
        return FolderScope(
            all=out.all,
            folder_ids=frozenset(out.folder_ids),
            excluded_subtrees=frozenset(out.excluded_subtrees),
        )

    async def grantees(self, principal: Principal) -> frozenset[tuple[str, str]]:
        # `principal` is deliberately not sent: identity derives it from the forwarded token.
        raw = await self._post(_GRANTEES_PATH, "{}")
        try:
            out = AuthzGranteesOut.model_validate_json(raw)
        except ValidationError as exc:
            raise AuthorizerUnavailable("invalid authorization response") from exc
        return frozenset((g.type, g.id) for g in out.grantees)

    async def check_members(
        self, principal: Principal, folder_id: UUID, members: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], bool]:
        # Same rule as `check`: the principal never travels in the body.
        payload = MemberCheckIn(
            folder_id=folder_id, members=[GranteeRef(type=t, id=i) for t, i in members]
        )
        raw = await self._post(_MEMBERS_PATH, payload.model_dump_json())
        try:
            out = MemberCheckOut.model_validate_json(raw)
        except ValidationError as exc:
            raise AuthorizerUnavailable("invalid authorization response") from exc
        answered = {(r.type, r.id): r.valid for r in out.results}
        if any(member not in answered for member in members):
            raise AuthorizerUnavailable("incomplete authorization response")
        return {member: answered[member] for member in members}

    async def resolve_principals(
        self, principal: Principal, users: Sequence[str], groups: Sequence[str]
    ) -> PrincipalResolveOut:
        """Does each user and group name exist? Never raises for an identity problem (D-153).

        A timeout, connection error, 429, 5xx or unreadable answer leaves every name
        ``unverifiable``, because only ``not_found`` may fail a rule. Identity's own 401 and its
        known 403 problems are answers about the caller and still propagate. Names are sent in
        chunks of the route's cap; a name longer than the route accepts is ``unverifiable``.
        """
        # `principal` is deliberately not sent: identity derives it from the forwarded token.
        out = PrincipalResolveOut()
        sendable_users = _sendable(users, out.users)
        sendable_groups = _sendable(groups, out.groups)
        step = MAX_RESOLVE_NAMES
        for start in range(0, max(len(sendable_users), len(sendable_groups)), step):
            chunk_users = sendable_users[start : start + step]
            chunk_groups = sendable_groups[start : start + step]
            payload = PrincipalResolveIn(users=chunk_users, groups=chunk_groups)
            answer: PrincipalResolveOut | None = None
            try:
                raw = await self._post(_RESOLVE_PATH, payload.model_dump_json())
                answer = PrincipalResolveOut.model_validate_json(raw)
            except AuthorizerUnavailable, ValidationError:
                _log.warning("principal_resolve_unverifiable")
            for names, answered, target in (
                (chunk_users, answer.users if answer else {}, out.users),
                (chunk_groups, answer.groups if answer else {}, out.groups),
            ):
                for name in names:
                    target[name] = answered.get(name, "unverifiable")
        return out


def _sendable(names: Sequence[str], target: dict[str, RefState]) -> list[str]:
    """Distinct names the route accepts; the rest are recorded as unverifiable in ``target``."""
    seen: dict[str, None] = {}
    for name in names:
        if not name or len(name) > MAX_RESOLVE_NAME_LENGTH:
            target[name] = "unverifiable"
        else:
            seen[name] = None
    return list(seen)


def remote_authorizer_factory(settings: AuthSettings) -> AuthorizerFactory:
    """Factory used with ``auth_lifespan``: one RemoteAuthorizer per request, bound to its token."""

    def factory(request: Request) -> Authorizer:
        token = parse_bearer(request)
        return RemoteAuthorizer(
            request.app.state.http,
            base_url=settings.identity_internal_url,
            bearer=SecretStr(token),
            timeout_s=settings.authz_timeout_s,
            context=check_context(request),
        )

    return factory
