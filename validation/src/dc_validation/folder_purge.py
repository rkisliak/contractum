"""What a folder still holds in validation, and the permanent removal of its unused rule sets.

Quick 261119-0v1 (deleting a folder, identity orchestrates). A folder-scoped rule set is **in use**
when its ``mandatory`` flag is set: it is enforced on the folder and everything below, which is a
governance decision somebody must clear deliberately before the folder can go. In-use sets block
the delete (``rule_sets``). Every other folder-scoped set is **unused** (``unused_rule_sets``) and
is permanently removed with the folder, published versions included.

A rule set can only be attached to, or pinned by, contracts in its own folder or below it, and
contracts and subfolders already block a folder delete. Once the folder holds no live contract and
no subfolder, no live contract can attach or pin one of its rule sets, so the mandatory flag is the
one in-use test left that validation can and must make. Pins recorded on contract versions of
soft-deleted contracts disappear with those contracts (contracts' purge). ``validation_run`` rows
are history (no foreign key to a rule set) and stay.

``purge_folder`` deletes the unused sets' versions and the sets, and refuses with 409
``folder-not-empty`` while a mandatory set is left. It runs with the delete-guard bypass
``dc.folder_purge`` on for that transaction only (migration 0008); the trigger still refuses unless
the parent rule set is folder-scoped and not mandatory. Idempotent; one counts-only audit event.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import CursorResult, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dc_validation.models import SCOPE_FOLDER, RuleSet, RuleSetVersion

PURGE_SETTING = "dc.folder_purge"


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int((await session.execute(stmt)).scalar_one())


async def folder_usage(session: AsyncSession, folder_id: uuid.UUID) -> dict[str, int]:
    """``rule_sets`` (in use: mandatory) and ``unused_rule_sets`` of ``folder_id``."""
    base = (
        select(func.count())
        .select_from(RuleSet)
        .where(RuleSet.scope == SCOPE_FOLDER, RuleSet.folder_id == folder_id)
    )
    return {
        "rule_sets": await _count(session, base.where(RuleSet.mandatory.is_(True))),
        "unused_rule_sets": await _count(session, base.where(RuleSet.mandatory.is_(False))),
    }


async def purge_folder(
    uow: UnitOfWork, folder_id: uuid.UUID, scope_path: tuple[uuid.UUID, ...]
) -> dict[str, int]:
    """Permanently remove the folder's unused rule sets and their versions; returns the counts."""
    session = uow.session
    rows = (
        await session.execute(
            select(RuleSet.id, RuleSet.mandatory)
            .where(RuleSet.scope == SCOPE_FOLDER, RuleSet.folder_id == folder_id)
            .order_by(RuleSet.id)
            .with_for_update()
        )
    ).all()
    in_use = sum(1 for _, mandatory in rows if mandatory)
    if in_use:
        raise ProblemError(
            409,
            "folder-not-empty",
            "Conflict",
            "The folder is not empty. Move or delete what it contains first.",
            blockers=[{"kind": "rule_sets", "count": in_use}],
        )
    ids = [row[0] for row in rows]
    versions = 0
    if ids:
        await session.execute(text("SELECT set_config(:name, 'on', true)"), {"name": PURGE_SETTING})
        deleted = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(RuleSetVersion).where(RuleSetVersion.rule_set_id.in_(ids))
            ),
        )
        versions = int(deleted.rowcount)
        await session.execute(delete(RuleSet).where(RuleSet.id.in_(ids)))
    counts = {"rule_sets": len(ids), "rule_set_versions": versions}
    uow.audit(
        "folder.purged", "folder", folder_id, before=None, after=counts, scope_path=scope_path
    )
    await uow.commit()
    return counts
