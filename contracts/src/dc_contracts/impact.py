"""Rule impact: keyset pages of in-scope contracts for validation's batch routes (D-158 .. D-161).

Contracts holds the documents and validation holds the rules, and validation never calls contracts
(D-125). So contracts lists the next page of in-scope contracts, sends their Latest documents to
validation in one batch with the caller's own bearer, and shapes the answer:

* a rule-set dry-run (RULE-10, D-159): contracts under the rule set's folder (optionally narrowed
  to a folder below it) that the caller's ``ruleset:dry-run`` scope covers;
* compliance drift (RULE-11, D-160): contracts under the query folder that the caller's
  ``governance:drift-read`` scope covers.

A candidate is a non-deleted contract with a Latest version whose ``ancestor_ids`` (root-first,
the contract's own folder included) contain every scope folder. Pages are ordered by contract id
(a uuidv7) and the cursor is the last id of the page, so walking every ``next_cursor`` visits each
contract once; a tampered cursor only moves the start, because the scope is in the WHERE clause.
The database session is closed before validation is called: no transaction is held while a page
is evaluated. Results never carry document content: metadata, outcomes, rule keys and the rule
author's messages only (D-36). JSON Pointers (where a rule's target matched) are content-derived:
a crafted target such as ``$..[?search(@, '^AKIA')]`` turns them into a map of matching values. So
they are returned only for contracts inside the caller's ``contract:read`` scope (asked of the PDP,
an outage is 503); for every other contract each failing rule keeps its verdict and keys but its
``pointers`` are emptied, ``pointers_total`` is 0 and ``pointers_withheld`` is true (CR-01). The
per-contract verdict itself stays a one-bit oracle for an author-controlled rule (AR-6-03); the
routes audit every call (``api/governance.py``). Nothing here writes: a page is a read (D-161).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from dc_auth.authorizer import Authorizer, AuthorizerUnavailable, FolderScope
from dc_auth.principal import Principal
from dc_core.problems import ProblemError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dc_contracts.models import Contract, ContractVersion
from dc_contracts.search import decode_cursor, encode_cursor, scope_clause
from dc_contracts.settings import ContractsSettings
from dc_contracts.validation_client import ValidationClient, unavailable

_log = structlog.get_logger("dc_contracts.impact")

# -- wire models (validation's answers, read leniently but typed) ---------------------------------


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ContractsImpactCountsOut(BaseModel):
    """Failing rules of one contract: blocking fails, blocking errors and non-blocking ones."""

    fail: int
    error: int
    warn: int


class ContractsImpactFailingRuleOut(BaseModel):
    """One rule a contract did not pass: keys, severity, fixed texts and pointers, no values.

    ``pointers_withheld`` is true when the caller may not read the contract's content: the
    pointers are then emptied and ``pointers_total`` is 0.
    """

    rule_set_id: uuid.UUID
    version_no: int | None
    mode: str
    rule_key: str
    severity: str
    outcome: str
    message: str
    error_code: str | None
    blocking: bool
    pointers: list[str]
    pointers_total: int
    pointers_withheld: bool = False


class _DryRunItem(_Wire):
    contract_id: uuid.UUID
    status: Literal["evaluated", "unparseable"]
    would_block: bool
    counts: ContractsImpactCountsOut
    failing_rules: list[ContractsImpactFailingRuleOut]


class _DryRunAnswer(_Wire):
    items: list[_DryRunItem]


class ContractsDriftVersionOut(BaseModel):
    """A mandatory rule-set version that currently applies at the contract's folder."""

    rule_set_id: uuid.UUID
    name: str
    version_no: int
    mode: str


class _DriftItem(_Wire):
    contract_id: uuid.UUID
    status: Literal["drifted", "at_risk", "compliant", "unparseable"]
    counts: ContractsImpactCountsOut
    failing_rules: list[ContractsImpactFailingRuleOut]
    current_versions: list[ContractsDriftVersionOut]


class _DriftAnswer(_Wire):
    items: list[_DriftItem]


# -- public response models -----------------------------------------------------------------------


