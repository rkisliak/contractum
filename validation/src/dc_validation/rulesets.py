"""Rule-set domain service: create, edit the draft, check and publish (RULE-01..04, D-109..D-117).

A rule set is one editable draft (``RuleSet.draft_*``) plus immutable numbered versions
(``RuleSetVersion``). Rules have no tables of their own: each keeps a stable key inside its set and
is versioned only through the set (D-109).

Publishing is check-then-freeze (research Pattern 3). The draft is read in a short session that is
closed again; the full rule-set check runs in the sandbox with no session or lock held (a check can
take seconds, and a held lock would stall every other writer); only then is the row locked, and
the version is inserted only if the draft is still exactly the bytes that were checked
(``If-Match`` plus the content hash, compared under ``FOR UPDATE``). A draft saved in between makes
the publish a 412, never a publish of unchecked rules.

Every write goes through the caller's ``UnitOfWork`` (or, for publish, one it opens itself) and
records exactly one metadata-only audit event: rule keys and content hashes, never a condition or
a fixture (D-117). ``folder_id``, ``ancestor_ids`` and ``organization_id`` come only from
identity's folder facts, never from the client (D-113).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Collection
from datetime import datetime, timedelta
from typing import Annotated, Any

from dc_auth.authorizer import FolderFacts
from dc_core.actor import Actor
from dc_core.app import read_session
from dc_core.conditional import require_if_match
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from dc_odcs.diff import compare
from dc_rules.checks import (
    FIXTURE_CODES,
    RuleSetCheckReport,
    assemble_check,
    check_units,
    static_problems,
)
from dc_rules.model import DSL_VERSION, RuleDef, RuleSetContent, content_sha256
from dc_rules.ports import Limits
from fastapi import Depends
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dc_validation.models import (
    SCOPE_FOLDER,
    SCOPE_PLATFORM,
    UNIQUE_RULE_SET_FOLDER_NAME_INDEX,
    OutboxEvent,
    RuleSet,
    RuleSetVersion,
)
from dc_validation.resolution import (
    CandidateSet,
    ResolvedRuleSet,
    VersionInfo,
    enforced_version_no,
    resolve_mandatory,
    restamp_on_set,
)
from dc_validation.sandbox import Sandbox, SandboxBusy, SandboxUnavailable
from dc_validation.settings import ValidationSettings

SERVICE = "validation"

# Test seam: awaited between the sandbox check and the lock. Never passed in production.
AfterCheck = Callable[[], Awaitable[None]]


def not_found() -> ProblemError:
    """The one 404 for an unknown rule set and for one the caller may not read."""
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


async def rule_set_folder(
    rule_set_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    """Route target resolver: the folder that holds the rule set.

    An unknown id and a platform-scope set (which has no folder, so no permission can be held on
    it) give the same uniform 404.
    """
    folder_id = (
        await session.execute(select(RuleSet.folder_id).where(RuleSet.id == rule_set_id))
    ).scalar_one_or_none()
    # The lookup is done: end its transaction so the pooled connection is not held idle in
    # transaction through the authorization call and a slow sandbox check (SEC-2, WR-04).
    await session.rollback()
    if folder_id is None:
        raise not_found()
    return folder_id


def etag_for_rule_set(revision: int) -> str:
    """The strong ETag of a rule set's draft: ``"r<revision>"`` (D-114)."""
    return f'"r{revision}"'


def rule_set_snapshot(rule_set: RuleSet) -> dict[str, Any]:
    """Allow-listed audit view of a rule set (never ``draft_rules``)."""
    return UnitOfWork.snapshot(rule_set)


def version_snapshot(version: RuleSetVersion) -> dict[str, Any]:
    """Allow-listed audit view of a version (keys and hash, never the rules)."""
    return UnitOfWork.snapshot(version)


def fixture_changes(previous: Any, document: Any) -> list[Any]:
    """The cel-2 ``changes`` binding of a fixture: ``dc_odcs.diff`` classification of the pair."""
    return compare(previous, document).as_binding()


def _rules_json(rules: list[RuleDef]) -> list[dict[str, Any]]:
    return [rule.model_dump(mode="json", by_alias=True) for rule in rules]


def _content_of(rules_json: list[dict[str, Any]]) -> RuleSetContent:
    return RuleSetContent.model_validate({"dsl_version": DSL_VERSION, "rules": rules_json})


def _name_taken() -> ProblemError:
    return ProblemError(
        409,
        "rule-set-name-taken",
        "Conflict",
        "A rule set with this name already exists in the folder.",
    )


