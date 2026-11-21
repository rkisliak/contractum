"""Caller-scoped facts about a document's contract links, for validation runs (VAL-05, D-153).

Validation never calls contracts (D-125), so contracts resolves every link of the document it is
about to validate and sends the answers with the run (``ref_facts.contracts``), keyed by the link
exactly as written. The resolution rule is the delete guard's (``references``): a ``contract_uuid``
link names that contract in any Organization; an ``odcs_id`` link names the contract with that ODCS
id in the referrer's own Organization.

The answers are scoped to the caller (D-54): a target is visible when the caller holds
``contract:read-metadata`` over its folder, or ``contract:read-published`` over its folder and the
contract has a published version. A target the caller cannot see is ``not_found``, exactly like a
missing or soft-deleted one, so link facts are never an existence oracle. A link naming
``/versions/<semver>`` also reports the version it pins and whether that version is published.

At most ``MAX_LINK_FACTS`` links are resolved (the first in document order); the run marks the rest
unverifiable. Two or three set-based queries, no per-link round trip, and no lock. Publish re-reads
them under its lock with the scopes it already holds (``recheck_link_facts``, IN-03) and answers
409 ``baseline-changed`` when one got worse since the gate.

An identity outage while reading the caller's scopes depends on the run (WR-02): advisory runs
(save, draft validate) send no facts, so every link is ``unverifiable``; the publish gate passes
``strict=True`` and the outage is 503 ``service-unavailable``, because ``unverifiable`` never fails
a rule and a mandatory link rule would otherwise pass for a missing target.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from dc_auth.authorizer import Authorizer, AuthorizerUnavailable, FolderScope
from dc_auth.principal import Principal
from dc_core.problems import ProblemError
from dc_odcs import MAX_VERSION_CHARS, extract_contract_refs, string_at
from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.lifecycle import compare_semver, parse_semver, parts_of
from dc_contracts.models import Contract, ContractVersion
from dc_contracts.search import scope_clause

_log = structlog.get_logger("dc_contracts.link_facts")

# Mirrors validation's request limits (``ValidationRefFactsIn``): more entries or longer keys would
# make the whole run request a 422.
MAX_LINK_FACTS = 200
MAX_LINK_KEY_CHARS = 2048

_PINNED = re.compile(r"/versions/([0-9A-Za-z.+-]+)(?:[/?#].*)?$")
_NOT_FOUND = "not_found"


def pinned_version_of(raw: str) -> str | None:
    """The strict SemVer a link ends in (``.../versions/1.0.0``), or ``None``.

    A pin longer than the shared ``MAX_VERSION_CHARS`` is ``None`` (an unverifiable pin) rather
    than a value validation would refuse, which would fail the whole run (WR-01).
    """
    found = _PINNED.search(raw)
    if found is None or len(found.group(1)) > MAX_VERSION_CHARS:
        return None
    if parse_semver(found.group(1)) is None:
        return None
    return found.group(1)


@dataclass(frozen=True)
class LinkFacts:
    """The facts sent with a run, plus what a publish needs to re-check them under its lock.

    ``links``, ``organization_id`` and the two scopes are the inputs the facts were computed from;
    ``recheck_link_facts`` recomputes them from the database with the same scopes (no identity
    call under a lock). Empty ``links`` means nothing to re-check.
    """

    facts: dict[str, dict[str, Any]]
    organization_id: uuid.UUID | None = None
    links: tuple[tuple[str, str, str], ...] = ()
    metadata_scope: FolderScope | None = field(default=None, repr=False)
    published_scope: FolderScope | None = field(default=None, repr=False)


def _document_links(document: Any) -> tuple[tuple[str, str, str], ...]:
    """``(raw, kind, value)`` per distinct link as written, first ``MAX_LINK_FACTS`` only."""
    links: list[tuple[str, str, str]] = []
    # Keyed by the link as written, like validation (IN-04): a second link to the same contract
    # that pins another version is its own fact.
    for ref in extract_contract_refs(document, max_refs=MAX_LINK_FACTS, distinct="raw"):
        raw = string_at(document, ref.pointer)
        if raw is None or len(raw) > MAX_LINK_KEY_CHARS or not raw:
            continue
        if any(existing[0] == raw for existing in links):
            continue
        links.append((raw, ref.kind, ref.value))
    return tuple(links)


def _named_targets(
    links: tuple[tuple[str, str, str], ...], organization_id: uuid.UUID
) -> list[ColumnElement[bool]]:
    """The links' resolution rule as conditions on ``Contract`` (any state)."""
    uuids = sorted({uuid.UUID(value) for _, kind, value in links if kind == "contract_uuid"})
    odcs_ids = sorted({value for _, kind, value in links if kind == "odcs_id"})
    named: list[ColumnElement[bool]] = []
    if uuids:
        named.append(Contract.id.in_(uuids))
    if odcs_ids:
        named.append(
            and_(Contract.organization_id == organization_id, Contract.odcs_id.in_(odcs_ids))
        )
    return named