class ContractsImpactContractOut(BaseModel):
    """Who an impact row is about: metadata only, never document content (D-36)."""

    contract_id: uuid.UUID
    name: str | None
    odcs_id: str
    folder_id: uuid.UUID
    latest_semver: str | None


class ContractsDryRunItemOut(ContractsImpactContractOut):
    status: Literal["evaluated", "unparseable"]
    would_block: bool
    counts: ContractsImpactCountsOut
    failing_rules: list[ContractsImpactFailingRuleOut]


class ContractsDryRunSummaryOut(BaseModel):
    """Page totals; ``hidden`` counts contracts left out of ``items`` (outcomes still count)."""

    model_config = ConfigDict(
        serialize_by_alias=True, validate_by_alias=True, validate_by_name=True
    )

    would_fail: int
    would_error: int
    pass_: int = Field(alias="pass")
    hidden: int


class ContractsRuleSetDryRunOut(BaseModel):
    rule_set_id: uuid.UUID
    version: int | Literal["draft"]
    scope_folder_id: uuid.UUID
    items: list[ContractsDryRunItemOut]
    summary: ContractsDryRunSummaryOut
    next_cursor: str | None
    total_in_scope: int


class ContractsDriftPinOut(BaseModel):
    """One rule-set version the contract's Latest was published against."""

    model_config = ConfigDict(extra="ignore")

    rule_set_id: uuid.UUID | None = None
    name: str | None = None
    folder_id: uuid.UUID | None = None
    scope: str | None = None
    version: int | None = None
    source: str | None = None
    mode: str | None = None
    dsl_version: str | None = None
    # True for a pin taken while its platform rule set was paused (warn only).
    paused: bool | None = None


class ContractsDriftItemOut(ContractsImpactContractOut):
    status: Literal["drifted", "at_risk", "unparseable"]
    counts: ContractsImpactCountsOut
    failing_rules: list[ContractsImpactFailingRuleOut]
    pinned_rule_sets: list[ContractsDriftPinOut]
    current_versions: list[ContractsDriftVersionOut]


class ContractsDriftSummaryOut(BaseModel):
    """Page totals; ``hidden`` counts contracts whose names the caller may not read."""

    drifted: int
    at_risk: int
    compliant: int
    unparseable: int
    hidden: int


class ContractsDriftOut(BaseModel):
    folder_id: uuid.UUID
    items: list[ContractsDriftItemOut]
    summary: ContractsDriftSummaryOut
    next_cursor: str | None
    total_in_scope: int


# -- paging ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PageLimits:
    """How large one page sent to validation may be (D-161; validation's batch caps)."""

    size: int
    max_folders: int
    max_bytes: int

    @classmethod
    def of(cls, settings: ContractsSettings) -> PageLimits:
        return cls(
            size=settings.impact_page_size,
            max_folders=settings.impact_page_max_folders,
            max_bytes=settings.impact_page_max_bytes,
        )


@dataclass(frozen=True)
class Candidate:
    """A contract on the page and the Latest version that is evaluated."""

    contract_id: uuid.UUID
    folder_id: uuid.UUID
    ancestor_ids: tuple[uuid.UUID, ...]
    odcs_id: str
    name: str | None
    semver: str | None
    media_type: str
    version_id: uuid.UUID
    size_bytes: int
    pinned_rule_sets: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Page:
    candidates: list[Candidate]
    documents: dict[uuid.UUID, bytes]
    next_cursor: str | None
    total_in_scope: int


def parse_cursor(raw: str | None, pointer: str) -> uuid.UUID | None:
    """The cursor's contract id; a malformed cursor is a 422 at ``pointer``."""
    if raw is None:
        return None
    try:
        return decode_cursor(raw)
    except ProblemError:
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "The request failed validation.",
            errors=[{"pointer": pointer, "detail": "cursor is not valid"}],
        ) from None


def candidate_where(
    scope: FolderScope, under: Sequence[uuid.UUID]
) -> list[ColumnElement[bool]] | None:
    """The candidate filter, or None when the caller's scope is empty (no query at all)."""
    visible = scope_clause(
        scope, folder_col=Contract.folder_id, ancestors_col=Contract.ancestor_ids
    )
    if visible is None:
        return None
    return [
        visible,
        Contract.deleted_at.is_(None),
        Contract.latest_version_id.is_not(None),
        Contract.ancestor_ids.contains(list(dict.fromkeys(under))),
    ]


