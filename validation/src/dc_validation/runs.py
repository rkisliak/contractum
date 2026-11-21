"""The validation run: parse, structural check, resolve, evaluate in the sandbox, report, store.

A run is a pure function of its request (D-125). It re-parses the raw bytes itself, takes the
folder's ancestry and context only from identity's PDP folder facts (never from the body), resolves
the mandatory rule sets itself and evaluates every applicable rule in the sandbox. It never calls
the contracts service. The same pipeline answers the contracts publish gate (``mode="publish"``,
stored forever as an immutable ``ValidationRun``), validate-on-save and the on-demand upload
(``mode="save"``, never stored) (D-126, D-128, D-133).

Ordering (research Pattern 3): everything that reads the database happens in one short session that
is closed before the sandbox starts, so no transaction is open while rules evaluate (D-137). Rules
are evaluated in a deterministic order (holder depth, rule-set name, rule key), each independently.
Every text in the answer is a rule author's static message or a fixed text (VAL-07).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from dc_auth.authorizer import FolderFacts
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.problems import ProblemError
from dc_odcs import MAX_VERSION_CHARS, schema_manifest_sha256, validate_structure
from dc_odcs.diff import DiffResult, compare
from dc_rules.model import DSL_VERSION, DslVersion, Operation, RuleDef
from dc_rules.protocol import EvalUnit, UnitResult
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dc_validation.compat import BaselineRef, build_compatibility, floor_from_version, version_text
from dc_validation.facts import ValidationRefFactsIn, build_refs, summarise_refs
from dc_validation.ingest import parse_document
from dc_validation.models import RuleSetVersion, ValidationRun
from dc_validation.report import (
    RuleBinding,
    ValidationPinOut,
    ValidationReportOut,
    build_report,
    pins_of,
)
from dc_validation.resolution import (
    Attachment,
    CandidateSet,
    ResolvedRuleSet,
    UnresolvedAttachment,
    order_key,
    resolve_explicit,
    resolve_mandatory,
)
from dc_validation.rulesets import (
    _unavailable,
    db_now,
    load_candidates,
    load_mandatory,
    not_found,
)
from dc_validation.sandbox import Sandbox, SandboxBusy, SandboxUnavailable

# Reported when the run had nothing to evaluate, so no worker ever started and none reported one.
NO_ENGINE = "none"

# The database column is a 32-bit integer.
_VersionNo = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]


class ValidationAttachmentIn(BaseModel):
    """An explicit rule-set attachment: ``version`` is ``"latest"`` or an exact version number."""

    model_config = ConfigDict(extra="forbid")

    rule_set_id: uuid.UUID
    version: _VersionNo | Literal["latest"]


class ValidationBaselineIn(BaseModel):
    """The previously published version a run compares against (D-147): bytes plus its identity.

    ``semver`` is shown in the report, so it is limited to plain version characters. It is display
    only, so it is optional (``None``: derived from the baseline document) and capped at the shared
    ``MAX_VERSION_CHARS``: a cap below what contracts publishes would turn every later run of
    that contract into a 422, which contracts reports as 503 forever (CR-02).
    """

    model_config = ConfigDict(extra="forbid")

    version_id: uuid.UUID | None = None
    semver: str | None = Field(
        default=None, min_length=1, max_length=MAX_VERSION_CHARS, pattern=r"^[0-9A-Za-z.+-]+$"
    )
    media_type: str = Field(max_length=200)
    document_b64: str


class ValidationRunIn(BaseModel):
    """The internal run request (D-126). The document travels base64 encoded."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["save", "publish"]
    contract_id: uuid.UUID | None = None
    draft_id: uuid.UUID | None = None
    media_type: str = Field(max_length=200)
    document_b64: str
    # The count is capped by ``settings.max_attachments`` in the route, not by a literal (WR-06).
    attachments: list[ValidationAttachmentIn] = Field(default_factory=list)
    attachments_revision: int | None = Field(default=None, ge=0, le=2_147_483_647)
    # The newest published version of the contract and the highest stable major published so far
    # (null while only prereleases exist). Both optional: no baseline means no comparison (D-147).
    baseline: ValidationBaselineIn | None = None
    stable_major_floor: int | None = Field(default=None, ge=0, le=2_147_483_647)
    # What contracts found out about the document's contract links (D-153); validation never
    # calls contracts. A link without a fact here is unverifiable.
    ref_facts: ValidationRefFactsIn | None = None


