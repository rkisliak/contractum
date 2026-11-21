"""Internal audit read: ``GET /internal/v1/events`` (never routed by the gateway, not in OpenAPI).

Declared ``require("audit:read")``: identity's PDP allows it for global admins only. The e2e suite
uses it to prove delivery black-box. It returns ``change`` rows unless ``category`` asks for
``access`` or ``denial``: existing journeys pin exact lists of change events per aggregate. The
public query (``dc_audit.api``) is the supported way to read events; this read stays for
compose-network polling. Both return ``dc_audit.dto.AuditEventOut``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from dc_auth.deps import require
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from fastapi import Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_audit.dto import AuditEventOut
from dc_audit.models import AuditEvent

router = PolicyRouter(prefix="/internal/v1")


@router.get("/events", dependencies=[require("audit:read")])
async def list_events(
    session: Annotated[AsyncSession, Depends(read_session)],
    aggregate_id: Annotated[str | None, Query()] = None,
    service: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    category: Annotated[Literal["change", "access", "denial"], Query()] = "change",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditEventOut]:
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.category == category)
        .order_by(AuditEvent.id)
        .limit(limit)
    )
    if aggregate_id is not None:
        stmt = stmt.where(AuditEvent.aggregate_id == aggregate_id)
    if service is not None:
        stmt = stmt.where(AuditEvent.service == service)
    if action is not None:
        stmt = stmt.where(AuditEvent.action == action)
    rows = (await session.execute(stmt)).scalars().all()
    return [AuditEventOut.model_validate(row) for row in rows]