def cut_page(candidates: Sequence[Candidate], limits: PageLimits) -> list[Candidate]:
    """The leading candidates that fit the count, folder and byte budgets; never empty if any.

    The first candidate is always taken, so a single large document still makes progress (a
    stored document is at most ``max_document_bytes``, far below validation's byte cap).
    """
    chosen: list[Candidate] = []
    folders: set[uuid.UUID] = set()
    total = 0
    for candidate in candidates[: limits.size]:
        if chosen and (
            total + candidate.size_bytes > limits.max_bytes
            or len(folders | {candidate.folder_id}) > limits.max_folders
        ):
            break
        chosen.append(candidate)
        folders.add(candidate.folder_id)
        total += candidate.size_bytes
    return chosen


async def _load_page(
    session: AsyncSession,
    where: list[ColumnElement[bool]],
    after: uuid.UUID | None,
    limits: PageLimits,
) -> Page:
    total = (
        await session.execute(select(func.count()).select_from(Contract).where(*where))
    ).scalar_one()
    stmt = (
        select(
            Contract.id,
            Contract.folder_id,
            Contract.ancestor_ids,
            Contract.odcs_id,
            ContractVersion.name,
            ContractVersion.semver,
            ContractVersion.media_type,
            ContractVersion.id,
            func.octet_length(ContractVersion.raw_document),
            ContractVersion.pinned_rule_sets,
        )
        .join(ContractVersion, ContractVersion.id == Contract.latest_version_id)
        .where(*where)
    )
    if after is not None:
        stmt = stmt.where(Contract.id > after)
    # limit + 1: the extra row only says "there is a next page". Sizes come from octet_length, so
    # no document is read before the page is cut.
    rows = (await session.execute(stmt.order_by(Contract.id).limit(limits.size + 1))).all()
    listed = [
        Candidate(
            contract_id=row[0],
            folder_id=row[1],
            ancestor_ids=tuple(row[2]),
            odcs_id=row[3],
            name=row[4],
            semver=row[5],
            media_type=row[6],
            version_id=row[7],
            size_bytes=int(row[8]),
            pinned_rule_sets=tuple(row[9] or ()),
        )
        for row in rows
    ]
    chosen = cut_page(listed, limits)
    documents: dict[uuid.UUID, bytes] = {}
    if chosen:
        loaded = await session.execute(
            select(ContractVersion.id, ContractVersion.raw_document).where(
                ContractVersion.id.in_([c.version_id for c in chosen])
            )
        )
        documents = {version_id: bytes(raw) for version_id, raw in loaded.all()}
    has_more = len(listed) > len(chosen)
    return Page(
        candidates=chosen,
        documents=documents,
        next_cursor=encode_cursor(chosen[-1].contract_id) if has_more and chosen else None,
        total_in_scope=int(total),
    )


async def load_page(
    session_factory: async_sessionmaker[AsyncSession],
    where: list[ColumnElement[bool]] | None,
    after: uuid.UUID | None,
    limits: PageLimits,
) -> Page:
    """The next page in a short read session that is closed before the caller goes on."""
    if where is None:
        return Page(candidates=[], documents={}, next_cursor=None, total_in_scope=0)
    async with session_factory() as session:
        page = await _load_page(session, where, after, limits)
        await session.rollback()
    return page


def batch_items(page: Page) -> list[dict[str, Any]]:
    """Validation's batch item shape: ids, the stored media type and the exact stored bytes."""
    return [
        {
            "contract_id": str(c.contract_id),
            "folder_id": str(c.folder_id),
            "media_type": c.media_type,
            "document_b64": base64.b64encode(page.documents[c.version_id]).decode("ascii"),
        }
        for c in page.candidates
    ]


async def scope_for(authorizer: Authorizer, principal: Principal, permission: str) -> FolderScope:
    """The caller's compact scope for ``permission``; an identity outage is 503."""
    try:
        return await authorizer.accessible_folders(principal, permission)
    except AuthorizerUnavailable as exc:
        raise ProblemError(
            503,
            "service-unavailable",
            "Service Unavailable",
            "Authorization is temporarily unavailable.",
        ) from exc