async def _facts_for(
    session: AsyncSession,
    links: tuple[tuple[str, str, str], ...],
    organization_id: uuid.UUID,
    metadata_scope: FolderScope,
    published_scope: FolderScope,
) -> dict[str, dict[str, Any]]:
    """The facts of ``links`` from the database, as the given scopes see them (no identity call)."""
    named = _named_targets(links, organization_id)
    visible_by_uuid: dict[uuid.UUID, Contract] = {}
    visible_by_odcs: dict[str, Contract] = {}
    states: dict[uuid.UUID, list[tuple[Any, str]]] = defaultdict(list)
    metadata_clause = scope_clause(
        metadata_scope, folder_col=Contract.folder_id, ancestors_col=Contract.ancestor_ids
    )
    published_clause = scope_clause(
        published_scope, folder_col=Contract.folder_id, ancestors_col=Contract.ancestor_ids
    )
    readable = []
    if metadata_clause is not None:
        readable.append(metadata_clause)
    if published_clause is not None:
        readable.append(
            and_(
                published_clause,
                exists().where(
                    ContractVersion.contract_id == Contract.id,
                    ContractVersion.state == "published",
                ),
            )
        )
    if readable and named:
        contracts = (
            (
                await session.execute(
                    select(Contract).where(
                        Contract.deleted_at.is_(None), or_(*named), or_(*readable)
                    )
                )
            )
            .scalars()
            .all()
        )
        for found in contracts:
            visible_by_uuid[found.id] = found
            if found.organization_id == organization_id:
                visible_by_odcs[found.odcs_id] = found
        if contracts:
            rows = (
                await session.execute(
                    select(
                        ContractVersion.contract_id,
                        ContractVersion.state,
                        ContractVersion.semver,
                        ContractVersion.semver_major,
                        ContractVersion.semver_minor,
                        ContractVersion.semver_patch,
                        ContractVersion.semver_prerelease,
                    ).where(
                        ContractVersion.contract_id.in_(sorted(visible_by_uuid)),
                        ContractVersion.state != "draft",
                    )
                )
            ).all()
            for row in rows:
                states[row.contract_id].append((parts_of(row), row.state))

    facts: dict[str, dict[str, Any]] = {}
    for raw, kind, value in links:
        pinned = pinned_version_of(raw)
        target = (
            visible_by_uuid.get(uuid.UUID(value))
            if kind == "contract_uuid"
            else visible_by_odcs.get(value)
        )
        if target is None:
            # Missing, soft-deleted and unreadable are one answer (no existence oracle, D-54).
            facts[raw] = {"state": _NOT_FOUND, "pinned_version": pinned, "pinned_published": None}
            continue
        rows_of = states.get(target.id, [])
        published = any(state == "published" for _, state in rows_of)
        pinned_published: bool | None = None
        if pinned is not None:
            wanted = parse_semver(pinned)
            pinned_published = wanted is not None and any(
                state == "published" and compare_semver(parts, wanted) == 0
                for parts, state in rows_of
            )
        facts[raw] = {
            "state": "published" if published else "unpublished_or_draft_only",
            "pinned_version": pinned,
            "pinned_published": pinned_published,
        }
    return facts


async def resolve_link_snapshot(
    session: AsyncSession,
    authorizer: Authorizer,
    principal: Principal,
    *,
    organization_id: uuid.UUID,
    document: Any,
    strict: bool = False,
) -> LinkFacts:
    """The caller's link facts with the inputs a publish re-checks under its lock (IN-03).

    An authorizer outage leaves no link to tell apart. Advisory runs (``strict=False``) then send
    no facts, so every link stays ``unverifiable`` (which can never fail a rule). The publish gate
    (``strict=True``) must not pass on that, so the outage is 503 ``service-unavailable`` (WR-02).
    """
    links = _document_links(document)
    if not links:
        return LinkFacts(facts={})
    try:
        metadata_scope = await authorizer.accessible_folders(principal, "contract:read-metadata")
        published_scope = await authorizer.accessible_folders(principal, "contract:read-published")
    except AuthorizerUnavailable as exc:
        _log.warning("link_facts_authorizer_unavailable", strict=strict)
        if strict:
            raise ProblemError(
                503,
                "service-unavailable",
                "Service Unavailable",
                "Authorization is temporarily unavailable.",
            ) from exc
        return LinkFacts(facts={})
    facts = await _facts_for(session, links, organization_id, metadata_scope, published_scope)
    return LinkFacts(
        facts=facts,
        organization_id=organization_id,
        links=links,
        metadata_scope=metadata_scope,
        published_scope=published_scope,
    )