@dataclass(frozen=True)
class RunBaseline:
    """The decoded baseline: raw bytes, not base64. ``semver`` is ``None`` when derived later."""

    version_id: uuid.UUID | None
    semver: str | None
    media_type: str
    raw: bytes


@dataclass(frozen=True)
class RunRequest:
    """The decoded request ``execute_run`` takes: the raw document bytes, not base64."""

    mode: Literal["save", "publish"]
    contract_id: uuid.UUID | None
    draft_id: uuid.UUID | None
    media_type: str
    raw: bytes
    attachments: tuple[ValidationAttachmentIn, ...] = ()
    attachments_revision: int | None = None
    baseline: RunBaseline | None = None
    stable_major_floor: int | None = None
    # On-demand pairs have no publish history: the floor comes from the previous document itself.
    derive_floor: bool = False
    ref_facts: ValidationRefFactsIn | None = None


class ValidationRunOut(BaseModel):
    """What a run answers: the report plus everything needed to pin and reproduce it (D-132)."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID | None
    mode: Literal["save", "publish"]
    content_sha256: str
    engine_version: str
    dsl_version: str
    schema_sha: str
    attachments_revision: int | None
    pins: list[ValidationPinOut]
    report: ValidationReportOut


class ValidationRunReadOut(BaseModel):
    """A stored publish run as the report fetch reads it (D-155): the report and the pins.

    The report is the D-128 report with its ``compatibility`` and ``ref_facts`` sections (``None``
    for a run stored before they existed); the pins are what the version was validated against.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    contract_id: uuid.UUID
    content_sha256: str
    engine_version: str
    dsl_version: str
    schema_sha: str
    created_at: datetime
    pins: list[ValidationPinOut]
    report: ValidationReportOut


async def run_folder(
    run_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    """Route target resolver: the folder a stored run was validated at (its contract's folder).

    The permission is checked there, with the caller's own token, so a run id is never an IDOR
    (T-6-26). An unknown id is the uniform 404.
    """
    folder_id = (
        await session.execute(select(ValidationRun.folder_id).where(ValidationRun.id == run_id))
    ).scalar_one_or_none()
    # End the lookup's transaction before the authorization call holds the request (SEC-2).
    await session.rollback()
    if folder_id is None:
        raise not_found()
    return folder_id


async def read_publishable_run(
    session: AsyncSession, run_id: uuid.UUID, contract_id: uuid.UUID
) -> ValidationRunReadOut:
    """The stored run, only when it belongs to ``contract_id`` and was publishable; 404 otherwise.

    A version's run is always publishable. A run that validation refused (blocking findings) stays
    unreadable here, and so does a run of another contract, with the same 404. Validation does not
    know which runs became versions: a run it passed whose publish contracts then refused at the
    lock (409 ``rules-changed``, ``baseline-changed``, ``semver-conflict`` or 412) is stored as
    publishable and is served to a ``contract:read-published`` holder at its folder who presents
    both its run id and contract id (OBS-1). Such a run id is never returned to any client, the
    route is internal, and contracts' report route only follows ``version.validation_run_id`` and
    checks ``content_sha256``, so nothing reaches a client through it.
    """
    run = (
        await session.execute(select(ValidationRun).where(ValidationRun.id == run_id))
    ).scalar_one_or_none()
    if run is None or run.contract_id != contract_id or not run.publishable:
        raise not_found()
    return ValidationRunReadOut(
        run_id=run.id,
        contract_id=run.contract_id,
        content_sha256=run.content_sha256,
        engine_version=run.engine_version,
        dsl_version=run.dsl_version,
        schema_sha=run.schema_sha,
        created_at=run.created_at,
        pins=[ValidationPinOut.model_validate(pin) for pin in run.resolved],
        report=ValidationReportOut.model_validate(run.report),
    )


def _invalid(pointer: str, detail: str) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Validation error",
        "The request did not validate.",
        errors=[{"pointer": pointer, "detail": detail}],
    )