def _precondition_failed() -> ProblemError:
    return ProblemError(
        412,
        "precondition-failed",
        "Precondition Failed",
        "The resource has changed since you last read it; reload it and retry.",
    )


async def create_rule_set(
    uow: UnitOfWork, facts: FolderFacts, name: str, description: str | None
) -> RuleSet:
    """Insert a rule set with an empty draft in the folder of ``facts``; one ``ruleset.created``.

    The name is checked first (case-insensitive, per folder); the unique index is the backstop for
    two requests racing past the check, mapped by its constraint name.
    """
    session = uow.session
    taken = (
        await session.execute(
            select(RuleSet.id).where(
                RuleSet.scope == SCOPE_FOLDER,
                RuleSet.folder_id == facts.id,
                func.lower(RuleSet.name) == name.lower(),
            )
        )
    ).first()
    if taken is not None:
        raise _name_taken()
    rule_set = RuleSet(
        scope=SCOPE_FOLDER,
        folder_id=facts.id,
        folder_kind=facts.kind,
        ancestor_ids=list(facts.ancestor_ids),
        organization_id=facts.organization_id,  # None for ROOT, which has no organization
        name=name,
        description=description,
        publish_counter=0,
        draft_rules=[],
        draft_revision=0,
        draft_content_sha256=content_sha256(RuleSetContent(rules=[])),
    )
    session.add(rule_set)
    try:
        await session.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == UNIQUE_RULE_SET_FOLDER_NAME_INDEX:
            raise _name_taken() from exc
        raise
    await session.refresh(rule_set)
    uow.audit(
        "ruleset.created",
        "rule_set",
        rule_set.id,
        before=None,
        after=rule_set_snapshot(rule_set),
        scope_path=facts.ancestor_ids,
        aggregate_version=0,
    )
    return rule_set


