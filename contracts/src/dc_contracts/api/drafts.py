"""Draft routes: ``/api/v1/contracts/contracts/{contract_id}/drafts`` (CONT-03, D-42).

``PUT .../drafts/{draft_id}`` replaces a draft's raw document under optimistic concurrency: the
request must carry ``If-Match`` with the draft's current strong ETag (``"r<revision>"``). A
missing header is 428 (answered before the body is read), a stale or weak one is 412. Like the
create routes there is no Pydantic body parameter: the policy dependency authenticates and
authorises first and the handler reads the raw bytes afterwards.

``POST .../drafts/{draft_id}/publish`` publishes the draft in place under the same ``If-Match``
rule (``contract:publish``); see ``dc_contracts.versions`` for the lock order and the gate.

A caller who may not read the contract gets 404 with the same body whether or not it exists
(``contract:update`` is visible via ``contract:read``). Events carry metadata only, never the
document.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Annotated, Any, Final, Literal

import structlog
from dc_auth.deps import UnitOfWorkDep, current_decision, parse_bearer, require
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from dc_odcs import extract_search_fields, validate_structure
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.access_events import record_content_read
from dc_contracts.api.contracts import (
    _DOCUMENT_MEDIA_TYPES,
    ContractStructuralReportOut,
    _report_out,
    contract_folder,
    require_writable_folder,
)
from dc_contracts.convert import negotiate, render
from dc_contracts.drafts import get_draft, list_drafts, save_draft
from dc_contracts.http_cache import etag_for_draft, precondition_required, version_etag
from dc_contracts.ingest import parse_upload, read_body_capped, structural_report
from dc_contracts.lifecycle import BumpKind
from dc_contracts.link_facts import draft_link_resolver
from dc_contracts.models import Contract, ContractVersion
from dc_contracts.settings import ContractsSettings
from dc_contracts.validation_client import GateContext, save_mode_embed, validate_draft
from dc_contracts.versions import create_draft, delete_draft, get_publish_gate, publish_draft

router = PolicyRouter(prefix="/api/v1/contracts")

_log = structlog.get_logger("dc_contracts.api.drafts")

# The gate's refusal slug; only this verdict is a refusal worth auditing (D-175). An outage (503
# ``validation-unavailable``), a 409, 412, 428 or 404 is not a verdict and records nothing.
_REFUSED_SLUG: Final = "contract-validation-failed"
# Caps on the ``contract.version.publish_refused`` event so a pathological draft or report cannot
# bloat the audit row.
_REFUSED_SEMVER_CHARS: Final = 256
_REFUSED_MAX_BLOCKING: Final = 50
_REFUSED_KEY_CHARS: Final = 200


class ContractDraftOut(BaseModel):
    id: uuid.UUID
    contract_id: uuid.UUID
    revision: int
    etag: str
    # The document's own ``version`` when it is a string, else null.
    declared_version: str | None
    # The version or draft this draft was started from (null for the first draft), provenance only.
    base_version_id: uuid.UUID | None
    odcs_api_version: str
    media_type: str
    content_sha256: str
    size_bytes: int
    name: str | None
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class ContractDraftCreateIn(BaseModel):
    """Start a new draft from exactly one source: a version (semver) or a draft of the contract."""

    model_config = ConfigDict(extra="forbid")

    from_version: str | None = None
    from_draft_id: uuid.UUID | None = None
    # minor by default (D-45); none copies the source bytes verbatim.
    bump: BumpKind = "minor"

    @field_validator("from_version")
    @classmethod
    def _bounded(cls, value: str | None) -> str | None:
        if value is not None and (len(value) > 300 or "\x00" in value):
            raise ValueError("from_version must be at most 300 characters without NUL")
        return value

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ContractDraftCreateIn:
        if (self.from_version is None) == (self.from_draft_id is None):
            raise ValueError("give exactly one of from_version and from_draft_id")
        return self


class ContractDraftListOut(BaseModel):
    items: list[ContractDraftOut]


class ContractDraftWriteOut(BaseModel):
    draft: ContractDraftOut
    structural_validation: ContractStructuralReportOut
    # The advisory save-mode report from validation (VAL-06, D-134): status ``completed`` with the
    # findings, or ``{"status": "unavailable"}``. Never blocks the save.
    validation: dict[str, Any]


class ContractDraftValidationOut(BaseModel):
    """The save-mode report for a stored draft: nothing was stored or changed.

    ``pins`` and ``report`` are validation's JSON as it answered (see "Validation reports" in
    ``docs/user/api-guide.md``); contracts never imports validation's models.
    """

    draft_id: uuid.UUID
    content_sha256: str
    engine_version: str
    schema_sha: str
    pins: list[dict[str, Any]]
    report: dict[str, Any]


class ContractVersionPublishedOut(BaseModel):
    """A published version: its id is the draft's id, its public address is its semver."""

    version_id: uuid.UUID
    contract_id: uuid.UUID
    semver: str
    status: Literal["latest", "preview", "older"]
    lifecycle: Literal["published", "unpublished"]
    content_sha256: str
    etag: str
    fetch_url: str