def not_attachable(value: str) -> ProblemError:
    """422 for a rule set that is not published and visible at the folder (ad-hoc and attach)."""
    return ProblemError(
        422,
        "rule-set-not-attachable",
        "Unprocessable Content",
        f"Rule set {value} is not a published rule set visible at this folder.",
    )


def _not_attachable(entry: UnresolvedAttachment) -> ProblemError:
    spec = "" if entry.version_spec == "latest" else f"@{entry.version_spec}"
    return not_attachable(f"{entry.rule_set_id}{spec}")


def decode_request(body: ValidationRunIn) -> RunRequest:
    """Turn the wire request into a ``RunRequest``; 422 for a bad base64 value or a missing id."""
    if body.mode == "publish":
        if body.contract_id is None:
            raise _invalid("/contract_id", "contract_id is required in publish mode.")
        if body.draft_id is None:
            raise _invalid("/draft_id", "draft_id is required in publish mode.")
    try:
        raw = base64.b64decode(body.document_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _invalid("/document_b64", "document_b64 is not valid base64.") from exc
    baseline: RunBaseline | None = None
    if body.baseline is not None:
        try:
            baseline_raw = base64.b64decode(body.baseline.document_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _invalid("/baseline/document_b64", "document_b64 is not valid base64.") from exc
        baseline = RunBaseline(
            version_id=body.baseline.version_id,
            semver=body.baseline.semver,
            media_type=body.baseline.media_type,
            raw=baseline_raw,
        )
    return RunRequest(
        mode=body.mode,
        contract_id=body.contract_id,
        draft_id=body.draft_id,
        media_type=body.media_type,
        raw=raw,
        attachments=tuple(body.attachments),
        attachments_revision=body.attachments_revision,
        baseline=baseline,
        stable_major_floor=body.stable_major_floor,
        ref_facts=body.ref_facts,
    )


def _as_attachments(items: Sequence[ValidationAttachmentIn]) -> list[Attachment]:
    return [Attachment(rule_set_id=a.rule_set_id, version=a.version) for a in items]


def rule_ctx(facts: FolderFacts, operation: Operation) -> dict[str, Any]:
    """The ``ctx`` binding of a run: the folder keys plus why the run happens.

    ``operation`` comes only from the server-side run mode, never from a caller. Only cel-2 units
    see it (the worker strips it for cel-1).
    """
    return {
        "folder_kind": facts.kind,
        "folder_path": facts.path,
        "organization_id": None if facts.organization_id is None else str(facts.organization_id),
        "operation": operation,
    }


async def _rule_bodies(
    session: AsyncSession, resolved: Sequence[ResolvedRuleSet]
) -> dict[uuid.UUID, list[RuleDef]]:
    """The rules of every resolved version, ordered by rule key (the evaluation order)."""
    ids = [entry.rule_set_version_id for entry in resolved]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(RuleSetVersion.id, RuleSetVersion.rules).where(RuleSetVersion.id.in_(ids))
        )
    ).all()
    return {
        row.id: sorted((RuleDef.model_validate(rule) for rule in row.rules), key=lambda r: r.key)
        for row in rows
    }


def _require_known_dsl(entry: ResolvedRuleSet) -> None:
    """A stored DSL this engine does not know is refused, never evaluated under another one."""
    if entry.dsl_version not in get_args(DslVersion):
        raise _unavailable(busy=False)


