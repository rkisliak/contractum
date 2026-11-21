"""Wire models for the internal authorization API (``/internal/v1/authz/*``)."""

import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from dc_core.problems import ProblemError
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from dc_auth.authorizer import FolderFacts, Outcome
from dc_auth.principal import PrincipalType, PrincipalVia

# Identity's explicit authentication answers that are not outages (AUTH-03). Anything else it
# answers with a non-200 status stays a fail-closed 503. Details are ours, never identity's.
PASSTHROUGH_403_SLUGS: frozenset[str] = frozenset(
    {"principal-not-registered", "interactive-session-required"}
)
_PASSTHROUGH_DETAILS: dict[str, str] = {
    "principal-not-registered": "This principal is not registered for access.",
    "interactive-session-required": "An interactive sign-in is required for this action.",
}
_PROBLEM_PREFIX = "urn:dc:problem:"


def passthrough_problem(status_code: int, body: bytes) -> ProblemError | None:
    """The ProblemError for one of identity's known 403 answers, else None.

    Leaf helper (no import of verify, deps or remote) so every caller of identity can reuse it.
    """
    if status_code != 403:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    problem_type = payload.get("type")
    if not isinstance(problem_type, str) or not problem_type.startswith(_PROBLEM_PREFIX):
        return None
    slug = problem_type[len(_PROBLEM_PREFIX) :]
    if slug not in PASSTHROUGH_403_SLUGS:
        return None
    return ProblemError(403, slug, "Forbidden", _PASSTHROUGH_DETAILS[slug])


UNKNOWN_CONTEXT = "unknown"
# Caller-supplied context is advisory (the verified token's principal is authoritative), so it is
# held to strict shapes and caps: a service name, and a route template under a routed prefix.
_SERVICE_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
_ROUTE_PREFIXES = ("/api/v1/", "/internal/")
MAX_ROUTE_LENGTH = 256


@dataclass(frozen=True)
class CheckContext:
    """Who asked for a check (D-173): the asking service and the route template, never a URL."""

    service: str
    route: str


def check_context(request: Request) -> CheckContext:
    """The context of a check made while serving ``request``.

    The service is ``app.state.service``; the route is the matched route's template
    (``/api/v1/x/things/{thing_id}``), so a raw URL or query string is never recorded. Either is
    ``unknown`` when absent.
    """
    service = getattr(request.app.state, "service", None)
    route = getattr(request.scope.get("route"), "path_format", None)
    return CheckContext(
        service=service if isinstance(service, str) and service else UNKNOWN_CONTEXT,
        route=route if isinstance(route, str) and route else UNKNOWN_CONTEXT,
    )


def _valid_route(value: str) -> bool:
    return (
        len(value) <= MAX_ROUTE_LENGTH
        and value.startswith(_ROUTE_PREFIXES)
        and value.isprintable()
        and not any(char in value for char in "?# ")
    )


class AuthzCheckIn(BaseModel):
    permission: str
    folder_id: UUID | None = None
    fresh: bool = False
    # D-173: the asking service and route template, recorded on a denial. Lenient on purpose: an
    # invalid value becomes None (recorded as ``unknown``) and never fails the check with a 422.
    service: str | None = None
    route: str | None = None

    @field_validator("service", mode="before")
    @classmethod
    def _lenient_service(cls, value: Any) -> str | None:
        if isinstance(value, str) and _SERVICE_NAME.fullmatch(value):
            return value
        return None

    @field_validator("route", mode="before")
    @classmethod
    def _lenient_route(cls, value: Any) -> str | None:
        if isinstance(value, str) and _valid_route(value):
            return value
        return None


class AuthzCheckOut(BaseModel):
    outcome: Outcome
    folder: FolderFacts | None = None
    permissions: list[str] = []


class AccessibleFoldersIn(BaseModel):
    permission: str


class AccessibleFoldersOut(BaseModel):
    all: bool
    folder_ids: list[UUID] = []
    excluded_subtrees: list[UUID] = []


class PrincipalOut(BaseModel):
    """Identity's answer for a forwarded opaque token (``POST /internal/v1/authz/principal``)."""

    id: str
    type: PrincipalType
    display: str
    issuer: str
    subject: str
    via: PrincipalVia
    credential_id: str | None = None


# Member kinds a subscriber card may link (A3): no individual users, no Entra service principals.
MEMBER_TYPES: frozenset[str] = frozenset({"local_group", "entra_group", "service_account"})
MAX_MEMBERS_PER_CHECK = 20


class GranteeRef(BaseModel):
    """One ``(type, id)`` identity a caller answers to: itself, a local group or an Entra group."""

    type: str
    id: str


class AuthzGranteesOut(BaseModel):
    """Identity's answer for ``POST /internal/v1/authz/grantees`` (no request body)."""

    grantees: list[GranteeRef]


class MemberCheckIn(BaseModel):
    """``POST /internal/v1/authz/members/check``: may these members be linked at this folder?"""

    model_config = ConfigDict(extra="forbid")

    folder_id: UUID
    members: list[GranteeRef] = Field(min_length=1, max_length=MAX_MEMBERS_PER_CHECK)


class MemberCheckResult(BaseModel):
    """One answer per requested member; ``valid`` is the only output (no reason, no hint)."""

    type: str
    id: str
    valid: bool


class MemberCheckOut(BaseModel):
    results: list[MemberCheckResult]


# Principal existence check (D-153): a bounded, tri-state answer and nothing else, so the route
# stays an existence check and never becomes a directory read.
MAX_RESOLVE_NAMES = 100
MAX_RESOLVE_NAME_LENGTH = 256
RefState = Literal["found", "not_found", "unverifiable"]
ResolveName = Annotated[str, StringConstraints(min_length=1, max_length=MAX_RESOLVE_NAME_LENGTH)]


class PrincipalResolveIn(BaseModel):
    """``POST /internal/v1/authz/principals/resolve``: do these users and groups exist?"""

    model_config = ConfigDict(extra="forbid")

    users: list[ResolveName] = Field(default_factory=list, max_length=MAX_RESOLVE_NAMES)
    groups: list[ResolveName] = Field(default_factory=list, max_length=MAX_RESOLVE_NAMES)


class PrincipalResolveOut(BaseModel):
    """One state per submitted name, keyed by the name exactly as submitted."""

    users: dict[str, RefState] = Field(default_factory=dict)
    groups: dict[str, RefState] = Field(default_factory=dict)
