"""Folder domain service: kind derivation, ltree paths, rename, archive semantics (RBAC-01).

Decisions D-13 to D-16. Folders form a fixed four-level tree (ROOT -> Organization -> Project ->
Team). Slugs are immutable lowercase ``a-z0-9`` with single hyphens (at most 63 characters) and
are used unchanged as ltree labels (PostgreSQL ltree accepts hyphens), so the slug path
``acme/payments-eu`` maps to the ltree ``root.acme.payments-eu``. Folders are never moved; only a
Global admin deletes one, and only when nothing live is inside it (``delete_folder``, quick
261119-0v1; ROOT never). Archiving makes the node and its whole subtree read-only.

Every write goes through the caller's ``UnitOfWork`` and records exactly one audit event.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dc_auth.authorizer import FolderFacts
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from dc_identity.folder_usage import RemoteFolderUsage, RemotePurge
from dc_identity.guards import lock_authz_writes, require_permission
from dc_identity.models import EntraRegistration, Folder, LocalGroup, RoleAssignment, ServiceAccount

if TYPE_CHECKING:
    from dc_auth.principal import Principal
    from sqlalchemy.ext.asyncio import AsyncSession

SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
SLUG_MAX_LENGTH = 63
ROOT_LABEL = "root"

_SLUG_RE = re.compile(SLUG_PATTERN)
_CHILD_KIND = {"root": "organization", "organization": "project", "project": "team"}

# The unique violation of "same slug under the same parent" surfaces under either unique index
# that rejects it (parent+slug, or the derived path), depending on which one Postgres checks
# first; both mean the same thing to the client. Names come from the naming convention in
# dc_core.db (``uq_<table>_<first column>``); the descriptive spelling is kept for safety.
_DUPLICATE_CONSTRAINTS = frozenset(
    {"uq_folder_parent_id_slug", "uq_folder_parent_id", "uq_folder_path"}
)


def _invalid(pointer: str, detail: str) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Unprocessable Content",
        "The request failed validation.",
        errors=[{"pointer": pointer, "detail": detail}],
    )


def _archived_problem(detail: str) -> ProblemError:
    return ProblemError(409, "folder-archived", "Conflict", detail)


def is_valid_slug(slug: str) -> bool:
    return len(slug) <= SLUG_MAX_LENGTH and _SLUG_RE.fullmatch(slug) is not None


def child_kind(parent_kind: str) -> str:
    """Kind of a new child, derived from the parent's depth (D-16)."""
    try:
        return _CHILD_KIND[parent_kind]
    except KeyError:
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "Team folders cannot contain folders",
        ) from None


def slug_path_to_ltree(path: str) -> str:
    """``acme/payments-eu`` -> ``root.acme.payments-eu``; every segment validated, no mapping."""
    segments = path.split("/")
    for segment in segments:
        if not segment:
            raise _invalid("/query/path", "path must not be empty or contain empty segments")
        if not is_valid_slug(segment):
            raise _invalid(
                "/query/path",
                "each path segment must be a lowercase slug of at most 63 characters",
            )
    return ".".join([ROOT_LABEL, *segments])


def ltree_to_slug_path(path: str) -> str:
    """Exact inverse of :func:`slug_path_to_ltree`; ``root`` alone is the empty path."""
    labels = path.split(".")
    if labels and labels[0] == ROOT_LABEL:
        labels = labels[1:]
    return "/".join(labels)