async def locked_rule_set(session: AsyncSession, rule_set_id: uuid.UUID) -> RuleSet:
    """The rule set under ``SELECT ... FOR UPDATE`` (serialises writers); 404 when unknown."""
    rule_set = (
        await session.execute(
            select(RuleSet)
            .where(RuleSet.id == rule_set_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if rule_set is None:
        raise not_found()
    return rule_set


async def newest_version(session: AsyncSession, rule_set_id: uuid.UUID) -> RuleSetVersion | None:
    """The highest-numbered version, deprecated or not."""
    return (
        await session.execute(
            select(RuleSetVersion)
            .where(RuleSetVersion.rule_set_id == rule_set_id)
            .order_by(RuleSetVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _store_draft(
    uow: UnitOfWork, rule_set: RuleSet, description: str | None, rules: list[RuleDef]
) -> bool:
    """Write ``rules`` (and the description when given) into the locked draft; True on a change.

    Content equal to the stored draft is a no-op: same revision, no event. Otherwise the revision
    is bumped and one ``ruleset.draft_updated`` event records the rule keys and the content hash,
    never a condition or a fixture. Every draft write (whole draft or one rule) goes through here,
    so they share the lock-then-compare discipline and the audit shape.
    """
    session = uow.session
    new_hash = content_sha256(RuleSetContent(rules=rules))
    new_description = rule_set.description if description is None else description
    if rule_set.draft_content_sha256 == new_hash and rule_set.description == new_description:
        return False
    before = rule_set_snapshot(rule_set)
    rule_set.draft_rules = _rules_json(rules)
    rule_set.draft_content_sha256 = new_hash
    rule_set.description = new_description
    rule_set.draft_revision += 1
    await session.flush()
    await session.refresh(rule_set)
    uow.audit(
        "ruleset.draft_updated",
        "rule_set",
        rule_set.id,
        before=before,
        after={
            **rule_set_snapshot(rule_set),
            "rule_keys": [rule.key for rule in rules],
            "content_sha256": new_hash,
        },
        scope_path=rule_set.ancestor_ids,
        aggregate_version=rule_set.publish_counter,
    )
    return True


async def save_draft(
    uow: UnitOfWork,
    rule_set_id: uuid.UUID,
    description: str | None,
    rules: list[RuleDef],
    if_match: str | None,
) -> tuple[RuleSet, bool]:
    """Replace the draft rules (and the description when given); ``bool`` is True on a change.

    The row is locked, then ``If-Match`` is compared with the strong ETag ``"r<revision>"``
    (missing 428, stale 412).
    """
    rule_set = await locked_rule_set(uow.session, rule_set_id)
    require_if_match(if_match, etag_for_rule_set(rule_set.draft_revision))
    changed = await _store_draft(uow, rule_set, description, rules)
    return rule_set, changed


def _draft_rules_of(rule_set: RuleSet) -> list[RuleDef]:
    return [RuleDef.model_validate(rule) for rule in rule_set.draft_rules]


async def put_rule(
    uow: UnitOfWork, rule_set_id: uuid.UUID, rule: RuleDef, if_match: str | None
) -> tuple[RuleSet, bool]:
    """Create or replace one rule in the draft by its key; ``bool`` is True on a change.

    An existing key is replaced in place (the draft order is kept), a new one is appended. The
    write is a whole-draft save under the same row lock, ``If-Match`` and event as ``save_draft``;
    putting a rule identical to the stored one is a no-op.
    """
    rule_set = await locked_rule_set(uow.session, rule_set_id)
    require_if_match(if_match, etag_for_rule_set(rule_set.draft_revision))
    rules = _draft_rules_of(rule_set)
    for index, existing in enumerate(rules):
        if existing.key == rule.key:
            rules[index] = rule
            break
    else:
        rules.append(rule)
    changed = await _store_draft(uow, rule_set, None, rules)
    return rule_set, changed


async def delete_rule(
    uow: UnitOfWork, rule_set_id: uuid.UUID, rule_key: str, if_match: str | None
) -> RuleSet:
    """Remove one rule from the draft; 404 when the key is not in it (after ``If-Match``)."""
    rule_set = await locked_rule_set(uow.session, rule_set_id)
    require_if_match(if_match, etag_for_rule_set(rule_set.draft_revision))
    rules = _draft_rules_of(rule_set)
    kept = [rule for rule in rules if rule.key != rule_key]
    if len(kept) == len(rules):
        raise not_found()
    await _store_draft(uow, rule_set, None, kept)
    return rule_set


async def list_rule_sets(session: AsyncSession, folder_id: uuid.UUID) -> list[RuleSet]:
    """The rule sets held at one folder (not its ancestors), by ``lower(name)`` then id."""
    return list(
        (
            await session.execute(
                select(RuleSet)
                .where(RuleSet.scope == SCOPE_FOLDER, RuleSet.folder_id == folder_id)
                .order_by(func.lower(RuleSet.name), RuleSet.id)
            )
        )
        .scalars()
        .all()
    )


async def list_versions(session: AsyncSession, rule_set_id: uuid.UUID) -> list[RuleSetVersion]:
    """Every published version of a rule set, newest first (deprecated ones included)."""
    return list(
        (
            await session.execute(
                select(RuleSetVersion)
                .where(RuleSetVersion.rule_set_id == rule_set_id)
                .order_by(RuleSetVersion.version_no.desc())
            )
        )
        .scalars()
        .all()
    )


async def deprecate_version(
    uow: UnitOfWork, rule_set_id: uuid.UUID, version_no: int
) -> tuple[RuleSetVersion, bool]:
    """Stamp a version deprecated; ``bool`` is True when it was newly deprecated (D-114).

    The rule set row is locked first (the same order as publish), then the version row, so a
    deprecation and a publish of the same set cannot interleave. An already deprecated version is
    a no-op without an event. The content stays readable, and a contract version that pins it keeps
    resolving it.
    """
    session = uow.session
    rule_set = await locked_rule_set(session, rule_set_id)
    version = (
        await session.execute(
            select(RuleSetVersion)
            .where(
                RuleSetVersion.rule_set_id == rule_set.id,
                RuleSetVersion.version_no == version_no,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if version is None:
        raise not_found()
    if version.deprecated_at is not None:
        return version, False
    before = version_snapshot(version)
    version.deprecated_at = func.now()
    version.deprecated_by = uow.actor.id
    await session.flush()
    await session.refresh(version)
    uow.audit(
        "ruleset.version_deprecated",
        "rule_set",
        rule_set.id,
        before=before,
        after=version_snapshot(version),
        scope_path=rule_set.ancestor_ids,
        aggregate_version=version.version_no,
    )
    return version, True


async def delete_rule_set(uow: UnitOfWork, rule_set_id: uuid.UUID) -> None:
    """Delete a rule set that has never been published; 409 ``rule-set-published`` otherwise.

    A published version may be pinned by a contract version, so a set with any version is kept
    for good (D-114). The count is read under the row lock, so a publish racing the delete either
    lands first (409 here) or finds the set gone.
    """
    session = uow.session
    rule_set = await locked_rule_set(session, rule_set_id)
    if rule_set.publish_counter > 0:
        raise ProblemError(
            409,
            "rule-set-published",
            "Conflict",
            "A rule set with published versions cannot be deleted.",
        )
    before = rule_set_snapshot(rule_set)
    ancestors = list(rule_set.ancestor_ids)
    await session.delete(rule_set)
    await session.flush()
    uow.audit(
        "ruleset.deleted",
        "rule_set",
        rule_set_id,
        before=before,
        after=None,
        scope_path=ancestors,
        aggregate_version=0,
    )


async def available_rule_sets(
    session: AsyncSession, facts: FolderFacts
) -> list[tuple[RuleSet, list[RuleSetVersion]]]:
    """Published rule sets that apply at a folder: platform sets plus those it or an ancestor holds.

    Keyed on ``facts.ancestor_ids`` (root first, the folder itself last), never on the caller's
    grants: the caller's permission was already checked at this very folder, so a descendant reads
    what applies to it (D-115, research Pitfall 4). Only sets with a published version appear, each
    with all its published versions newest first (deprecated ones included, so a caller can tell
    the latest from the latest active). Ordered by holder depth (platform 0, ROOT 1 .. the folder
    last), then ``lower(name)``, then id; drafts are never read.
    """
    rows = (
        (
            await session.execute(
                select(RuleSet).where(
                    or_(
                        RuleSet.scope == SCOPE_PLATFORM,
                        and_(
                            RuleSet.scope == SCOPE_FOLDER,
                            RuleSet.folder_id.in_(list(facts.ancestor_ids)),
                        ),
                    ),
                    RuleSet.publish_counter > 0,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    depth = {folder_id: index for index, folder_id in enumerate(facts.ancestor_ids, start=1)}

    def holder_depth(rule_set: RuleSet) -> int:
        return 0 if rule_set.folder_id is None else depth[rule_set.folder_id]

    ordered = sorted(rows, key=lambda r: (holder_depth(r), r.name.lower(), r.id))
    versions = (
        (
            await session.execute(
                select(RuleSetVersion)
                .where(RuleSetVersion.rule_set_id.in_([r.id for r in ordered]))
                .order_by(RuleSetVersion.rule_set_id, RuleSetVersion.version_no.desc())
            )
        )
        .scalars()
        .all()
    )
    by_set: dict[uuid.UUID, list[RuleSetVersion]] = {r.id: [] for r in ordered}
    for version in versions:
        by_set[version.rule_set_id].append(version)
    return [(r, by_set[r.id]) for r in ordered if by_set[r.id]]


async def available_rule_set(
    session: AsyncSession, facts: FolderFacts, rule_set_id: uuid.UUID
) -> tuple[RuleSet, list[RuleSetVersion]]:
    """One rule set as seen from a folder; the uniform 404 when it does not apply there.

    An unknown id, a set held in a sibling branch or below the folder, and a set with no
    published version are the same 404.
    """
    for rule_set, versions in await available_rule_sets(session, facts):
        if rule_set.id == rule_set_id:
            return rule_set, versions
    raise not_found()


async def db_now(session: AsyncSession) -> datetime:
    """The database clock (``SELECT now()``): the only clock that decides warn versus enforce."""
    return (await session.execute(select(func.now()))).scalar_one()


def _version_info(version: RuleSetVersion) -> VersionInfo:
    return VersionInfo(
        id=version.id,
        version_no=version.version_no,
        dsl_version=version.dsl_version,
        enforce_at=version.enforce_at,
        deprecated_at=version.deprecated_at,
    )


def _candidate(
    rule_set: RuleSet, versions: list[RuleSetVersion], ancestor_ids: tuple[uuid.UUID, ...]
) -> CandidateSet:
    if rule_set.folder_id is None:
        depth = 0
    else:
        depth = ancestor_ids.index(rule_set.folder_id) + 1  # 1-based: ROOT is depth 1
    return CandidateSet(
        rule_set_id=rule_set.id,
        name=rule_set.name,
        scope=rule_set.scope,
        folder_id=rule_set.folder_id,
        folder_kind=rule_set.folder_kind,
        depth=depth,
        versions=tuple(_version_info(v) for v in versions),
        paused=rule_set.paused_at is not None,
    )


async def load_mandatory(session: AsyncSession, facts: FolderFacts) -> list[CandidateSet]:
    """Mandatory rule sets that apply at a folder, with their published versions.

    Keyed on the structural ``facts.ancestor_ids`` (root first, the folder itself last), which a
    block-inheritance flag never changes, so a block flag cannot stop a mandatory set (D-29, D-119).
    A set held at the folder itself applies (the ancestors include it); platform sets always apply.
    """
    ancestors = tuple(facts.ancestor_ids)
    rows = (
        (
            await session.execute(
                select(RuleSet).where(
                    RuleSet.mandatory.is_(True),
                    or_(
                        RuleSet.scope == SCOPE_PLATFORM,
                        and_(RuleSet.scope == SCOPE_FOLDER, RuleSet.folder_id.in_(list(ancestors))),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    versions = (
        (
            await session.execute(
                select(RuleSetVersion)
                .where(RuleSetVersion.rule_set_id.in_([r.id for r in rows]))
                .order_by(RuleSetVersion.rule_set_id, RuleSetVersion.version_no)
            )
        )
        .scalars()
        .all()
    )
    by_set: dict[uuid.UUID, list[RuleSetVersion]] = {r.id: [] for r in rows}
    for version in versions:
        by_set[version.rule_set_id].append(version)
    return [_candidate(r, by_set[r.id], ancestors) for r in rows]


async def load_candidates(
    session: AsyncSession, facts: FolderFacts, rule_set_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, CandidateSet]:
    """The named rule sets that are visible at a folder, each with all its published versions.

    Visible means held by the platform or by the folder or one of its ancestors, and with a
    published version (the same rule as ``available_rule_sets``). A set that is unknown, held in a
    sibling branch or below, or never published is simply absent from the result: the caller
    reports it as unresolvable, it never learns which of those it was.
    """
    ids = list(set(rule_set_ids))
    if not ids:
        return {}
    ancestors = tuple(facts.ancestor_ids)
    rows = (
        (
            await session.execute(
                select(RuleSet).where(
                    RuleSet.id.in_(ids),
                    RuleSet.publish_counter > 0,
                    or_(
                        RuleSet.scope == SCOPE_PLATFORM,
                        and_(RuleSet.scope == SCOPE_FOLDER, RuleSet.folder_id.in_(list(ancestors))),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {}
    versions = (
        (
            await session.execute(
                select(RuleSetVersion)
                .where(RuleSetVersion.rule_set_id.in_([r.id for r in rows]))
                .order_by(RuleSetVersion.rule_set_id, RuleSetVersion.version_no)
            )
        )
        .scalars()
        .all()
    )
    by_set: dict[uuid.UUID, list[RuleSetVersion]] = {r.id: [] for r in rows}
    for version in versions:
        by_set[version.rule_set_id].append(version)
    return {r.id: _candidate(r, by_set[r.id], ancestors) for r in rows}


async def resolved_mandatory(session: AsyncSession, facts: FolderFacts) -> list[ResolvedRuleSet]:
    """The effective mandatory entries at a folder, at the database's current time."""
    now = await db_now(session)
    return resolve_mandatory(await load_mandatory(session, facts), now)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _not_published() -> ProblemError:
    return ProblemError(
        422,
        "rule-set-not-published",
        "Unprocessable Content",
        "A rule set must have a published version before it can be made mandatory.",
    )


def _not_mandatory() -> ProblemError:
    return ProblemError(
        409,
        "rule-set-not-mandatory",
        "Conflict",
        "The version has no enforcement date: its rule set is not mandatory.",
    )


async def _versions_locked(session: AsyncSession, rule_set_id: uuid.UUID) -> list[RuleSetVersion]:
    """Every version of a set, ascending, under ``FOR UPDATE`` (the set row is already locked)."""
    return list(
        (
            await session.execute(
                select(RuleSetVersion)
                .where(RuleSetVersion.rule_set_id == rule_set_id)
                .order_by(RuleSetVersion.version_no)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def set_mandatory(
    uow: UnitOfWork, rule_set_id: uuid.UUID, mandatory: bool, warn_days: int
) -> tuple[RuleSet, bool]:
    """Set or clear the mandatory flag; ``bool`` is True when it changed (D-119, D-120, D-142).

    Setting an already mandatory set is a no-op (no event, no ``enforce_at`` moves): no action
    extends or restarts a warn period. A first-time set stamps the newest version ``now + grace``;
    a set that was cleared restores the version enforced at clear time immediately and gives only
    the newer versions a new grace. Clearing (the audited break-glass) records the version that was
    enforced, then nulls ``enforce_at`` on every version, all in one transaction with one event.
    """
    session = uow.session
    rule_set = await locked_rule_set(session, rule_set_id)
    if mandatory == rule_set.mandatory:
        return rule_set, False
    now = await db_now(session)
    versions = await _versions_locked(session, rule_set.id)
    before = rule_set_snapshot(rule_set)
    infos = tuple(_version_info(v) for v in versions)
    by_no = {v.version_no: v for v in versions}

    if mandatory:
        if rule_set.publish_counter == 0 or not versions:
            raise _not_published()
        restored = rule_set.cleared_enforced_version_no
        stamps = restamp_on_set(infos, restored, now, timedelta(days=warn_days))
        rule_set.mandatory = True
        rule_set.mandatory_set_at = now
        rule_set.mandatory_set_by = uow.actor.id
        rule_set.cleared_enforced_version_no = None
        for number, when in stamps.items():
            by_no[number].enforce_at = when
        await session.flush()
        await session.refresh(rule_set)
        uow.audit(
            "ruleset.mandatory_set",
            "rule_set",
            rule_set.id,
            before=before,
            after={
                **rule_set_snapshot(rule_set),
                "version_no": versions[-1].version_no,
                "enforce_at": _iso(by_no[versions[-1].version_no].enforce_at),
                "enforce_at_by_version": {str(n): _iso(w) for n, w in sorted(stamps.items())},
                "restored_version_no": restored if restored in stamps else None,
            },
            scope_path=rule_set.ancestor_ids,
            aggregate_version=rule_set.publish_counter,
        )
        return rule_set, True

    candidate = _candidate(rule_set, versions, tuple(rule_set.ancestor_ids))
    enforced = enforced_version_no(candidate, now)
    warn = sorted(
        v.version_no
        for v in versions
        if v.enforce_at is not None
        and v.enforce_at > now
        and (enforced is None or v.version_no > enforced)
    )
    previous = {str(v.version_no): _iso(v.enforce_at) for v in versions if v.enforce_at}
    rule_set.mandatory = False
    rule_set.cleared_enforced_version_no = enforced
    for version in versions:
        version.enforce_at = None
    await session.flush()
    await session.refresh(rule_set)
    uow.audit(
        "ruleset.mandatory_cleared",
        "rule_set",
        rule_set.id,
        before=before,
        after={
            **rule_set_snapshot(rule_set),
            "enforced_version_no": enforced,
            "warn_version_nos": warn,
            "enforce_at_by_version": previous,
        },
        scope_path=rule_set.ancestor_ids,
        aggregate_version=rule_set.publish_counter,
    )
    return rule_set, True


async def _locked_version(
    session: AsyncSession, rule_set: RuleSet, version_no: int
) -> RuleSetVersion:
    """One version of a (locked) rule set under ``FOR UPDATE``; unknown is the uniform 404."""
    version = (
        await session.execute(
            select(RuleSetVersion)
            .where(
                RuleSetVersion.rule_set_id == rule_set.id,
                RuleSetVersion.version_no == version_no,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if version is None:
        raise not_found()
    return version


async def enforce_now(
    uow: UnitOfWork, rule_set_id: uuid.UUID, version_no: int
) -> tuple[RuleSetVersion, bool]:
    """Bring a version's enforcement date forward to now; ``bool`` is True when it moved (RULE-08).

    A version with no ``enforce_at`` (the set is not mandatory) is 409 ``rule-set-not-mandatory``;
    one already enforced is a no-op. This only ever moves the date earlier; to postpone or set any
    date use ``reschedule_enforcement`` (it keeps the ``ruleset.enforced_early`` event separate).
    """
    session = uow.session
    rule_set = await locked_rule_set(session, rule_set_id)
    version = await _locked_version(session, rule_set, version_no)
    if version.enforce_at is None:
        raise _not_mandatory()
    now = await db_now(session)
    if version.enforce_at <= now:
        return version, False
    before = version_snapshot(version)
    version.enforce_at = now
    await session.flush()
    await session.refresh(version)
    uow.audit(
        "ruleset.enforced_early",
        "rule_set",
        rule_set.id,
        before=before,
        after={**version_snapshot(version), "previous_enforce_at": before["enforce_at"]},
        scope_path=rule_set.ancestor_ids,
        aggregate_version=version.version_no,
    )
    return version, True


async def reschedule_enforcement(
    uow: UnitOfWork, rule_set_id: uuid.UUID, version_no: int, enforce_at: datetime
) -> tuple[RuleSetVersion, bool]:
    """Move a version's enforcement date to any timestamp; ``bool`` is True when it changed.

    A future date warns until then (postponing an enforced version is allowed), now or a past date
    enforces at once; a past date is stored as the database now, never before it. This reverses
    D-120's earlier-only rule at the user's request (quick 261118-vpa); the database clock still
    decides the mode. The same effective date again, or a past date for an already enforced
    version, is a no-op (no event). A version with no ``enforce_at`` (the set is not mandatory) is
    409 ``rule-set-not-mandatory``. ``enforce_now`` stays as is.
    """
    session = uow.session
    rule_set = await locked_rule_set(session, rule_set_id)
    version = await _locked_version(session, rule_set, version_no)
    if version.enforce_at is None:
        raise _not_mandatory()
    now = await db_now(session)
    effective = now if enforce_at <= now else enforce_at
    old = version.enforce_at
    if effective == old or (effective == now and old <= now):
        return version, False
    before = version_snapshot(version)
    version.enforce_at = effective
    await session.flush()
    await session.refresh(version)
    uow.audit(
        "ruleset.enforcement_rescheduled",
        "rule_set",
        rule_set.id,
        before=before,
        after={
            **version_snapshot(version),
            "previous_enforce_at": before["enforce_at"],
            "previous_mode": "enforce" if old <= now else "warn",
            "mode": "enforce" if effective <= now else "warn",
            "paused": rule_set.paused_at is not None,
        },
        scope_path=rule_set.ancestor_ids,
        aggregate_version=version.version_no,
    )
    return version, True


async def set_paused(uow: UnitOfWork, rule_set_id: uuid.UUID, paused: bool) -> tuple[RuleSet, bool]:
    """Pause or resume a platform rule set; ``bool`` is True when the state changed.

    A paused set still runs and reports, but resolves as a non-blocking ``warn`` (the overlay in
    ``resolution.py``); ``enforce_at`` is untouched, so resume restores the computed mode exactly.
    Repeating the current state is a no-op (no event). Only platform sets can be paused (the
    ``paused_platform_only`` CHECK and the route agree): this reverses D-130 and D-150 at the user's
    request (quick 261118-vpa); folder sets keep the D-119/D-142 clear/set break-glass.
    """
    session = uow.session
    rule_set = await locked_rule_set(session, rule_set_id)
    if rule_set.scope != SCOPE_PLATFORM:
        raise not_found()
    if not rule_set.mandatory:
        raise _not_mandatory()
    if (rule_set.paused_at is not None) == paused:
        return rule_set, False
    now = await db_now(session)
    versions = await _versions_locked(session, rule_set.id)
    candidate = _candidate(rule_set, versions, tuple(rule_set.ancestor_ids))
    enforced = enforced_version_no(candidate, now)
    warn = sorted(
        v.version_no
        for v in versions
        if v.enforce_at is not None
        and v.enforce_at > now
        and (enforced is None or v.version_no > enforced)
    )
    before = rule_set_snapshot(rule_set)
    old_paused_at = rule_set.paused_at
    if paused:
        rule_set.paused_at = now
        rule_set.paused_by = uow.actor.id
    else:
        rule_set.paused_at = None
        rule_set.paused_by = None
    await session.flush()
    await session.refresh(rule_set)
    after: dict[str, Any] = {
        **rule_set_snapshot(rule_set),
        "enforced_version_no": enforced,
        "warn_version_nos": warn,
        "enforce_at_by_version": {
            str(v.version_no): _iso(v.enforce_at) for v in versions if v.enforce_at is not None
        },
    }
    if not paused:
        after["paused_since"] = _iso(old_paused_at)
    uow.audit(
        "ruleset.paused" if paused else "ruleset.resumed",
        "rule_set",
        rule_set.id,
        before=before,
        after=after,
        scope_path=rule_set.ancestor_ids,
        aggregate_version=rule_set.publish_counter,
    )
    return rule_set, True


def _unavailable(*, busy: bool) -> ProblemError:
    return ProblemError(
        503,
        "validation-unavailable",
        "Service Unavailable",
        "The rule sandbox is saturated; retry shortly."
        if busy
        else "The rule sandbox could not be started.",
        headers={"Retry-After": "1"} if busy else None,
    )


async def run_check(
    sandbox: Sandbox, content: RuleSetContent, limits: Limits
) -> RuleSetCheckReport:
    """The full rule-set check through the sandbox (no session or lock may be held by the caller).

    A saturated sandbox is 503 ``validation-unavailable`` with ``Retry-After: 1`` and an
    unavailable one the same slug without it; neither is ever a pass (D-137).
    """
    static = static_problems(content, limits)
    # Fixtures with a baseline get their classified changes from the dc_odcs diff, injected so
    # dc_rules stays free of other platform packages. The diff of up to ``max_fixtures_per_kind``
    # documents is CPU work, so it runs off the event loop.
    units = await asyncio.to_thread(check_units, content, limits, changes_for=fixture_changes)
    try:
        results = await sandbox.check(units) if units else {}
    except SandboxBusy as exc:
        raise _unavailable(busy=True) from exc
    except SandboxUnavailable as exc:
        raise _unavailable(busy=False) from exc
    return assemble_check(content, static, results, limits)


def _refusal(report: RuleSetCheckReport) -> ProblemError:
    """422 for a failed check: fixture slug only when every problem is a fixture problem."""
    problems = [*report.rule_set_problems, *(p for rule in report.rules for p in rule.problems)]
    check = report.model_dump(mode="json", exclude_none=True)
    if problems and all(problem.code in FIXTURE_CODES for problem in problems):
        return ProblemError(
            422,
            "ruleset-fixtures-failed",
            "Unprocessable Content",
            "A rule's fixtures do not behave as declared; see the check report.",
            check=check,
        )
    return ProblemError(
        422,
        "rule-invalid",
        "Unprocessable Content",
        "The rule set did not pass its check; see the check report.",
        check=check,
    )


async def publish_rule_set(
    session_factory: async_sessionmaker[AsyncSession],
    sandbox: Sandbox,
    settings: ValidationSettings,
    actor: Actor,
    rule_set_id: uuid.UUID,
    if_match: str | None,
    *,
    after_check: AfterCheck | None = None,
    warn_days: int | None = None,
) -> tuple[RuleSetVersion, bool]:
    """Publish the draft as the next immutable version; ``bool`` is True when one was created.

    Order (research Pattern 3): read the draft and close the session, cheap ``If-Match``, check in
    the sandbox with nothing held, refuse with 422 when the check is not ok, then lock the row and
    re-check ``If-Match`` and the hash that was checked (412 otherwise). A draft whose hash equals
    the newest version's is not published again: that version is returned with ``False`` and no
    event is recorded. ``after_check`` is a test seam, never passed in production. ``warn_days`` is
    the grace period in force (the stored governance setting, D-34); ``None`` uses the env value.
    """
    async with session_factory() as read:
        row = (
            await read.execute(select(RuleSet).where(RuleSet.id == rule_set_id))
        ).scalar_one_or_none()
        if row is None:
            raise not_found()
        revision = row.draft_revision
        draft_rules = list(row.draft_rules)
    require_if_match(if_match, etag_for_rule_set(revision))

    content = _content_of(draft_rules)
    checked_hash = content_sha256(content)
    limits = settings.limits()
    report = await run_check(sandbox, content, limits)
    if not report.ok:
        raise _refusal(report)
    if after_check is not None:
        await after_check()

    uow = UnitOfWork(session_factory, actor=actor, outbox_model=OutboxEvent, service=SERVICE)
    async with uow:
        session = uow.session
        rule_set = await locked_rule_set(session, rule_set_id)
        require_if_match(if_match, etag_for_rule_set(rule_set.draft_revision))
        if (
            rule_set.draft_content_sha256 != checked_hash
            or content_sha256(_content_of(rule_set.draft_rules)) != checked_hash
        ):
            raise _precondition_failed()

        current = await newest_version(session, rule_set.id)
        if current is not None and current.content_sha256 == checked_hash:
            session.expunge(current)  # the unit's rollback on exit must not expire the result
            return current, False

        before = rule_set_snapshot(rule_set)
        rule_set.publish_counter += 1
        version = RuleSetVersion(
            rule_set_id=rule_set.id,
            version_no=rule_set.publish_counter,
            dsl_version=DSL_VERSION,
            rules=list(rule_set.draft_rules),
            content_sha256=checked_hash,
            rule_keys=[rule.key for rule in content.rules],
        )
        if rule_set.mandatory:
            # D-120: a new version of a mandatory set starts its own warn period, stamped from the
            # database clock in the same transaction as the insert. ``warn_days`` is an int >= 0.
            warn_days = int(settings.mandatory_warn_days if warn_days is None else warn_days)
            version.enforce_at = func.now() + func.make_interval(0, 0, 0, warn_days)  # bound
        session.add(version)
        await session.flush()
        await session.refresh(rule_set)
        await session.refresh(version)
        uow.audit(
            "ruleset.version_published",
            "rule_set",
            rule_set.id,
            before=before,
            after={**rule_set_snapshot(rule_set), "version": version_snapshot(version)},
            scope_path=rule_set.ancestor_ids,
            aggregate_version=version.version_no,
        )
        await uow.commit()
        session.expunge(version)  # the unit's rollback on exit must not expire the result
    return version, True
