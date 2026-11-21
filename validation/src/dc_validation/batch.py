"""Batch evaluation of a page of documents for rule impact (D-158, D-159, D-160, D-161).

Contracts holds the documents and validation must never call contracts (D-125), so contracts
pages through the contracts in scope and sends each page here with the caller's own token. This
module evaluates a page in two ways:

* **dry-run** (RULE-10, D-159): one rule-set version, a published version number or the draft,
  evaluated against every document *as if it were enforced* (source ``mandatory``, mode
  ``enforce``);
* **drift** (RULE-11, D-160): the current mandatory resolution of each document's own folder
  (``load_mandatory`` plus ``resolve_mandatory`` at the database clock; no attachments, no
  compatibility), classified ``drifted``, ``at_risk`` or ``compliant``.

Folder facts never come from the body (D-158, Open Question 4): every distinct item folder is
re-authorised by a PDP check of the route's permission with the caller's token and must lie inside
the scope folder's subtree, otherwise the whole call is the uniform 404.

Nothing here writes: no stored run, no audit event, no notice. Results carry ids, counts, rule
keys, severities, the rule author's own message (or a fixed text per error code) and at most
``MAX_POINTERS`` JSON Pointers per rule, never a document value (D-36, T-6-30). Contracts passes
the pointers on only to callers who may read the contract's content (CR-01).

A page cannot starve save and publish runs (WR-03): the batch pool is smaller than the worker
pool (``ValidationSettings.batch_slot_count``), and before each document the page checks its
wall-clock budget (``batch_page_budget_s``) and whether the caller is still connected; past the
budget or after a disconnect it stops and answers 503 ``validation-unavailable``, so a page never
keeps evaluating after contracts has given up on it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from dc_auth.authorizer import AuthorizerUnavailable, FolderFacts, Outcome
from dc_auth.principal import Principal
from dc_core.problems import ProblemError
from dc_rules.model import DSL_VERSION, RuleDef
from dc_rules.ports import ERROR_MESSAGES, ErrorCode
from dc_rules.protocol import EvalUnit, UnitResult
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dc_validation.facts import build_refs
from dc_validation.ingest import parse_document
from dc_validation.models import RuleSet, RuleSetVersion
from dc_validation.report import RuleBinding, is_blocking
from dc_validation.resolution import Mode, ResolvedRuleSet, resolve_mandatory
from dc_validation.rulesets import _unavailable, db_now, load_mandatory, not_found
from dc_validation.runs import _evaluate, _rule_bodies, _units, rule_ctx
from dc_validation.sandbox import Sandbox, SandboxBusy
from dc_validation.settings import ValidationSettings

# At most this many JSON Pointers are listed per failing rule; ``pointers_total`` has the count.
MAX_POINTERS = 20

# A synthetic, stable version id for a draft, so its units are told apart from published ones.
_DRAFT_NAMESPACE = uuid.UUID("6f1c2f0e-6a43-4c55-9d0c-6b1f3c7e2d11")


# -- wire models ----------------------------------------------------------------------------------


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationBatchItemIn(_In):
    """One document of a page: its contract, the folder contracts says it lives in, the bytes."""

    contract_id: uuid.UUID
    folder_id: uuid.UUID
    media_type: str = Field(max_length=200)
    document_b64: str


# A published version number (the column is a 32-bit integer) or the draft.
_VersionNo = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]


class ValidationDryRunIn(_In):
    """A dry-run page (D-159): the version, the scope folder and the documents."""

    version: _VersionNo | Literal["draft"]
    scope_folder_id: uuid.UUID
    items: list[ValidationBatchItemIn]


class ValidationBatchCountsOut(_Out):
    """Failing rules of one document: blocking fails, blocking errors and non-blocking ones."""

    fail: int
    error: int
    warn: int


class ValidationFailingRuleOut(_Out):
    """One rule a document did not pass: keys, severity, fixed texts and pointers, no values."""

    rule_set_id: uuid.UUID
    version_no: int | None  # None for a draft
    mode: Mode
    rule_key: str
    severity: Literal["error", "warning", "info"]
    outcome: Literal["fail", "error"]
    message: str
    error_code: ErrorCode | None
    blocking: bool
    pointers: list[str]
    pointers_total: int


class ValidationDryRunItemOut(_Out):
    contract_id: uuid.UUID
    status: Literal["evaluated", "unparseable"]
    would_block: bool
    counts: ValidationBatchCountsOut
    failing_rules: list[ValidationFailingRuleOut]


class ValidationDryRunTotalsOut(_Out):
    """Page totals: items that would fail, would block only on errors, and that pass."""

    model_config = ConfigDict(
        extra="forbid", serialize_by_alias=True, validate_by_alias=True, validate_by_name=True
    )

    would_fail: int
    would_error: int
    pass_: int = Field(alias="pass")


class ValidationDryRunOut(_Out):
    rule_set_id: uuid.UUID
    version: int | Literal["draft"]
    items: list[ValidationDryRunItemOut]
    totals: ValidationDryRunTotalsOut


class ValidationDriftIn(_In):
    """A drift page (D-160): the documents, each resolved for its own folder."""

    items: list[ValidationBatchItemIn]


class ValidationDriftVersionOut(_Out):
    """A mandatory rule-set version that currently applies at an item's folder."""

    rule_set_id: uuid.UUID
    name: str
    version_no: int
    mode: Mode