def _units(
    resolved: Sequence[ResolvedRuleSet], bodies: dict[uuid.UUID, list[RuleDef]]
) -> tuple[list[EvalUnit], dict[int, RuleBinding]]:
    """One unit per rule in order: holder depth, rule-set name, then rule key."""
    units: list[EvalUnit] = []
    bindings: dict[int, RuleBinding] = {}
    for entry in resolved:
        _require_known_dsl(entry)
        for rule in bodies.get(entry.rule_set_version_id, []):
            unit_id = len(units)
            units.append(
                EvalUnit(
                    id=unit_id,
                    cache_key=f"{entry.rule_set_version_id}:{rule.key}",
                    rule=rule,
                    dsl_version=entry.dsl_version,  # checked by _require_known_dsl
                )
            )
            bindings[unit_id] = RuleBinding(resolved=entry, rule=rule)
    return units, bindings


def _deprecated(candidates: Sequence[CandidateSet]) -> set[uuid.UUID]:
    return {v.id for c in candidates for v in c.versions if v.deprecated_at is not None}


@dataclass(frozen=True)
class _Compared:
    """The baseline of a run after parsing and diffing; ``result`` is ``None`` when unusable."""

    previous: Any
    result: DiffResult | None
    semver: str | None


async def _compare_baseline(baseline: RunBaseline, document: Any, settings: Any) -> _Compared:
    """Parse the baseline like the document and diff it off the event loop (D-147, T-6-14).

    A baseline that is over the size cap, does not parse or is not a supported ODCS document is
    unusable (``result`` ``None``); the caller decides whether that blocks (publish) or only
    advises (save). It never raises for the baseline's content.
    """
    try:
        parsed = await parse_document(baseline.raw, baseline.media_type, settings)
    except ProblemError:
        return _Compared(None, None, baseline.semver)
    semver = baseline.semver
    if semver is None:
        semver = version_text(parsed.document.get("version"))
    result = await asyncio.to_thread(compare, parsed.document, document)
    return _Compared(parsed.document, result, semver)


async def _evaluate(
    sandbox: Sandbox,
    units: list[EvalUnit],
    document: Any,
    ctx: dict[str, Any],
    compared: _Compared | None = None,
    refs: dict[str, Any] | None = None,
) -> dict[int, UnitResult]:
    if not units:
        return {}
    try:
        # cel-2 rules see the parsed baseline as ``previous`` and the full classified list as
        # ``changes`` (D-152). Without a usable baseline previous is null and changes is [], so a
        # guarded rule passes and an unguarded has(previous.x) is an error outcome, never a pass.
        # ``refs`` holds the reference facts resolved before evaluation (D-153).
        usable = compared if compared is not None and compared.result is not None else None
        return await sandbox.evaluate(
            units,
            document,
            ctx,
            previous=None if usable is None else usable.previous,
            changes=[] if usable is None or usable.result is None else usable.result.as_binding(),
            refs=refs,
        )
    except SandboxBusy as exc:
        raise _unavailable(busy=True) from exc
    except SandboxUnavailable as exc:
        raise _unavailable(busy=False) from exc


