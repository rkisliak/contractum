"""Public audit query: ``GET /api/v1/audit/events`` and ``/events/{event_id}`` (AUD-05).

D-166..D-169.

The route gate is the principal-level ``audit:list`` (ALLOW for any non-restricted principal, so a
PAT, a read-only PAT or a service-account key may query, API-04). The rows are trimmed in SQL to
identity's ``accessible_folders(principal, "audit:query")``: a caller who can see nothing gets an
empty 200 page, never a 403 (D-35). An identity outage is 503, never an unfiltered page. Bad
query parameters (a NUL, a naive time, ``from`` after ``to``, a forged cursor) are refused with
422 ``validation-error`` before identity is asked.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from dc_auth.authorizer import AuthorizerUnavailable, FolderScope
from dc_auth.deps import authenticate, require
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_audit.dto import AuditEventOut, AuditEventPageOut
from dc_audit.models import AuditEvent
from dc_audit.query import (
    DEFAULT_LIMIT,
    MAX_CURSOR_LENGTH,
    MAX_LIMIT,
    MAX_TEXT_LENGTH,
    Category,
    build_statement,
    decode_cursor,
    make_filters,
    next_cursor,
    visible,
)

router = PolicyRouter(prefix="/api/v1/audit")

QUERY_PERMISSION = "audit:query"


async def _scope(request: Request, principal: Principal) -> FolderScope:
    """The caller's ``audit:query`` scope from identity; an outage fails closed (503)."""
    authorizer = request.app.state.authorizer_factory(request)
    try:
        scope: FolderScope = await authorizer.accessible_folders(principal, QUERY_PERMISSION)
    except AuthorizerUnavailable as exc:
        raise ProblemError(
            503,
            "service-unavailable",
            "Service Unavailable",
            "Authorization is temporarily unavailable.",
        ) from exc
    return scope


@router.get(
    "/events",
    response_model=AuditEventPageOut,
    dependencies=[require("audit:list")],
    summary="Query audit events",
)
async def list_audit_events(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    aggregate_type: Annotated[
        str | None, Query(max_length=MAX_TEXT_LENGTH, description="Object type, e.g. contract")
    ] = None,
    aggregate_id: Annotated[
        str | None, Query(max_length=MAX_TEXT_LENGTH, description="Object id")
    ] = None,
    actor_id: Annotated[
        str | None,
        Query(max_length=MAX_TEXT_LENGTH, description="Actor id, e.g. a user id or sa:<uuid>"),
    ] = None,
    service: Annotated[
        str | None, Query(max_length=MAX_TEXT_LENGTH, description="Emitting service")
    ] = None,
    action: Annotated[
        str | None,
        Query(
            max_length=MAX_TEXT_LENGTH,
            description="Exact action, or a dotted prefix when it ends with a dot",
        ),
    ] = None,
    category: Annotated[Category | None, Query(description="change, access or denial")] = None,
    from_: Annotated[
        datetime | None,
        Query(alias="from", description="Inclusive lower bound on occurred_at (RFC 3339)"),
    ] = None,
    to: Annotated[
        datetime | None, Query(description="Exclusive upper bound on occurred_at (RFC 3339)")
    ] = None,
    cursor: Annotated[
        str | None, Query(max_length=MAX_CURSOR_LENGTH, description="next_cursor of a page")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> AuditEventPageOut:
    """Audit events in folders the caller may see, newest first, filtered and keyset paged."""
    filters = make_filters(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        actor_id=actor_id,
        service=service,
        action=action,
        category=category,
        from_=from_,
        to=to,
    )
    after = decode_cursor(cursor) if cursor is not None else None
    predicate = visible(await _scope(request, principal))
    if predicate is None:
        return AuditEventPageOut(items=[], next_cursor=None)
    stmt = build_statement(predicate, filters, limit, cursor=after)
    rows = list((await session.execute(stmt)).scalars().all())
    return AuditEventPageOut(
        items=[AuditEventOut.model_validate(row) for row in rows[:limit]],
        next_cursor=next_cursor(rows, limit),
    )


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


@router.get(
    "/events/{event_id}",
    response_model=AuditEventOut,
    dependencies=[require("audit:list")],
    summary="Get one audit event",
    responses={404: {"description": "Unknown event, or an event the caller may not see"}},
)
async def get_audit_event(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    event_id: uuid.UUID,
) -> AuditEventOut:
    """One audit event by its ``event_id``; an unknown and an invisible event are the same 404."""
    predicate = visible(await _scope(request, principal))
    if predicate is None:
        raise _not_found()
    # The visibility predicate and the id are one statement: no existence oracle (T-7-15).
    stmt = select(AuditEvent).where(predicate, AuditEvent.event_id == event_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found()
    return AuditEventOut.model_validate(row)