def _by_contract[T: _DryRunItem | _DriftItem](items: Sequence[T], page: Page) -> dict[uuid.UUID, T]:
    """Validation's answer keyed by contract; any other set of contracts than sent is a 503."""
    answered = {item.contract_id: item for item in items}
    if len(answered) != len(items) or set(answered) != {c.contract_id for c in page.candidates}:
        _log.warning("validation_answer_mismatch")
        raise unavailable("Contract validation answered for different contracts than were sent.")
    return answered


def visible_rules(
    rules: Sequence[ContractsImpactFailingRuleOut],
    content: FolderScope,
    candidate: Candidate,
) -> list[ContractsImpactFailingRuleOut]:
    """The failing rules as the caller may see them: pointers only with ``contract:read`` (CR-01).

    A pointer says where a rule's target matched, so with an author-controlled target it locates
    values of the document; only a caller who may read that content gets them.
    """
    if content.covers(candidate.folder_id, candidate.ancestor_ids):
        return list(rules)
    return [
        rule.model_copy(update={"pointers": [], "pointers_total": 0, "pointers_withheld": True})
        for rule in rules
    ]


def _contract_fields(candidate: Candidate) -> dict[str, Any]:
    return {
        "contract_id": candidate.contract_id,
        "name": candidate.name,
        "odcs_id": candidate.odcs_id,
        "folder_id": candidate.folder_id,
        "latest_semver": candidate.semver,
    }


# -- dry-run --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DryRunRequest:
    rule_set_id: uuid.UUID
    version: int | Literal["draft"]
    rule_set_folder_id: uuid.UUID
    rule_set_ancestor_ids: tuple[uuid.UUID, ...]
    folder_id: uuid.UUID | None
    cursor: uuid.UUID | None


def _empty_dry_run(req: DryRunRequest, scope_folder_id: uuid.UUID) -> ContractsRuleSetDryRunOut:
    return ContractsRuleSetDryRunOut(
        rule_set_id=req.rule_set_id,
        version=req.version,
        scope_folder_id=scope_folder_id,
        items=[],
        summary=ContractsDryRunSummaryOut(would_fail=0, would_error=0, pass_=0, hidden=0),
        next_cursor=None,
        total_in_scope=0,
    )


async def dry_run_page(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: ContractsSettings,
    client: ValidationClient,
    authorizer: Authorizer,
    principal: Principal,
    bearer: str,
    req: DryRunRequest,
) -> ContractsRuleSetDryRunOut:
    """One dry-run page (D-159): the next in-scope contracts, evaluated as if enforced."""
    scope_folder_id = req.folder_id or req.rule_set_folder_id
    # A narrowing folder above the rule set's folder is outside its subtree, like a sibling.
    if req.folder_id is not None and (
        req.folder_id != req.rule_set_folder_id and req.folder_id in req.rule_set_ancestor_ids
    ):
        return _empty_dry_run(req, scope_folder_id)
    scope = await scope_for(authorizer, principal, "ruleset:dry-run")
    under = [req.rule_set_folder_id] + ([req.folder_id] if req.folder_id is not None else [])
    page = await load_page(
        session_factory, candidate_where(scope, under), req.cursor, PageLimits.of(settings)
    )
    if not page.candidates:
        return _empty_dry_run(req, scope_folder_id).model_copy(
            update={"total_in_scope": page.total_in_scope}
        )
    raw = await client.dry_run(
        req.rule_set_id, req.version, scope_folder_id, batch_items(page), bearer
    )
    try:
        answer = _DryRunAnswer.model_validate(raw)
    except ValidationError as exc:
        _log.warning("validation_bad_answer")
        raise unavailable("Contract validation answered with an unreadable response.") from exc
    results = _by_contract(answer.items, page)
    metadata = await scope_for(authorizer, principal, "contract:read-metadata")
    content = await scope_for(authorizer, principal, "contract:read")
    items: list[ContractsDryRunItemOut] = []
    would_fail = would_error = passed = hidden = 0
    for candidate in page.candidates:
        result = results[candidate.contract_id]
        if result.counts.fail > 0:
            would_fail += 1
        elif result.would_block:
            would_error += 1
        else:
            passed += 1
        if not metadata.covers(candidate.folder_id, candidate.ancestor_ids):
            hidden += 1  # A13: counted, never named
            continue
        items.append(
            ContractsDryRunItemOut(
                **_contract_fields(candidate),
                status=result.status,
                would_block=result.would_block,
                counts=result.counts,
                failing_rules=visible_rules(result.failing_rules, content, candidate),
            )
        )
    return ContractsRuleSetDryRunOut(
        rule_set_id=req.rule_set_id,
        version=req.version,
        scope_folder_id=scope_folder_id,
        items=items,
        summary=ContractsDryRunSummaryOut(
            would_fail=would_fail, would_error=would_error, pass_=passed, hidden=hidden
        ),
        next_cursor=page.next_cursor,
        total_in_scope=page.total_in_scope,
    )