async def execute_run(
    app_state: Any,
    principal: Principal,
    facts: FolderFacts,
    request: RunRequest,
    adhoc: Sequence[ValidationAttachmentIn] = (),
    authorizer: Any | None = None,
) -> ValidationRunOut:
    """Validate ``request.raw`` at the folder of ``facts`` and answer with the report.

    ``facts`` must come from the PDP decision for the caller's own token. Publish mode stores the
    run (immutably, in its own transaction) and returns its id; save mode stores nothing.
    ``authorizer`` is the caller's own (forwarded token); it resolves people and teams. Without
    one they are unverifiable.
    """
    settings = app_state.settings
    sandbox: Sandbox = app_state.sandbox
    session_factory: async_sessionmaker[AsyncSession] = app_state.session_factory
    parsed = await parse_document(request.raw, request.media_type, settings)
    structural = await asyncio.to_thread(validate_structure, parsed.document, parsed.api_version)

    ancestors = tuple(facts.ancestor_ids)
    async with session_factory() as session:
        session.sync_session.info["read_only"] = True
        now = await db_now(session)
        mandatory_candidates = await load_mandatory(session, facts)
        mandatory = resolve_mandatory(mandatory_candidates, now)
        mandatory_ids = {entry.rule_set_id for entry in mandatory}
        candidates = await load_candidates(
            session, facts, [a.rule_set_id for a in (*request.attachments, *adhoc)]
        )
        explicit, unresolved_explicit = resolve_explicit(
            _as_attachments(request.attachments), candidates, ancestors, mandatory_ids, "explicit"
        )
        taken = mandatory_ids | {entry.rule_set_id for entry in explicit}
        adhoc_resolved, unresolved_adhoc = resolve_explicit(
            _as_attachments(adhoc), candidates, ancestors, taken, "adhoc"
        )
        if unresolved_adhoc:
            # User input, corrected at once by the caller: a fixed 422 whatever the reason (an
            # unknown id, another branch's set or a missing version look alike), before any
            # sandbox work. The value echoed is the caller's own query parameter.
            raise _not_attachable(unresolved_adhoc[0])
        resolved = sorted([*mandatory, *explicit, *adhoc_resolved], key=order_key)
        unresolved: list[UnresolvedAttachment] = list(unresolved_explicit)
        bodies = await _rule_bodies(session, resolved)
        deprecated = _deprecated([*mandatory_candidates, *candidates.values()])
    # The session is closed: no transaction is open while the sandbox evaluates (D-137).

    units, bindings = _units(resolved, bodies)
    ctx = rule_ctx(facts, request.mode)
    compared: _Compared | None = None
    floor = request.stable_major_floor
    if request.baseline is not None:
        compared = await _compare_baseline(request.baseline, parsed.document, settings)
        if request.derive_floor and compared.previous is not None:
            floor = floor_from_version(compared.previous.get("version"))
    declared = parsed.document.get("version")
    compat = build_compatibility(
        mode=request.mode,
        baseline=(
            None
            if request.baseline is None or compared is None
            else BaselineRef(request.baseline.version_id, compared.semver)
        ),
        result=None if compared is None else compared.result,
        declared=declared,
        stable_major_floor=floor,
    )
    refs = await build_refs(
        app_state, principal, facts, parsed.document, request.ref_facts, authorizer=authorizer
    )
    results = await _evaluate(sandbox, units, parsed.document, ctx, compared, refs)
    report = build_report(
        structural,
        resolved,
        bindings,
        list(results.values()),
        unresolved,
        max_findings=settings.max_findings,
        native_findings=compat.findings,
        compatibility=compat.section,
        ref_facts=summarise_refs(refs),
    )
    pins = pins_of(resolved, deprecated)
    engine_version = sandbox.engine_version or NO_ENGINE
    schema_sha = schema_manifest_sha256()

    run_id: uuid.UUID | None = None
    if request.mode == "publish":
        assert request.contract_id is not None and request.draft_id is not None  # noqa: S101
        async with session_factory() as store, store.begin():
            run = ValidationRun(
                mode="publish",
                contract_id=request.contract_id,
                draft_id=request.draft_id,
                folder_id=facts.id,
                content_sha256=parsed.content_sha256,
                resolved=[pin.model_dump(mode="json") for pin in pins],
                engine_version=engine_version,
                dsl_version=DSL_VERSION,
                schema_sha=schema_sha,
                report=report.model_dump(mode="json"),
                publishable=report.summary.publishable,
                requested_by=principal.id,
            )
            store.add(run)
            await store.flush()
            run_id = run.id
    return ValidationRunOut(
        run_id=run_id,
        mode=request.mode,
        content_sha256=parsed.content_sha256,
        engine_version=engine_version,
        dsl_version=DSL_VERSION,
        schema_sha=schema_sha,
        attachments_revision=request.attachments_revision,
        pins=pins,
        report=report,
    )