def draft_out(version: ContractVersion) -> ContractDraftOut:
    return ContractDraftOut(
        id=version.id,
        contract_id=version.contract_id,
        revision=version.revision,
        etag=etag_for_draft(version.revision),
        declared_version=version.odcs_version,
        base_version_id=version.base_version_id,
        odcs_api_version=version.odcs_api_version,
        media_type=version.media_type,
        content_sha256=version.content_sha256,
        size_bytes=version.size_bytes,
        name=version.name,
        created_by=version.created_by,
        created_at=version.created_at,
        updated_by=version.updated_by,
        updated_at=version.updated_at,
    )


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


_IF_MATCH_PARAMETER = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "description": (
        'The draft\'s current strong ETag, for example "r1", or *. Weak tags never match.'
    ),
    "schema": {"type": "string"},
}


@router.post(
    "/contracts/{contract_id}/drafts",
    status_code=201,
    response_model=ContractDraftWriteOut,
    dependencies=[require("contract:update", target=contract_folder)],
    responses={
        404: {"description": "Contract or source not found, or not visible to the caller"},
        409: {
            "description": (
                "The folder is archived, or the contract already has the maximum number of "
                "drafts (draft-limit-reached)"
            )
        },
        422: {
            "description": (
                "Invalid body, or the source's top-level version is not a single-line scalar "
                "so a bump cannot be applied in place (copy-field-not-editable)"
            )
        },
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def create_contract_draft(
    contract_id: uuid.UUID,
    body: ContractDraftCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> ContractDraftWriteOut:
    """Start a new draft from a version or draft; the copy keeps the source bytes except version."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)
    settings: ContractsSettings = request.app.state.settings
    draft = await create_draft(
        uow,
        contract_id,
        from_version=body.from_version,
        from_draft_id=body.from_draft_id,
        bump_kind=body.bump,
        settings=settings,
    )
    await uow.commit()
    report = await asyncio.to_thread(validate_structure, draft.document, draft.odcs_api_version)
    response.headers["Location"] = f"/api/v1/contracts/contracts/{contract_id}/drafts/{draft.id}"
    response.headers["ETag"] = etag_for_draft(draft.revision)
    validation = await save_mode_embed(
        request, contract_id, draft.id, bytes(draft.raw_document), draft.media_type
    )
    return ContractDraftWriteOut(
        draft=draft_out(draft), structural_validation=_report_out(report), validation=validation
    )


@router.delete(
    "/contracts/{contract_id}/drafts/{draft_id}",
    status_code=204,
    response_class=Response,
    dependencies=[require("contract:update", target=contract_folder)],
    openapi_extra={"parameters": [_IF_MATCH_PARAMETER]},
    responses={
        404: {"description": "Contract or draft not found, or not visible to the caller"},
        409: {
            "description": (
                "The folder is archived, the draft is already published, or it is the only "
                "row of a never-published contract (last-draft-protected)"
            )
        },
        412: {"description": "If-Match does not match the draft's current ETag"},
        428: {"description": "The request carries no If-Match header"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def delete_contract_draft(
    contract_id: uuid.UUID,
    draft_id: uuid.UUID,
    request: Request,
    uow: UnitOfWorkDep,
) -> Response:
    """Discard a draft under ``If-Match``."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)
    if_match = request.headers.get("if-match")
    if if_match is None or not if_match.strip():
        raise precondition_required()
    await delete_draft(uow, contract_id, draft_id, if_match)
    await uow.commit()
    return Response(status_code=204)


@router.put(
    "/contracts/{contract_id}/drafts/{draft_id}",
    status_code=200,
    response_model=ContractDraftWriteOut,
    dependencies=[require("contract:update", target=contract_folder)],
    openapi_extra={
        "requestBody": {"required": True, "content": _DOCUMENT_MEDIA_TYPES},
        "parameters": [_IF_MATCH_PARAMETER],
    },
    responses={
        404: {"description": "Contract or draft not found, or not visible to the caller"},
        409: {"description": "The folder is archived"},
        412: {"description": "If-Match does not match the draft's current ETag"},
        413: {"description": "Document larger than the size cap"},
        415: {"description": "Content-Type is not YAML or JSON"},
        422: {
            "description": (
                "Malformed document, unsupported ODCS version, failed apiVersion/kind/id gate, "
                "or the document id differs from the contract's ODCS id"
            )
        },
        428: {"description": "The request carries no If-Match header"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def update_contract_draft(
    contract_id: uuid.UUID,
    draft_id: uuid.UUID,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> ContractDraftWriteOut:
    """Replace the draft with a new raw document; stale or missing ``If-Match`` is refused."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)
    # Before the body is read or parsed: a client that forgot the header costs nothing.
    if_match = request.headers.get("if-match")
    if if_match is None or not if_match.strip():
        raise precondition_required()

    settings: ContractsSettings = request.app.state.settings
    raw = await read_body_capped(request, settings.max_document_bytes)
    parsed = await parse_upload(raw, request.headers.get("content-type", ""), settings)
    report = await structural_report(parsed)
    fields = extract_search_fields(parsed.document)

    contract = (
        await uow.session.execute(select(Contract).where(Contract.id == contract_id))
    ).scalar_one()
    draft, changed = await save_draft(uow, contract, draft_id, parsed, raw, fields, if_match)
    if changed:
        await uow.commit()
        written, raw_document = draft_out(draft), bytes(draft.raw_document)
    else:
        # SEC-1: an identical save writes nothing, but `lock_draft` still holds the draft row
        # `FOR UPDATE` in an open transaction. Read what the answer needs, then end the
        # transaction (a rollback also expires the entities) before the slow validation embed.
        written, raw_document = draft_out(draft), bytes(draft.raw_document)
        await uow.session.rollback()
    response.headers["ETag"] = etag_for_draft(written.revision)
    validation = await save_mode_embed(
        request, contract_id, draft_id, raw_document, written.media_type
    )
    return ContractDraftWriteOut(
        draft=written, structural_validation=_report_out(report), validation=validation
    )


def _refused_blocking(report: object) -> list[dict[str, str]]:
    """The blocking ``{rule_key, code}`` pairs of a refusal report, in report order, capped.

    ``code`` is the finding's ``error_code`` when it has one, else its ``outcome`` (``fail``). A
    structurally invalid document adds ``{rule_key: odcs-json-schema, code: structural}`` first.
    Identifiers only: a finding's message, location or any other field never leaves here (D-148).
    """
    if not isinstance(report, dict):
        return []
    pairs: list[dict[str, str]] = []
    structural = report.get("structural")
    if isinstance(structural, dict) and structural.get("valid") is False:
        pairs.append({"rule_key": "odcs-json-schema", "code": "structural"})
    findings = report.get("findings")
    for item in findings if isinstance(findings, list) else ():
        if len(pairs) >= _REFUSED_MAX_BLOCKING:
            break
        if not isinstance(item, dict) or item.get("blocking") is not True:
            continue
        rule_key = item.get("rule_key")
        error_code = item.get("error_code")
        outcome = item.get("outcome")
        code = error_code if isinstance(error_code, str) else outcome
        pairs.append(
            {
                "rule_key": rule_key[:_REFUSED_KEY_CHARS] if isinstance(rule_key, str) else "",
                "code": code[:_REFUSED_KEY_CHARS] if isinstance(code, str) else "",
            }
        )
    return pairs


def _refused_run_id(value: object) -> str | None:
    """The validation run id when it is a UUID, else None (never free text)."""
    try:
        return str(uuid.UUID(str(value))) if value is not None else None
    except ValueError:
        return None


async def _record_refused_publish(
    uow: UnitOfWorkDep, contract_id: uuid.UUID, draft_id: uuid.UUID, exc: ProblemError
) -> None:
    """Audit a publish the gate refused as ``contract.version.publish_refused`` (D-175).

    The refused unit is rolled back first, so the event commits in its own transaction and never
    alongside anything the publish touched. Any failure here is logged (ids and error class only)
    and swallowed: the client always gets the gate's 422 (T-7-08).
    """
    try:
        await uow.session.rollback()
        ancestors = (
            await uow.session.execute(
                select(Contract.ancestor_ids).where(Contract.id == contract_id)
            )
        ).scalar_one_or_none()
        declared = (
            await uow.session.execute(
                select(ContractVersion.odcs_version).where(
                    ContractVersion.id == draft_id, ContractVersion.contract_id == contract_id
                )
            )
        ).scalar_one_or_none()
        uow.audit(
            "contract.version.publish_refused",
            "contract",
            contract_id,
            before=None,
            after={
                "draft_id": str(draft_id),
                "semver": declared[:_REFUSED_SEMVER_CHARS] if isinstance(declared, str) else None,
                "run_id": _refused_run_id(exc.extensions.get("run_id")),
                "blocking": _refused_blocking(exc.extensions.get("report")),
            },
            scope_path=list(ancestors or ()),
        )
        await uow.commit()
    except Exception as failure:
        _log.warning(
            "publish_refused_not_recorded",
            contract_id=str(contract_id),
            draft_id=str(draft_id),
            error=type(failure).__name__,
        )


@router.post(
    "/contracts/{contract_id}/drafts/{draft_id}/publish",
    status_code=201,
    response_model=ContractVersionPublishedOut,
    dependencies=[require("contract:publish", target=contract_folder)],
    openapi_extra={"parameters": [_IF_MATCH_PARAMETER]},
    responses={
        404: {"description": "Contract or draft not found, or not visible to the caller"},
        409: {
            "description": (
                "The folder is archived, the version is not above every existing version "
                "(semver-conflict), or the draft is already published"
            )
        },
        412: {"description": "If-Match does not match the draft's current ETag"},
        422: {
            "description": (
                "The document's version is not a quoted SemVer 2.0.0 string, the ODCS version is "
                "not accepted, the document id differs from the contract's ODCS id, or the "
                "document fails an enforced mandatory rule, an attached error rule or its ODCS "
                "schema (contract-validation-failed: the body carries the report and the run id)"
            )
        },
        428: {"description": "The request carries no If-Match header"},
        503: {
            "description": (
                "Authorization or contract validation is temporarily unavailable "
                "(validation-unavailable); nothing was published"
            )
        },
    },
)
async def publish_contract_draft(
    contract_id: uuid.UUID,
    draft_id: uuid.UUID,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> ContractVersionPublishedOut:
    """Publish the draft under ``If-Match``; the first publish of a contract becomes its Latest."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)
    if_match = request.headers.get("if-match")
    if if_match is None or not if_match.strip():
        raise precondition_required()

    settings: ContractsSettings = request.app.state.settings
    try:
        result = await publish_draft(
            uow,
            contract_id,
            draft_id,
            if_match,
            settings,
            gate=get_publish_gate(request.app.state),
            ctx=GateContext(
                contract_id=contract_id,
                draft_id=draft_id,
                facts=facts,
                bearer=parse_bearer(request),
                # WR-02: publish fails closed when identity cannot scope the links.
                link_resolver=draft_link_resolver(
                    request.app.state.authorizer_factory(request),
                    request.state.principal,
                    contract_id,
                    draft_id,
                    strict=True,
                ),
            ),
        )
    except ProblemError as exc:
        # Only the gate's verdict is a refusal (D-175); the original 422 is re-raised unchanged.
        if exc.slug == _REFUSED_SLUG:
            await _record_refused_publish(uow, contract_id, draft_id, exc)
        raise
    await uow.commit()
    version = result.version
    assert version.semver is not None  # noqa: S101 - a published row always carries its semver
    fetch_url = f"/api/v1/contracts/contracts/{contract_id}/versions/{version.semver}"
    etag = version_etag(version.content_sha256)
    response.headers["Location"] = fetch_url
    response.headers["ETag"] = etag
    return ContractVersionPublishedOut(
        version_id=version.id,
        contract_id=contract_id,
        semver=version.semver,
        status=result.status,
        lifecycle="published",
        content_sha256=version.content_sha256,
        etag=etag,
        fetch_url=fetch_url,
    )


@router.post(
    "/contracts/{contract_id}/drafts/{draft_id}/validate",
    response_model=ContractDraftValidationOut,
    dependencies=[require("contract:read", target=contract_folder)],
    responses={
        404: {"description": "Contract or draft not found, or not visible to the caller"},
        503: {
            "description": (
                "Authorization or contract validation is temporarily unavailable "
                "(validation-unavailable)"
            )
        },
    },
)
async def validate_contract_draft(
    contract_id: uuid.UUID, draft_id: uuid.UUID, request: Request
) -> ContractDraftValidationOut:
    """Validate a stored draft on demand (save mode): the draft is neither changed nor stored.

    Needs ``contract:read``, so a Policy Manager (no contract role) cannot use it.
    """
    answer = await validate_draft(request, contract_id, draft_id)
    return ContractDraftValidationOut(
        draft_id=draft_id,
        content_sha256=answer["content_sha256"],
        engine_version=answer["engine_version"],
        schema_sha=answer["schema_sha"],
        pins=answer.get("pins", []),
        report=answer["report"],
    )


@router.get(
    "/contracts/{contract_id}/drafts",
    response_model=ContractDraftListOut,
    dependencies=[require("contract:read", target=contract_folder)],
    responses={404: {"description": "Contract not found, or not visible to the caller"}},
)
async def list_contract_drafts(
    contract_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> ContractDraftListOut:
    """The contract's drafts with their declared version, revision and ETag."""
    drafts = await list_drafts(session, contract_id)
    return ContractDraftListOut(items=[draft_out(draft) for draft in drafts])


@router.get(
    "/contracts/{contract_id}/drafts/{draft_id}",
    response_model=ContractDraftOut,
    dependencies=[require("contract:read", target=contract_folder)],
    responses={404: {"description": "Contract or draft not found, or not visible to the caller"}},
)
async def get_contract_draft(
    contract_id: uuid.UUID,
    draft_id: uuid.UUID,
    response: Response,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> ContractDraftOut:
    """One draft's metadata; the ``ETag`` header carries its current revision tag."""
    draft = await get_draft(session, contract_id, draft_id)
    if draft is None:
        raise _not_found()
    response.headers["ETag"] = etag_for_draft(draft.revision)
    return draft_out(draft)


@router.get(
    "/contracts/{contract_id}/drafts/{draft_id}/document",
    response_class=Response,
    dependencies=[require("contract:read", target=contract_folder)],
    responses={
        200: {
            "description": (
                "The exact bytes of the draft with their stored media type. An Accept preference "
                "for the other format (application/json or application/yaml) returns a converted "
                "rendering, which is not byte-faithful and carries its own ETag."
            ),
            "content": _DOCUMENT_MEDIA_TYPES,
        },
        404: {"description": "Contract or draft not found, or not visible to the caller"},
        406: {"description": "Accept names no representation this service can produce"},
    },
)
async def get_contract_draft_document(
    contract_id: uuid.UUID,
    draft_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> Response:
    draft = await get_draft(session, contract_id, draft_id)
    if draft is None:
        raise _not_found()
    # Authorization already ran, so Accept is only looked at for a caller who may read drafts.
    target = negotiate(request.headers.get("accept"), draft.media_type)
    if target == "original":
        content, media_type = draft.raw_document, draft.media_type
    else:
        # Parsing and rendering are CPU-bound: keep them off the event loop.
        content, media_type = await asyncio.to_thread(
            render, draft.raw_document, draft.media_type, target
        )
    # One best-effort access event per content 200 (D-171); sync and non-raising.
    record_content_read(
        request, contract_id=contract_id, ref="draft", media_type=media_type, draft_id=draft.id
    )
    # Content-Type as a header so Starlette cannot append a charset; nosniff keeps a browser from
    # reinterpreting stored bytes; the representation depends on Accept, so caches key on it.
    return Response(
        content=content,
        headers={
            "Content-Type": media_type,
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Vary": "Accept",
            "ETag": etag_for_draft(draft.revision, target),
            "Cache-Control": "private, no-cache",
        },
    )


__all__ = [
    "ContractDraftCreateIn",
    "ContractDraftValidationOut",
    "ContractDraftListOut",
    "ContractDraftOut",
    "ContractDraftWriteOut",
    "ContractVersionPublishedOut",
    "draft_out",
    "router",
]