class ValidationDriftItemOut(_Out):
    contract_id: uuid.UUID
    folder_id: uuid.UUID
    status: Literal["drifted", "at_risk", "compliant", "unparseable"]
    counts: ValidationBatchCountsOut
    failing_rules: list[ValidationFailingRuleOut]
    current_versions: list[ValidationDriftVersionOut]


class ValidationDriftTotalsOut(_Out):
    drifted: int
    at_risk: int
    compliant: int
    unparseable: int


class ValidationDriftOut(_Out):
    folder_id: uuid.UUID
    items: list[ValidationDriftItemOut]
    totals: ValidationDriftTotalsOut


# -- decoding -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchItem:
    """A decoded item: raw document bytes, not base64."""

    contract_id: uuid.UUID
    folder_id: uuid.UUID
    media_type: str
    raw: bytes


def _invalid(pointer: str, detail: str) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Validation error",
        "The request did not validate.",
        errors=[{"pointer": pointer, "detail": detail}],
    )


def _over_cap(detail: str) -> ProblemError:
    return _invalid("/items", detail)


def decode_items(
    items: Sequence[ValidationBatchItemIn], settings: ValidationSettings
) -> list[BatchItem]:
    """Enforce the page caps, then decode every item's base64 (D-161).

    Over ``max_batch_items`` documents, ``max_batch_folders`` distinct folders or
    ``max_batch_bytes`` decoded bytes is a 422 with a fixed text; a bad base64 value is a 422
    naming its position. Nothing submitted is echoed, and nothing is evaluated before this passes.
    """
    if len(items) > settings.max_batch_items:
        raise _over_cap("Too many documents in one page.")
    if len({item.folder_id for item in items}) > settings.max_batch_folders:
        raise _over_cap("Too many folders in one page.")
    decoded: list[BatchItem] = []
    total = 0
    for index, item in enumerate(items):
        try:
            raw = base64.b64decode(item.document_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _invalid(
                f"/items/{index}/document_b64", "document_b64 is not valid base64."
            ) from exc
        total += len(raw)
        if total > settings.max_batch_bytes:
            raise _over_cap("The documents of one page exceed the size limit.")
        decoded.append(
            BatchItem(
                contract_id=item.contract_id,
                folder_id=item.folder_id,
                media_type=item.media_type,
                raw=raw,
            )
        )
    return decoded


# -- folder facts ---------------------------------------------------------------------------------


async def folder_facts(
    authorizer: Any,
    principal: Principal,
    permission: str,
    folder_ids: Sequence[uuid.UUID],
    scope_folder_id: uuid.UUID,
) -> dict[uuid.UUID, FolderFacts]:
    """Facts per distinct folder from the PDP (the caller's token), each inside the scope.

    A folder the caller may not use with ``permission``, or one outside ``scope_folder_id``'s
    subtree, makes the whole call the uniform 404: the body never widens the scope (T-6-31).
    """
    distinct = list(dict.fromkeys(folder_ids))
    try:
        decisions = await asyncio.gather(
            *(authorizer.check(principal, permission, fid, fresh=True) for fid in distinct)
        )
    except AuthorizerUnavailable as exc:
        raise ProblemError(
            503,
            "service-unavailable",
            "Service Unavailable",
            "Authorization is temporarily unavailable.",
        ) from exc
    facts: dict[uuid.UUID, FolderFacts] = {}
    for fid, decision in zip(distinct, decisions, strict=True):
        folder = decision.folder
        if (
            decision.outcome is not Outcome.ALLOW
            or folder is None
            or scope_folder_id not in folder.ancestor_ids
        ):
            raise not_found()
        facts[fid] = folder
    return facts


# -- evaluation -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Prepared:
    """The units of one resolution (per folder for drift, once for a dry-run)."""

    resolved: tuple[ResolvedRuleSet, ...]
    units: list[EvalUnit]
    bindings: dict[int, RuleBinding]


def _prepare(
    resolved: Sequence[ResolvedRuleSet],
    bodies: Mapping[uuid.UUID, list[RuleDef]],
    *,
    cacheable: bool = True,
) -> _Prepared:
    units, bindings = _units(resolved, dict(bodies))
    if not cacheable:
        # A draft changes under the same id: never let the worker reuse a compiled rule for it.
        units = [unit.model_copy(update={"cache_key": None}) for unit in units]
    return _Prepared(tuple(resolved), units, bindings)


def _pointers(result: UnitResult) -> tuple[list[str], int]:
    seen = list(dict.fromkeys(finding.pointer for finding in result.findings))
    if not seen:
        seen = [""]  # a verdict without a location is reported at the document root
    return seen[:MAX_POINTERS], len(seen)


def _verdicts(
    prepared: _Prepared, results: Mapping[int, UnitResult]
) -> list[ValidationFailingRuleOut]:
    """One entry per rule that did not pass, in evaluation order; a missing verdict is an error."""
    out: list[ValidationFailingRuleOut] = []
    for unit_id in sorted(prepared.bindings):
        binding = prepared.bindings[unit_id]
        result = results.get(unit_id) or UnitResult(
            id=unit_id, outcome="error", error_code="engine_error"
        )
        if result.outcome == "pass":
            continue
        resolved, rule = binding.resolved, binding.rule
        outcome: Literal["fail", "error"] = "fail" if result.outcome == "fail" else "error"
        code: ErrorCode | None = None
        if outcome == "error":
            first = next((f.error_code for f in result.findings if f.error_code), None)
            code = result.error_code or first or "engine_error"
            message = ERROR_MESSAGES[code]
        else:
            message = rule.message
        pointers, total = _pointers(result)
        out.append(
            ValidationFailingRuleOut(
                rule_set_id=resolved.rule_set_id,
                version_no=None if resolved.version_no == 0 else resolved.version_no,
                mode=resolved.mode,
                rule_key=rule.key,
                severity=rule.severity,
                outcome=outcome,
                message=message,
                error_code=code,
                blocking=is_blocking(rule.severity, resolved.source, resolved.mode),
                pointers=pointers,
                pointers_total=total,
            )
        )
    return out


def _counts(rules: Sequence[ValidationFailingRuleOut]) -> ValidationBatchCountsOut:
    fail = sum(1 for r in rules if r.severity == "error" and r.outcome == "fail")
    error = sum(1 for r in rules if r.severity == "error" and r.outcome == "error")
    return ValidationBatchCountsOut(fail=fail, error=error, warn=len(rules) - fail - error)


async def _evaluate_item(
    app_state: Any,
    principal: Principal,
    authorizer: Any,
    facts: FolderFacts,
    item: BatchItem,
    prepared: _Prepared,
) -> list[ValidationFailingRuleOut] | None:
    """The failing rules of one document; ``None`` when it does not parse (no content echoed)."""
    try:
        parsed = await parse_document(item.raw, item.media_type, app_state.settings)
    except ProblemError:
        return None
    if not prepared.units:
        return []
    refs = await build_refs(
        app_state, principal, facts, parsed.document, None, authorizer=authorizer
    )
    sandbox: Sandbox = app_state.sandbox
    results = await _evaluate(
        sandbox,
        prepared.units,
        parsed.document,
        # batch judges stored published versions as the publish gate would
        rule_ctx(facts, "publish"),
        None,
        refs,
    )
    return _verdicts(prepared, results)


Disconnected = Callable[[], Awaitable[bool]]


def page_stopped() -> ProblemError:
    """503 for a page stopped by its budget or a gone caller; nothing was answered (WR-03)."""
    return ProblemError(
        503,
        "validation-unavailable",
        "Service Unavailable",
        "The page could not be evaluated within its time budget; retry with fewer documents.",
        headers={"Retry-After": "1"},
    )


class PageClock:
    """The page's wall-clock budget and the caller's connection, checked before each document."""

    def __init__(self, budget_s: float, disconnected: Disconnected | None) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._deadline = loop.time() + budget_s
        self._disconnected = disconnected

    async def check(self) -> None:
        """Raise ``page_stopped`` when the budget is spent or the caller has gone."""
        if self._loop.time() >= self._deadline:
            raise page_stopped()
        if self._disconnected is not None and await self._disconnected():
            raise page_stopped()


@asynccontextmanager
async def _batch_slot(app_state: Any) -> AsyncIterator[None]:
    """Hold a batch slot for the page; a saturated batch pool is 503 with Retry-After (D-161)."""
    sandbox: Sandbox = app_state.sandbox
    try:
        async with sandbox.batch_slot():
            yield
    except SandboxBusy as exc:
        raise _unavailable(busy=True) from exc


# -- dry-run --------------------------------------------------------------------------------------


async def _load_dry_run_version(
    session: AsyncSession, rule_set_id: uuid.UUID, version: int | Literal["draft"]
) -> tuple[ResolvedRuleSet, list[RuleDef], bool]:
    """The chosen version as one enforced mandatory entry, its rules and whether it caches."""
    rule_set = (
        await session.execute(select(RuleSet).where(RuleSet.id == rule_set_id))
    ).scalar_one_or_none()
    if rule_set is None or rule_set.folder_id is None:
        raise not_found()
    if version == "draft":
        version_id = uuid.uuid5(_DRAFT_NAMESPACE, f"{rule_set.id}:{rule_set.draft_content_sha256}")
        rules_json = rule_set.draft_rules
        dsl_version: str = DSL_VERSION
        version_no = 0
        cacheable = False
    else:
        row = (
            await session.execute(
                select(RuleSetVersion).where(
                    RuleSetVersion.rule_set_id == rule_set_id,
                    RuleSetVersion.version_no == version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise not_found()
        version_id = row.id
        rules_json = row.rules
        dsl_version = row.dsl_version
        version_no = row.version_no
        cacheable = True
    resolved = ResolvedRuleSet(
        rule_set_id=rule_set.id,
        rule_set_version_id=version_id,
        name=rule_set.name,
        scope=rule_set.scope,
        folder_id=rule_set.folder_id,
        folder_kind=rule_set.folder_kind,
        depth=1,
        version_no=version_no,
        dsl_version=dsl_version,
        # D-159: evaluated as if it were enforced.
        source="mandatory",
        mode="enforce",
        enforce_at=None,
    )
    rules = sorted((RuleDef.model_validate(r) for r in rules_json), key=lambda r: r.key)
    return resolved, rules, cacheable


async def dry_run(
    app_state: Any,
    principal: Principal,
    authorizer: Any,
    *,
    rule_set_id: uuid.UUID,
    rule_set_folder_id: uuid.UUID,
    version: int | Literal["draft"],
    scope_folder_id: uuid.UUID,
    items: Sequence[BatchItem],
    disconnected: Disconnected | None = None,
) -> ValidationDryRunOut:
    """Evaluate one rule-set version as if enforced against each document of the page."""
    # The scope folder must itself be the rule set's folder or below it, and every item folder
    # must lie under the scope folder (and so under the rule set's folder).
    await folder_facts(
        authorizer, principal, "ruleset:dry-run", [scope_folder_id], rule_set_folder_id
    )
    facts = await folder_facts(
        authorizer, principal, "ruleset:dry-run", [i.folder_id for i in items], scope_folder_id
    )
    session_factory: async_sessionmaker[AsyncSession] = app_state.session_factory
    async with session_factory() as session:
        session.sync_session.info["read_only"] = True
        resolved, rules, cacheable = await _load_dry_run_version(session, rule_set_id, version)
    # The session is closed: no transaction is open while the sandbox evaluates (D-137).
    prepared = _prepare([resolved], {resolved.rule_set_version_id: rules}, cacheable=cacheable)

    out: list[ValidationDryRunItemOut] = []
    would_fail = would_error = passed = 0
    async with _batch_slot(app_state):
        clock = PageClock(app_state.settings.batch_page_budget_s, disconnected)
        for item in items:
            await clock.check()
            rules_out = await _evaluate_item(
                app_state, principal, authorizer, facts[item.folder_id], item, prepared
            )
            if rules_out is None:
                # Fail closed: a document that cannot be evaluated is counted as an error.
                would_error += 1
                out.append(
                    ValidationDryRunItemOut(
                        contract_id=item.contract_id,
                        status="unparseable",
                        would_block=True,
                        counts=ValidationBatchCountsOut(fail=0, error=0, warn=0),
                        failing_rules=[],
                    )
                )
                continue
            counts = _counts(rules_out)
            would_block = any(r.blocking for r in rules_out)
            if counts.fail:
                would_fail += 1
            elif would_block:
                would_error += 1
            else:
                passed += 1
            out.append(
                ValidationDryRunItemOut(
                    contract_id=item.contract_id,
                    status="evaluated",
                    would_block=would_block,
                    counts=counts,
                    failing_rules=rules_out,
                )
            )
    return ValidationDryRunOut(
        rule_set_id=rule_set_id,
        version=version,
        items=out,
        totals=ValidationDryRunTotalsOut(
            would_fail=would_fail, would_error=would_error, pass_=passed
        ),
    )


# -- drift ----------------------------------------------------------------------------------------

DriftStatus = Literal["drifted", "at_risk", "compliant"]


def drift_status(rules: Sequence[ValidationFailingRuleOut]) -> DriftStatus:
    """``drifted``: a blocking finding under an enforced version (D-160, D-124). ``at_risk``: only
    error-severity findings of warn-mode versions, which block once those versions enforce.
    ``compliant``: neither (warnings and info never move a contract)."""
    if any(r.blocking for r in rules):
        return "drifted"
    if any(r.severity == "error" and r.mode == "warn" for r in rules):
        return "at_risk"
    return "compliant"


def _current_versions(prepared: _Prepared) -> list[ValidationDriftVersionOut]:
    return [
        ValidationDriftVersionOut(
            rule_set_id=entry.rule_set_id,
            name=entry.name,
            version_no=entry.version_no,
            mode=entry.mode,
        )
        for entry in prepared.resolved
    ]


async def drift(
    app_state: Any,
    principal: Principal,
    authorizer: Any,
    *,
    folder_id: uuid.UUID,
    items: Sequence[BatchItem],
    disconnected: Disconnected | None = None,
) -> ValidationDriftOut:
    """Evaluate each document against the current mandatory resolution of its own folder."""
    facts = await folder_facts(
        authorizer, principal, "governance:drift-read", [i.folder_id for i in items], folder_id
    )
    session_factory: async_sessionmaker[AsyncSession] = app_state.session_factory
    prepared: dict[uuid.UUID, _Prepared] = {}
    async with session_factory() as session:
        session.sync_session.info["read_only"] = True
        now = await db_now(session)
        for fid, folder in facts.items():
            # D-121 at the database clock; drift ignores attachments and compatibility (D-160).
            resolved = resolve_mandatory(await load_mandatory(session, folder), now)
            prepared[fid] = _prepare(resolved, await _rule_bodies(session, resolved))
    # The session is closed: no transaction is open while the sandbox evaluates (D-137).

    out: list[ValidationDriftItemOut] = []
    tally = {"drifted": 0, "at_risk": 0, "compliant": 0, "unparseable": 0}
    async with _batch_slot(app_state):
        clock = PageClock(app_state.settings.batch_page_budget_s, disconnected)
        for item in items:
            await clock.check()
            plan = prepared[item.folder_id]
            rules_out = await _evaluate_item(
                app_state, principal, authorizer, facts[item.folder_id], item, plan
            )
            status: DriftStatus | Literal["unparseable"]
            if rules_out is None:
                status, rules_out = "unparseable", []
            else:
                status = drift_status(rules_out)
            tally[status] += 1
            out.append(
                ValidationDriftItemOut(
                    contract_id=item.contract_id,
                    folder_id=item.folder_id,
                    status=status,
                    counts=_counts(rules_out),
                    failing_rules=rules_out,
                    current_versions=_current_versions(plan),
                )
            )
    return ValidationDriftOut(
        folder_id=folder_id, items=out, totals=ValidationDriftTotalsOut(**tally)
    )
