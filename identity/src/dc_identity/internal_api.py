"""Internal policy decision point API: ``/internal/v1/authz/*`` (never routed by the gateway).

The principal is always the caller's verified bearer token, never anything in the body, so a
service can only ask about the user it is acting for (no confused deputy).
"""

from __future__ import annotations

from typing import Annotated

from dc_auth.authz_wire import (
    UNKNOWN_CONTEXT,
    AccessibleFoldersIn,
    AccessibleFoldersOut,
    AuthzCheckIn,
    AuthzCheckOut,
    AuthzGranteesOut,
    CheckContext,
    GranteeRef,
    MemberCheckIn,
    MemberCheckOut,
    MemberCheckResult,
    PrincipalOut,
    PrincipalResolveIn,
    PrincipalResolveOut,
)
from dc_auth.deps import authenticate, require
from dc_auth.principal import Principal
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Request

from dc_identity.access import grantee_set, grantees_for
from dc_identity.authz import LocalAuthorizer, load_folder_facts
from dc_identity.grants import confirm_entra_group, member_is_eligible
from dc_identity.principal_resolve import resolve_principals, throttle_of

router = PolicyRouter(prefix="/internal/v1/authz")


def _authorizer(request: Request, context: CheckContext | None = None) -> LocalAuthorizer:
    return LocalAuthorizer(
        request.app.state.session_factory, request.app.state.principal_gate, context=context
    )


@router.post("/check", dependencies=[require("authz:check")])
async def check(
    body: AuthzCheckIn,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> AuthzCheckOut:
    # D-173: a denial names the calling service and its route template from the validated body
    # (``unknown`` when missing or malformed, never a 422).
    context = CheckContext(
        service=body.service or UNKNOWN_CONTEXT, route=body.route or UNKNOWN_CONTEXT
    )
    decision = await _authorizer(request, context).check(
        principal, body.permission, body.folder_id, fresh=body.fresh
    )
    return AuthzCheckOut(
        outcome=decision.outcome,
        folder=decision.folder,
        permissions=sorted(decision.permissions),
    )


@router.post("/accessible-folders", dependencies=[require("authz:check")])
async def accessible_folders(
    body: AccessibleFoldersIn,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> AccessibleFoldersOut:
    scope = await _authorizer(request).accessible_folders(principal, body.permission)
    return AccessibleFoldersOut(
        all=scope.all,
        folder_ids=sorted(scope.folder_ids, key=str),
        excluded_subtrees=sorted(scope.excluded_subtrees, key=str),
    )


@router.post("/principal", dependencies=[require("authz:check")])
async def principal_of_token(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> PrincipalOut:
    """Who is the caller's bearer token? Other services forward a ``dcp_`` token here (D-71).

    Nothing is read from the body, and nothing is cached: ``authenticate`` has already looked the
    credential up in the database, and the gate re-runs the owner's liveness and Entra admission
    so a dead owner fails here with 401, 403 ``principal-not-registered`` or 503 (D-72).
    """
    await request.app.state.principal_gate.resolve(principal)
    return PrincipalOut(
        id=principal.id,
        type=principal.type,
        display=principal.display,
        issuer=principal.issuer,
        subject=principal.subject,
        via=principal.via,
        credential_id=principal.credential_id,
    )


@router.post("/grantees", dependencies=[require("authz:check")])
async def grantees(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> AuthzGranteesOut:
    """Which identities does the caller answer to: itself, its local and Entra groups (D-60).

    Nothing is read from the body (no confused deputy). The gate runs first, so a deactivated,
    unregistered or Graph-unreachable principal gets 401, 403 or 503 and never a partial set.
    """
    await request.app.state.principal_gate.resolve(principal)
    async with request.app.state.session_factory() as session:
        found = await grantee_set(session, grantees_for(principal))
    return AuthzGranteesOut(grantees=[GranteeRef(type=t, id=i) for t, i in sorted(found)])


@router.post("/members/check", dependencies=[require("authz:check")])
async def check_members(
    body: MemberCheckIn,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> MemberCheckOut:
    """May each group or service account be linked at the folder? One boolean per member (D-27).

    The same eligibility rules as a grant (Organization confinement, active registration, live
    account) plus Graph's confirmation of an Entra group (D-64). Every refusal is the same
    ``valid: false`` with no reason, so the answer is no existence oracle. The principal is the
    token's; the gate runs first, and a Graph outage is 503 for the whole call, never ``true``.
    """
    await request.app.state.principal_gate.resolve(principal)
    # Database answers first, in one short session; Graph is asked afterwards, outside any session.
    async with request.app.state.session_factory() as session:
        facts = await load_folder_facts(session, body.folder_id)
        eligible = [
            facts is not None
            and await member_is_eligible(session, principal, facts, member.type, member.id)
            for member in body.members
        ]
    results: list[MemberCheckResult] = []
    for member, valid in zip(body.members, eligible, strict=True):
        if valid and member.type == "entra_group":
            valid = await _graph_confirms(request, member.id)
        results.append(MemberCheckResult(type=member.type, id=member.id, valid=valid))
    return MemberCheckOut(results=results)


@router.post("/principals/resolve", dependencies=[require("authz:check")])
async def resolve_principal_names(
    body: PrincipalResolveIn,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> PrincipalResolveOut:
    """Do these user and group names exist? ``found``, ``not_found`` or ``unverifiable`` (D-153).

    A bounded existence check for validation's reference-integrity rules: at most 100 + 100 names,
    a per-principal throttle (429), and no attribute beyond the tri-state. The gate runs first, so
    a deactivated or unregistered caller gets 401, 403 or 503 and never an answer.
    """
    await request.app.state.principal_gate.resolve(principal)
    throttle_of(request.app).spend(principal.id, len(body.users) + len(body.groups))
    async with request.app.state.session_factory() as session:
        return await resolve_principals(
            session,
            entra_configured=bool(request.app.state.entra_tenant_ids),
            users=body.users,
            groups=body.groups,
        )


async def _graph_confirms(request: Request, object_id: str) -> bool:
    try:
        await confirm_entra_group(
            request.app.state.session_factory, request.app.state.directory, object_id
        )
    except ProblemError as exc:
        if exc.status == 422:  # Graph does not know the group: the fixed unknown-principal
            return False
        raise
    return True