async def resolve_link_facts(
    session: AsyncSession,
    authorizer: Authorizer,
    principal: Principal,
    *,
    organization_id: uuid.UUID,
    document: Any,
    strict: bool = False,
) -> dict[str, dict[str, Any]]:
    """``{link as written: {state, pinned_version, pinned_published}}`` for the caller."""
    snapshot = await resolve_link_snapshot(
        session,
        authorizer,
        principal,
        organization_id=organization_id,
        document=document,
        strict=strict,
    )
    return snapshot.facts


async def recheck_link_facts(session: AsyncSession, snapshot: LinkFacts) -> bool:
    """Whether the facts still hold, re-read under the publish lock (IN-03); no identity call.

    Call it after ``index_references`` has locked the live target contracts ``FOR KEY SHARE``.
    It first locks every non-draft version row of those targets ``FOR KEY SHARE`` (by id): an
    unpublish, republish, promote or deprecate of a target holds ``FOR UPDATE`` on its version
    row, so it either committed before (and is seen here) or waits until this publish commits.
    The facts are then recomputed with the scopes captured before the gate and compared link by
    link. Only a fact that got *worse* answers ``False`` (``published`` >
    ``unpublished_or_draft_only`` > ``not_found``, and a pinned version that was published and no
    longer is): validation passed
    with the old facts, and a worse fact is the one way a stored verdict could claim more than the
    truth. A fact that got better (a target published or created meanwhile, for example by a
    concurrent publish of a contract that links back) leaves the verdict stricter than the truth,
    so it is accepted; refusing it would make mutually linking publishes refuse each other.
    """
    if not snapshot.links:
        return True
    if (
        snapshot.organization_id is None
        or snapshot.metadata_scope is None
        or snapshot.published_scope is None
    ):  # pragma: no cover - a snapshot with links always carries its inputs
        return False
    named = _named_targets(snapshot.links, snapshot.organization_id)
    if named:
        targets = select(Contract.id).where(Contract.deleted_at.is_(None), or_(*named))
        await session.execute(
            select(ContractVersion.id)
            .where(ContractVersion.contract_id.in_(targets), ContractVersion.state != "draft")
            .order_by(ContractVersion.id)
            .with_for_update(read=True, key_share=True)
        )
    again = await _facts_for(
        session,
        snapshot.links,
        snapshot.organization_id,
        snapshot.metadata_scope,
        snapshot.published_scope,
    )
    return not any(_worse(snapshot.facts[raw], again.get(raw)) for raw in snapshot.facts)


_STATE_RANK = {_NOT_FOUND: 0, "unpublished_or_draft_only": 1, "published": 2}


def _worse(then: dict[str, Any], now: dict[str, Any] | None) -> bool:
    """Whether the fact ``now`` is weaker than the fact ``then`` validation judged."""
    if now is None:  # pragma: no cover - the same links are always recomputed
        return True
    if _STATE_RANK.get(now["state"], 0) < _STATE_RANK.get(then["state"], 0):
        return True
    return bool(then.get("pinned_published") is True and now.get("pinned_published") is not True)


LinkFactsResolver = Callable[[AsyncSession], Awaitable[LinkFacts]]


def draft_link_resolver(
    authorizer: Authorizer,
    principal: Principal,
    contract_id: uuid.UUID,
    draft_id: uuid.UUID,
    *,
    strict: bool = False,
) -> LinkFactsResolver:
    """A resolver for the stored draft's links, for the unlocked read block of a run.

    It reads the draft's parsed document and the contract's Organization in the session it is
    given, so it runs next to the baseline and attachment reads and holds no lock. The publish
    gate passes ``strict=True``: an identity outage is then a 503, never unverifiable links (WR-02).
    """

    async def _resolve(session: AsyncSession) -> LinkFacts:
        row = (
            await session.execute(
                select(Contract.organization_id, ContractVersion.document)
                .join(ContractVersion, ContractVersion.contract_id == Contract.id)
                .where(Contract.id == contract_id, ContractVersion.id == draft_id)
            )
        ).first()
        if row is None:
            return LinkFacts(facts={})
        return await resolve_link_snapshot(
            session,
            authorizer,
            principal,
            organization_id=row.organization_id,
            document=row.document,
            strict=strict,
        )

    return _resolve


__all__ = [
    "MAX_LINK_FACTS",
    "LinkFacts",
    "LinkFactsResolver",
    "draft_link_resolver",
    "pinned_version_of",
    "recheck_link_facts",
    "resolve_link_facts",
    "resolve_link_snapshot",
]
