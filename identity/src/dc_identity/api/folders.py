"""Folder routes: ``/api/v1/identity/folders`` (RBAC-01, D-13 to D-16, D-35).

Folders are addressed by UUID; ``GET ?path=acme/payments/ingest`` resolves a slug path. There is
no way to move a folder or change its slug. ``DELETE /{folder_id}`` (quick 261119-0v1) removes a
folder only when nothing live is inside it, for Global admins only (``folder:delete``), after
asking contracts and validation what it still holds; ``GET /{folder_id}/deletion-check`` answers
the same question without deleting. Every route declares
``require(...)``: a caller who cannot see a folder gets 404 for it by id (D-08), and the list
endpoint answers 200 with only the folders the caller may see, an empty page and no count when
there are none (D-35). ``?path=`` and ``?parent_id=`` are filters, so an invisible match is an
empty page too.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from dc_auth.deps import UnitOfWorkDep, authenticate, current_decision, parse_bearer, require
from dc_auth.principal import Principal
from dc_auth.roles import Role, permissions_of, roles_with
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import cast as sa_cast
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from dc_identity.access import (
    effective_roles,
    grantees_for,
    inheritance_cut,
    root_id,
    visible_folders_subquery,
)
from dc_identity.folder_usage import FolderUsagePort, RemoteFolderUsage, unavailable
from dc_identity.folders import (
    SLUG_MAX_LENGTH,
    SLUG_PATTERN,
    archived_ancestor_problem,
    blockers_for,
    collect_purges,
    create_folder,
    delete_folder,
    ltree_to_slug_path,
    not_empty_problem,
    rename_folder,
    root_delete_problem,
    set_archived,
    set_block_inheritance,
    slug_path_to_ltree,
)
from dc_identity.models import Folder, Ltree
from dc_identity.text import DisplayName

GLOBAL_ADMIN = Role.GLOBAL_ADMIN.value

router = PolicyRouter(prefix="/api/v1/identity/folders")


class IdentityFolderCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: uuid.UUID
    slug: str = Field(pattern=SLUG_PATTERN, max_length=SLUG_MAX_LENGTH)
    display_name: DisplayName


class IdentityFolderPatchIn(BaseModel):
    # Required and non-nullable: FastAPI 0.151+ keeps JSON null and omitted apart, and both are
    # a 422 here. Slug, parent and kind are immutable, so extra fields are refused.
    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName


class IdentityFolderOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: str
    slug: str
    display_name: str
    path: str
    archived: bool
    archived_at: datetime | None
    block_inheritance: bool
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class IdentityBlockInheritanceIn(BaseModel):
    # Required and non-nullable: JSON null, a missing field and extra fields are all a 422.
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class IdentityBlockInheritanceOut(BaseModel):
    """What ``PUT .../block-inheritance`` returns to a caller who cannot read the folder.

    The permission is evaluated on the parent, so an admin whose grants the flag cuts below still
    gets ALLOW. They learn the state they just set and nothing else about the folder.
    """

    id: uuid.UUID
    block_inheritance: bool


class IdentityEffectiveAccessOut(BaseModel):
    folder_id: uuid.UUID
    roles: list[str]
    permissions: list[str]
    # True when a block flag on the folder or an ancestor cuts grants held above it.
    inheritance_blocked: bool
    # The blocking folder, only when the caller may read it (D-35); otherwise null even when
    # ``inheritance_blocked`` is true, because the cut can sit above the caller's own grants.
    inheritance_blocked_at: uuid.UUID | None


class IdentityFolderBlockerOut(BaseModel):
    """One reason a folder cannot be deleted yet; counts only, never names."""

    kind: Literal[
        "archived_ancestor",
        "subfolders",
        "contracts",
        "templates",
        "rule_sets",
        "subscriber_cards",
        "groups",
        "service_accounts",
        "entra_registrations",
    ]
    count: int


class IdentityFolderPurgeOut(BaseModel):
    """Leftovers that are permanently removed together with the folder; counts only."""

    kind: Literal["deleted_contracts", "archived_subscriber_cards", "unused_rule_sets"]
    count: int


class IdentityFolderDeletionCheckOut(BaseModel):
    folder_id: uuid.UUID
    deletable: bool
    blockers: list[IdentityFolderBlockerOut]
    purges: list[IdentityFolderPurgeOut]


class IdentityFolderListOut(BaseModel):
    items: list[IdentityFolderOut]
    next_cursor: str | None


def _out(folder: Folder, *, archived: bool) -> IdentityFolderOut:
    return IdentityFolderOut(
        id=folder.id,
        parent_id=folder.parent_id,
        kind=folder.kind,
        slug=folder.slug,
        display_name=folder.display_name,
        path=ltree_to_slug_path(folder.path),
        archived=archived,
        archived_at=folder.archived_at,
        block_inheritance=folder.block_inheritance,
        created_by=folder.created_by,
        created_at=folder.created_at,
        updated_by=folder.updated_by,
        updated_at=folder.updated_at,
    )


async def _create_parent_id(body: IdentityFolderCreateIn) -> uuid.UUID:
    return body.parent_id


async def _path_folder_id(folder_id: uuid.UUID) -> uuid.UUID:
    return folder_id


def _decode_cursor(cursor: str) -> uuid.UUID:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return uuid.UUID(base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii"))
    except ValueError, binascii.Error, UnicodeError:
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "The request failed validation.",
            errors=[{"pointer": "/query/cursor", "detail": "cursor is not valid"}],
        ) from None


def _encode_cursor(value: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(str(value).encode("ascii")).decode("ascii").rstrip("=")


@router.post(
    "",
    status_code=201,
    response_model=IdentityFolderOut,
    dependencies=[require("folder:create", target=_create_parent_id)],
    responses={
        404: {"description": "Parent folder not found"},
        409: {"description": "Slug already exists here, or the parent is archived"},
        422: {"description": "Validation error, or the parent is a Team folder"},
    },
)
async def create_folder_route(
    body: IdentityFolderCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> IdentityFolderOut:
    # Read inside the handler, not as a dependency: when body validation fails FastAPI still
    # resolves dependencies that do not depend on the body, and the decision does not exist yet.
    parent = current_decision(request).folder
    if parent is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise ProblemError(404, "not-found", "Not Found", "The resource was not found.")
    folder = await create_folder(uow, parent, slug=body.slug, display_name=body.display_name)
    response.headers["Location"] = f"/api/v1/identity/folders/{folder.id}"
    return _out(folder, archived=False)


@router.get(
    "",
    response_model=IdentityFolderListOut,
    dependencies=[require("folder:list")],
    responses={422: {"description": "Invalid path or cursor"}},
)
async def list_folders(
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    parent_id: Annotated[uuid.UUID | None, Query()] = None,
    path: Annotated[str | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityFolderListOut:
    ancestor = aliased(Folder)
    effectively_archived = (
        exists()
        .where(ancestor.path.op("@>", is_comparison=True)(Folder.path))
        .where(ancestor.archived_at.is_not(None))
        .correlate(Folder)
    )
    stmt = select(Folder, effectively_archived.label("effectively_archived"))

    # D-35: trim to the folders the caller may read, in SQL (no id list on the wire). A global
    # admin at ROOT sees every folder; everyone else joins the visibility subquery.
    grantees = grantees_for(principal)
    if GLOBAL_ADMIN not in await effective_roles(session, grantees, await root_id(session)):
        visible = visible_folders_subquery(grantees, roles_with("folder:read"))
        stmt = stmt.where(Folder.id.in_(select(visible.c.id)))
    if parent_id is not None:
        stmt = stmt.where(Folder.parent_id == parent_id)
    if path is not None:
        label_path = slug_path_to_ltree(path)  # validates every segment (T-01-36)
        stmt = stmt.where(Folder.path == sa_cast(label_path, Ltree))
    if not include_archived:
        stmt = stmt.where(~effectively_archived)
    if cursor is not None:
        stmt = stmt.where(Folder.id > _decode_cursor(cursor))
    stmt = stmt.order_by(Folder.id).limit(limit + 1)

    rows = (await session.execute(stmt)).all()
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1][0].id) if len(rows) > limit else None
    return IdentityFolderListOut(
        items=[_out(row[0], archived=bool(row[1])) for row in page],
        next_cursor=next_cursor,
    )


@router.get(
    "/{folder_id}",
    response_model=IdentityFolderOut,
    dependencies=[require("folder:read", target=_path_folder_id)],
    responses={404: {"description": "Folder not found"}},
)
async def get_folder(
    folder_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentityFolderOut:
    facts = current_decision(request).folder
    folder = (await session.execute(select(Folder).where(Folder.id == folder_id))).scalar_one()
    return _out(folder, archived=facts.archived if facts else False)


@router.patch(
    "/{folder_id}",
    response_model=IdentityFolderOut,
    dependencies=[require("folder:update", target=_path_folder_id)],
    responses={
        404: {"description": "Folder not found"},
        409: {"description": "Folder is archived"},
        422: {"description": "Validation error (only display_name may be changed)"},
    },
)
async def rename_folder_route(
    folder_id: uuid.UUID,
    body: IdentityFolderPatchIn,
    uow: UnitOfWorkDep,
) -> IdentityFolderOut:
    folder = await rename_folder(uow, folder_id, body.display_name)
    return _out(folder, archived=False)


@router.post(
    "/{folder_id}/archive",
    response_model=IdentityFolderOut,
    dependencies=[require("folder:archive", target=_path_folder_id)],
    responses={
        404: {"description": "Folder not found"},
        409: {"description": "An ancestor is archived"},
        422: {"description": "The ROOT folder cannot be archived"},
    },
)
async def archive_folder(folder_id: uuid.UUID, uow: UnitOfWorkDep) -> IdentityFolderOut:
    folder, _ = await set_archived(uow, folder_id, archived=True, actor_id=uow.actor.id)
    return _out(folder, archived=folder.archived_at is not None)


@router.post(
    "/{folder_id}/unarchive",
    response_model=IdentityFolderOut,
    dependencies=[require("folder:unarchive", target=_path_folder_id)],
    responses={
        404: {"description": "Folder not found"},
        409: {"description": "An ancestor is archived"},
        422: {"description": "The ROOT folder cannot be archived"},
    },
)
async def unarchive_folder(folder_id: uuid.UUID, uow: UnitOfWorkDep) -> IdentityFolderOut:
    folder, _ = await set_archived(uow, folder_id, archived=False, actor_id=uow.actor.id)
    return _out(folder, archived=False)


def _usage_port(request: Request) -> FolderUsagePort:
    port: FolderUsagePort | None = getattr(request.app.state, "folder_usage", None)
    if port is None:  # pragma: no cover - the lifespan hook always installs one
        raise unavailable()
    return port


async def _blockers_now(
    request: Request, folder_id: uuid.UUID, remote: RemoteFolderUsage
) -> list[dict[str, Any]]:
    """The blockers from a short read session (no lock is held while other services are called)."""
    async with request.app.state.session_factory() as session:
        return await blockers_for(session, folder_id, remote)


def _is_root(request: Request) -> bool:
    facts = current_decision(request).folder
    return facts is not None and facts.kind == "root"


@router.get(
    "/{folder_id}/deletion-check",
    response_model=IdentityFolderDeletionCheckOut,
    dependencies=[require("folder:delete", target=_path_folder_id)],
    responses={
        404: {"description": "Folder not found"},
        422: {"description": "The ROOT folder cannot be deleted"},
    },
)
async def check_folder_deletion(
    folder_id: uuid.UUID, request: Request
) -> IdentityFolderDeletionCheckOut:
    """What still blocks deleting the folder, and what would be removed with it. Writes nothing."""
    if _is_root(request):
        raise root_delete_problem()
    remote = await _usage_port(request).counts(folder_id, parse_bearer(request))
    blockers = await _blockers_now(request, folder_id, remote)
    return IdentityFolderDeletionCheckOut(
        folder_id=folder_id,
        deletable=not blockers,
        blockers=[IdentityFolderBlockerOut(**b) for b in blockers],
        purges=[IdentityFolderPurgeOut(**p) for p in collect_purges(remote)],
    )


@router.delete(
    "/{folder_id}",
    status_code=204,
    dependencies=[require("folder:delete", target=_path_folder_id)],
    responses={
        404: {"description": "Folder not found"},
        409: {
            "description": "The folder is not empty (a blockers list) or an ancestor is archived"
        },
        422: {"description": "The ROOT folder cannot be deleted"},
    },
)
async def delete_folder_route(
    folder_id: uuid.UUID,
    request: Request,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    """Delete an empty folder. Contracts and validation are asked first, with the caller's bearer.

    Order: usage counts (no lock held), identity-owned blockers, then the purge of leftovers
    (already soft-deleted contracts, archived cards, unused rule sets) in the other services, then
    the delete in one identity transaction. Any doubt on the remote side is 503 and nothing is
    deleted; a purge that already ran only removed things that no longer mattered.
    """
    if _is_root(request):
        raise root_delete_problem()
    port = _usage_port(request)
    bearer = parse_bearer(request)
    remote = await port.counts(folder_id, bearer)
    blockers = await _blockers_now(request, folder_id, remote)
    if any(b["kind"] == "archived_ancestor" for b in blockers):
        raise archived_ancestor_problem()
    if blockers:
        raise not_empty_problem(blockers)
    purged = await port.purge(folder_id, bearer)
    await delete_folder(uow, principal, folder_id, remote, purged)
    return Response(status_code=204)


@router.put(
    "/{folder_id}/block-inheritance",
    response_model=IdentityFolderOut | IdentityBlockInheritanceOut,
    # Evaluated on the parent (D-29): a Team admin cannot flip the flag on their own team, and the
    # admin who can clear it is never locked out by it.
    dependencies=[require("folder:set-block", target=_path_folder_id)],
    responses={
        200: {
            "description": "The full folder, or only its id and flag state when the caller "
            "cannot read the folder (the permission is evaluated on the parent)"
        },
        403: {"description": "The caller can read the folder but may not set its flag"},
        404: {"description": "Folder not found"},
        409: {"description": "The folder or an ancestor is archived"},
        422: {"description": "Only Organization and Team folders can block inheritance"},
    },
)
async def set_block_inheritance_route(
    folder_id: uuid.UUID,
    body: IdentityBlockInheritanceIn,
    request: Request,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityFolderOut | IdentityBlockInheritanceOut:
    """Set the flag. The full folder goes only to a caller who could ``GET`` it."""
    can_read = "folder:read" in current_decision(request).permissions
    folder, _ = await set_block_inheritance(uow, principal, folder_id, enabled=body.enabled)
    if not can_read:
        return IdentityBlockInheritanceOut(id=folder.id, block_inheritance=folder.block_inheritance)
    return _out(folder, archived=False)


@router.get(
    "/{folder_id}/effective-access",
    response_model=IdentityEffectiveAccessOut,
    dependencies=[require("folder:read", target=_path_folder_id)],
    responses={404: {"description": "Folder not found"}},
)
async def get_effective_access(
    folder_id: uuid.UUID,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentityEffectiveAccessOut:
    grantees = grantees_for(principal)
    held = await effective_roles(session, grantees, folder_id)
    cut = await inheritance_cut(session, folder_id)
    # The cut can sit above the caller's own grant, on a folder they cannot read (D-35): name it
    # only when they can.
    visible_cut = None
    if cut is not None:
        cut_permissions = permissions_of(await effective_roles(session, grantees, cut))
        visible_cut = cut if "folder:read" in cut_permissions else None
    return IdentityEffectiveAccessOut(
        folder_id=folder_id,
        roles=sorted(held),
        permissions=sorted(permissions_of(held)),
        inheritance_blocked=cut is not None,
        inheritance_blocked_at=visible_cut,
    )


__all__ = [
    "IdentityBlockInheritanceIn",
    "IdentityBlockInheritanceOut",
    "IdentityEffectiveAccessOut",
    "IdentityFolderBlockerOut",
    "IdentityFolderCreateIn",
    "IdentityFolderDeletionCheckOut",
    "IdentityFolderListOut",
    "IdentityFolderOut",
    "IdentityFolderPatchIn",
    "IdentityFolderPurgeOut",
    "router",
]