async def _chain(
    session: AsyncSession, folder_id: uuid.UUID
) -> list[tuple[uuid.UUID, datetime | None]]:
    """Ancestors-or-self as (id, archived_at), root first; empty when the folder is unknown."""
    target = aliased(Folder)
    ancestor = aliased(Folder)
    stmt = (
        select(ancestor.id, ancestor.archived_at)
        .join(target, ancestor.path.op("@>")(target.path))
        .where(target.id == folder_id)
        .order_by(func.nlevel(ancestor.path))
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def _locked(session: AsyncSession, folder_id: uuid.UUID) -> Folder:
    folder = (
        await session.execute(select(Folder).where(Folder.id == folder_id).with_for_update())
    ).scalar_one_or_none()
    if folder is None:
        raise ProblemError(404, "not-found", "Not Found", "The resource was not found.")
    return folder


def _snapshot(folder: Folder) -> dict[str, Any]:
    return UnitOfWork.snapshot(folder)


async def create_folder(
    uow: UnitOfWork, parent: FolderFacts, *, slug: str, display_name: str
) -> Folder:
    """Create a child of ``parent``; kind from depth, path from the parent's ltree."""
    if parent.archived:
        raise _archived_problem("The parent folder is archived; its subtree is read-only.")
    kind = child_kind(parent.kind)
    session = uow.session
    folder = Folder(
        parent_id=parent.id,
        kind=kind,
        slug=slug,
        display_name=display_name,
        # The slug is the ltree label unchanged (hyphens included). It is a bound parameter,
        # never formatted into SQL, and was validated against SLUG_PATTERN.
        path=f"{parent.path}.{slug}",
    )
    # No SAVEPOINT: releasing a nested transaction fires the audit tripwire's before_commit
    # hook before the outbox row exists. A failed flush aborts the whole unit of work anyway
    # (the 409 below makes UnitOfWork roll back), so there is nothing to preserve.
    session.add(folder)
    try:
        await session.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint in _DUPLICATE_CONSTRAINTS:
            raise ProblemError(
                409, "conflict", "Conflict", "A folder with this slug already exists here"
            ) from exc
        raise
    await session.refresh(folder)
    uow.audit(
        "folder.created",
        "folder",
        folder.id,
        before=None,
        after=_snapshot(folder),
        scope_path=(*parent.ancestor_ids, folder.id),
    )
    await uow.commit()
    return folder


async def rename_folder(uow: UnitOfWork, folder_id: uuid.UUID, display_name: str) -> Folder:
    """Change the display name only; a no-op (no event) when unchanged."""
    session = uow.session
    folder = await _locked(session, folder_id)
    chain = await _chain(session, folder_id)
    if any(archived_at is not None for _, archived_at in chain):
        raise _archived_problem("The folder is archived; its subtree is read-only.")
    if folder.display_name == display_name:
        return folder
    before = _snapshot(folder)
    folder.display_name = display_name
    await session.flush()
    await session.refresh(folder)
    uow.audit(
        "folder.renamed",
        "folder",
        folder.id,
        before=before,
        after=_snapshot(folder),
        scope_path=tuple(fid for fid, _ in chain),
    )
    await uow.commit()
    return folder


async def set_archived(
    uow: UnitOfWork, folder_id: uuid.UUID, *, archived: bool, actor_id: str
) -> tuple[Folder, bool]:
    """Archive or unarchive one node (not its descendants); returns (folder, changed)."""
    session = uow.session
    folder = await _locked(session, folder_id)
    if folder.kind == "root":
        raise ProblemError(
            422, "validation-error", "Unprocessable Content", "The ROOT folder cannot be archived."
        )
    chain = await _chain(session, folder_id)
    if any(archived_at is not None for fid, archived_at in chain if fid != folder.id):
        raise _archived_problem(
            "An ancestor folder is archived; this subtree stays read-only until it is unarchived."
        )
    if (folder.archived_at is not None) == archived:
        return folder, False
    before = _snapshot(folder)
    if archived:
        folder.archived_at = func.now()
        folder.archived_by = actor_id
    else:
        folder.archived_at = None
        folder.archived_by = None
    await session.flush()
    await session.refresh(folder)
    uow.audit(
        "folder.archived" if archived else "folder.unarchived",
        "folder",
        folder.id,
        before=before,
        after=_snapshot(folder),
        scope_path=tuple(fid for fid, _ in chain),
    )
    await uow.commit()
    return folder, True


async def set_block_inheritance(
    uow: UnitOfWork, actor: Principal, folder_id: uuid.UUID, *, enabled: bool
) -> tuple[Folder, bool]:
    """Set or clear the block-inheritance flag (D-29); returns (folder, changed).

    The flag only changes which grants apply (the access evaluator implements the cut); this
    function records the state change. Only Organization and Team folders may carry it. A write
    that leaves the flag unchanged records nothing. Takes the authorization-write lock first so
    it serialises with every other authorization write, then re-checks ``folder:set-block`` at
    the parent (where the gate evaluates it) so a grant revoked after the gate cannot be used to
    cut every inherited grant below.
    """
    session = uow.session
    await lock_authz_writes(session)
    folder = await _locked(session, folder_id)
    if folder.kind not in ("organization", "team"):
        raise ProblemError(
            422,
            "block-inheritance-not-allowed",
            "Unprocessable Content",
            "Only Organization and Team folders can block inheritance.",
        )
    chain = await _chain(session, folder_id)
    await require_permission(session, actor, chain[-2][0], "folder:set-block")
    if any(archived_at is not None for _, archived_at in chain):
        raise _archived_problem("The folder is archived; its subtree is read-only.")
    if folder.block_inheritance == enabled:
        return folder, False
    before = _snapshot(folder)
    folder.block_inheritance = enabled
    await session.flush()
    await session.refresh(folder)
    uow.audit(
        "folder.block_set" if enabled else "folder.block_cleared",
        "folder",
        folder.id,
        before=before,
        after=_snapshot(folder),
        scope_path=tuple(fid for fid, _ in chain),
    )
    await uow.commit()
    return folder, True


# The kinds that keep a folder from being deleted, in the fixed order every answer lists them in.
# Only LIVE things block: leftovers (soft-deleted contracts, archived subscriber cards, rule sets
# that are not in use) are purged with the folder, see PURGE_KINDS.
BLOCKER_KINDS: tuple[str, ...] = (
    "archived_ancestor",
    "subfolders",
    "contracts",
    "templates",
    "rule_sets",
    "subscriber_cards",
    "groups",
    "service_accounts",
    "entra_registrations",
)
# What is permanently removed together with the folder. ``deleted_contracts`` are contracts that
# were already soft-deleted, ``archived_subscriber_cards`` and ``unused_rule_sets`` likewise.
PURGE_KINDS: tuple[str, ...] = (
    "deleted_contracts",
    "archived_subscriber_cards",
    "unused_rule_sets",
)


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int((await session.execute(stmt)).scalar_one())


async def collect_blockers(
    session: AsyncSession,
    folder: Folder,
    chain: list[tuple[uuid.UUID, datetime | None]],
    remote: RemoteFolderUsage,
) -> list[dict[str, Any]]:
    """The non-zero ``{kind, count}`` blockers in ``BLOCKER_KINDS`` order (empty = deletable).

    ``archived_ancestor`` is 1 when a folder above this one is archived (the folder itself being
    archived is fine: deleting a retired folder is the cleanup path). ``subfolders`` counts child
    folders archived or not; groups, service accounts and Entra registrations are the ones the
    folder owns, deactivated ones included. The contracts and validation kinds come from
    ``remote``. The deletion check and the DELETE route both call this, so they cannot disagree.
    """
    counts: dict[str, int] = {
        "archived_ancestor": int(
            any(archived_at is not None for fid, archived_at in chain if fid != folder.id)
        ),
        "subfolders": await _count(
            session, select(func.count()).select_from(Folder).where(Folder.parent_id == folder.id)
        ),
        "contracts": remote.contracts,
        "templates": remote.templates,
        "rule_sets": remote.rule_sets,
        "subscriber_cards": remote.subscriber_cards,
        "groups": await _count(
            session,
            select(func.count())
            .select_from(LocalGroup)
            .where(LocalGroup.owner_folder_id == folder.id),
        ),
        "service_accounts": await _count(
            session,
            select(func.count())
            .select_from(ServiceAccount)
            .where(ServiceAccount.owner_folder_id == folder.id),
        ),
        "entra_registrations": await _count(
            session,
            select(func.count())
            .select_from(EntraRegistration)
            .where(EntraRegistration.owner_folder_id == folder.id),
        ),
    }
    return [{"kind": kind, "count": counts[kind]} for kind in BLOCKER_KINDS if counts[kind] > 0]


async def blockers_for(
    session: AsyncSession, folder_id: uuid.UUID, remote: RemoteFolderUsage
) -> list[dict[str, Any]]:
    """``collect_blockers`` for a folder id; 404 when it is gone. Read-only, takes no lock."""
    folder = (
        await session.execute(select(Folder).where(Folder.id == folder_id))
    ).scalar_one_or_none()
    if folder is None:
        raise ProblemError(404, "not-found", "Not Found", "The resource was not found.")
    return await collect_blockers(session, folder, await _chain(session, folder_id), remote)


def collect_purges(remote: RemoteFolderUsage) -> list[dict[str, Any]]:
    """The non-zero ``{kind, count}`` leftovers removed with the folder."""
    counts = {
        "deleted_contracts": remote.deleted_contracts,
        "archived_subscriber_cards": remote.archived_subscriber_cards,
        "unused_rule_sets": remote.unused_rule_sets,
    }
    return [{"kind": kind, "count": counts[kind]} for kind in PURGE_KINDS if counts[kind] > 0]


def not_empty_problem(blockers: list[dict[str, Any]]) -> ProblemError:
    return ProblemError(
        409,
        "folder-not-empty",
        "Conflict",
        "The folder is not empty. Move or delete what it contains first.",
        blockers=blockers,
    )


def archived_ancestor_problem() -> ProblemError:
    return _archived_problem(
        "An ancestor folder is archived; this subtree stays read-only until it is unarchived."
    )


def root_delete_problem() -> ProblemError:
    return ProblemError(
        422, "validation-error", "Unprocessable Content", "The ROOT folder cannot be deleted."
    )


async def delete_folder(
    uow: UnitOfWork,
    actor: Principal,
    folder_id: uuid.UUID,
    remote: RemoteFolderUsage,
    purged: RemotePurge | None = None,
) -> None:
    """Delete one empty folder, its role grants and nothing else; one ``folder.deleted`` event.

    Takes the authorization-write lock first (removing grants is an authorization write), locks the
    folder row (``FOR UPDATE`` also blocks a concurrent child, group or grant insert, whose foreign
    key check takes ``FOR KEY SHARE``), re-checks ``folder:delete`` under the lock, then re-reads
    the identity-owned blockers. ``remote`` carries the answers of contracts and validation read
    before this transaction; ``purged`` is what the purge calls removed, recorded in the event. A
    row that slips in between and trips a foreign key is the same 409. ROOT never. Grants on the
    folder are removed in the same transaction: Global roles are bound to ROOT, so this can never
    remove the last Global admin.
    """
    session = uow.session
    await lock_authz_writes(session)
    folder = await _locked(session, folder_id)
    if folder.kind == "root":
        raise root_delete_problem()
    await require_permission(session, actor, folder_id, "folder:delete")
    chain = await _chain(session, folder_id)
    blockers = await collect_blockers(session, folder, chain, remote)
    if any(b["kind"] == "archived_ancestor" for b in blockers):
        raise archived_ancestor_problem()
    if blockers:
        raise not_empty_problem(blockers)
    grants = (
        (await session.execute(select(RoleAssignment).where(RoleAssignment.folder_id == folder_id)))
        .scalars()
        .all()
    )
    removed = [UnitOfWork.snapshot(grant) for grant in grants]
    ancestors = await _ancestor_summaries(session, folder_id)
    before = {
        **_snapshot(folder),
        "slug_path": ltree_to_slug_path(folder.path),
        "ancestors": ancestors,
        "removed_grants": removed,
        "purged": asdict(purged) if purged is not None else {},
    }
    try:
        for grant in grants:
            await session.delete(grant)
        await session.flush()
        await session.delete(folder)
        await session.flush()
    except IntegrityError as exc:
        raise not_empty_problem([]) from exc
    uow.audit(
        "folder.deleted",
        "folder",
        folder_id,
        before=before,
        after=None,
        scope_path=tuple(fid for fid, _ in chain),
    )
    await uow.commit()


async def _ancestor_summaries(session: AsyncSession, folder_id: uuid.UUID) -> list[dict[str, Any]]:
    """``{id, kind, display_name}`` of every ancestor (not the folder), root first."""
    target = aliased(Folder)
    ancestor = aliased(Folder)
    stmt = (
        select(ancestor.id, ancestor.kind, ancestor.display_name)
        .join(target, ancestor.path.op("@>")(target.path))
        .where(target.id == folder_id, ancestor.id != folder_id)
        .order_by(func.nlevel(ancestor.path))
    )
    return [
        {"id": str(row[0]), "kind": row[1], "display_name": row[2]}
        for row in (await session.execute(stmt)).all()
    ]