# -- drift ----------------------------------------------------------------------------------------


def _pins(candidate: Candidate) -> list[ContractsDriftPinOut]:
    return [ContractsDriftPinOut.model_validate(pin) for pin in candidate.pinned_rule_sets]


async def drift_page(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: ContractsSettings,
    client: ValidationClient,
    authorizer: Authorizer,
    principal: Principal,
    bearer: str,
    folder_id: uuid.UUID,
    cursor: uuid.UUID | None,
) -> ContractsDriftOut:
    """One compliance-drift page (D-160): drifted and at-risk contracts listed, the rest counted.

    Validation evaluates each Latest document against the current mandatory resolution of its own
    folder; contracts adds the metadata and the version's pinned rule sets. Nothing is written.
    """
    scope = await scope_for(authorizer, principal, "governance:drift-read")
    page = await load_page(
        session_factory, candidate_where(scope, [folder_id]), cursor, PageLimits.of(settings)
    )
    tally = {"drifted": 0, "at_risk": 0, "compliant": 0, "unparseable": 0}
    if not page.candidates:
        return ContractsDriftOut(
            folder_id=folder_id,
            items=[],
            summary=ContractsDriftSummaryOut(**tally, hidden=0),
            next_cursor=None,
            total_in_scope=page.total_in_scope,
        )
    raw = await client.drift(folder_id, batch_items(page), bearer)
    try:
        answer = _DriftAnswer.model_validate(raw)
    except ValidationError as exc:
        _log.warning("validation_bad_answer")
        raise unavailable("Contract validation answered with an unreadable response.") from exc
    results = _by_contract(answer.items, page)
    metadata = await scope_for(authorizer, principal, "contract:read-metadata")
    content = await scope_for(authorizer, principal, "contract:read")
    items: list[ContractsDriftItemOut] = []
    hidden = 0
    for candidate in page.candidates:
        result = results[candidate.contract_id]
        tally[result.status] += 1
        if not metadata.covers(candidate.folder_id, candidate.ancestor_ids):
            hidden += 1  # A13: counted, never named
            continue
        if result.status == "compliant":
            continue
        items.append(
            ContractsDriftItemOut(
                **_contract_fields(candidate),
                status=result.status,
                counts=result.counts,
                failing_rules=visible_rules(result.failing_rules, content, candidate),
                pinned_rule_sets=_pins(candidate),
                current_versions=result.current_versions,
            )
        )
    return ContractsDriftOut(
        folder_id=folder_id,
        items=items,
        summary=ContractsDriftSummaryOut(**tally, hidden=hidden),
        next_cursor=page.next_cursor,
        total_in_scope=page.total_in_scope,
    )


__all__ = [
    "Candidate",
    "ContractsDriftItemOut",
    "ContractsDriftOut",
    "ContractsDriftSummaryOut",
    "ContractsDryRunItemOut",
    "ContractsDryRunSummaryOut",
    "ContractsRuleSetDryRunOut",
    "DryRunRequest",
    "Page",
    "PageLimits",
    "batch_items",
    "candidate_where",
    "cut_page",
    "drift_page",
    "dry_run_page",
    "load_page",
    "parse_cursor",
    "scope_for",
    "visible_rules",
]
