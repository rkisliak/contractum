"""Display names for principal ids: ``POST /api/v1/identity/principals/names``.

Every resource records who changed it as a principal id (``created_by``, ``published_by``,
``updated_by``: ``local:<uuid>``, ``entra:<tenant>:<object>`` or ``sa:<uuid>``). A person reading
that resource sees the id unless something turns it into a name, and only a Global admin may list
users. This route answers the display name, and nothing else, for at most 100 ids at a time.

* Gated by the principal-level ``principal:read-names``, which every non-restricted principal
  holds (like ``me:read``); a restricted bootstrap token gets 403 ``password-rotation-required``.
* It answers only ids it knows: a local user, a service account, a just-in-time Entra user or a
  registered Entra user or service principal. An unknown, malformed or other-namespace id is
  simply absent from ``items``, so the answer never distinguishes "unknown" from "not ours".
* Only ``display_name`` leaves identity: no username, email, state or folder. Principal ids are
  random UUIDs that the caller already holds from a resource it was allowed to read.
* Reads are not audited.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from dc_auth.deps import require
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from fastapi import Depends
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.models import EntraRegistration, EntraUser, ServiceAccount, User
from dc_identity.principal_ids import (
    entra_principal_id,
    local_principal_id,
    parse_principal_id,
    service_account_principal_id,
)

router = PolicyRouter(prefix="/api/v1/identity/principals")

MAX_IDS = 100

PrincipalIdText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class IdentityPrincipalNamesIn(BaseModel):
    ids: list[PrincipalIdText] = Field(min_length=1, max_length=MAX_IDS)


class IdentityPrincipalNameOut(BaseModel):
    id: str
    display_name: str


class IdentityPrincipalNamesOut(BaseModel):
    items: list[IdentityPrincipalNameOut]


@router.post(
    "/names",
    dependencies=[require("principal:read-names")],
    response_model=IdentityPrincipalNamesOut,
    status_code=200,
    summary="Display names for principal ids",
)
async def principal_names(
    body: IdentityPrincipalNamesIn,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentityPrincipalNamesOut:
    local_ids: set[uuid.UUID] = set()
    account_ids: set[uuid.UUID] = set()
    entra_pairs: set[tuple[str, str]] = set()
    for raw in dict.fromkeys(body.ids):
        ref = parse_principal_id(raw)
        if ref is None:
            continue
        if ref.local_id is not None:
            local_ids.add(ref.local_id)
        elif ref.service_account_id is not None:
            account_ids.add(ref.service_account_id)
        elif ref.tenant_id is not None and ref.object_id is not None:
            entra_pairs.add((ref.tenant_id, ref.object_id))

    names: dict[str, str] = {}
    if local_ids:
        rows = await session.execute(
            select(User.id, User.display_name).where(User.id.in_(local_ids))
        )
        for user_id, name in rows:
            names[local_principal_id(user_id)] = name
    if account_ids:
        rows = await session.execute(
            select(ServiceAccount.id, ServiceAccount.name).where(ServiceAccount.id.in_(account_ids))
        )
        for account_id, name in rows:
            names[service_account_principal_id(account_id)] = name
    if entra_pairs:
        registered = await session.execute(
            select(
                EntraRegistration.tenant_id,
                EntraRegistration.object_id,
                EntraRegistration.display_name,
            ).where(
                EntraRegistration.kind.in_(("user", "service_principal")),
                tuple_(EntraRegistration.tenant_id, EntraRegistration.object_id).in_(entra_pairs),
            )
        )
        for tenant_id, object_id, name in registered:
            names[entra_principal_id(tenant_id, object_id)] = name
        # The just-in-time row carries the name Entra last reported; it wins over a registration.
        seen = await session.execute(
            select(EntraUser.tenant_id, EntraUser.object_id, EntraUser.display_name).where(
                tuple_(EntraUser.tenant_id, EntraUser.object_id).in_(entra_pairs)
            )
        )
        for tenant_id, object_id, name in seen:
            names[entra_principal_id(tenant_id, object_id)] = name

    return IdentityPrincipalNamesOut(
        items=[
            IdentityPrincipalNameOut(id=pid, display_name=name)
            for pid, name in sorted(names.items())
        ]
    )
