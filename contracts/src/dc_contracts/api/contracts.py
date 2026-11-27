"""Contract routes: ``/api/v1/contracts`` (CONT-01, CONT-06, D-09 to D-11, D-16, D-19).

* ``POST /folders/{folder_id}/contracts`` takes the raw document bytes (YAML or JSON). There is
  deliberately no Pydantic body parameter: the policy dependency authenticates and authorises
  first and the handler reads the body afterwards (research Pitfall 1).
* ``GET /contracts/{id}`` serves metadata as JSON; ``GET /contracts/{id}/document`` serves the
  exact submitted bytes with the submitted media type. Metadata is never injected into the
  document and the document is never re-serialised.

A caller who cannot read the target gets 404 with the same body whether or not the contract
exists (D-08). ``folder_id``, ``ancestor_ids`` and ``organization_id`` come from identity's
folder facts, never from the client.

Metadata and content are separate permissions (D-36). ``GET /contracts/{id}`` and the list need
only ``contract:read-metadata``, which a Policy Manager holds; the content-derived fields
(api_version, domain, tags, media_type, content_sha256, size_bytes) are then null and
``content_redacted`` is true. The document route needs ``contract:read``, so a metadata-only
caller gets the PDP's 404, byte-identical to the 404 for an unknown contract.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from dc_auth.authorizer import (
    AuthorizerUnavailable,
    Decision,
    FolderFacts,
    FolderScope,
    Outcome,
)
from dc_auth.deps import (
    UnitOfWorkDep,
    authenticate,
    current_decision,
    parse_bearer,
    recorded_refusal,
    require,
)
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.names import NameRule
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from dc_odcs import (
    ALLOWED_MEDIA_TYPES,
    MAX_ODCS_ID_LENGTH,
    StructuralReport,
    extract_search_fields,
)
from fastapi import Depends, Query, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.access_events import record_content_read
from dc_contracts.convert import negotiate, render
from dc_contracts.drafts import get_draft
from dc_contracts.http_cache import etag_for_draft
from dc_contracts.ingest import (
    check_document_name,
    parse_upload,
    read_body_capped,
    structural_report,
)
from dc_contracts.lifecycle import parts_of
from dc_contracts.models import Contract, ContractVersion
from dc_contracts.notation import get_adapter
from dc_contracts.repo import get_contract_with_version, insert_contract, search_contract_page
from dc_contracts.runtime_settings import effective_governance
from dc_contracts.search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_Q_LENGTH,
    ContractSearchFilters,
    decode_cursor,
    decode_name_cursor,
    encode_cursor,
    encode_name_cursor,
    reject_nul,
)
from dc_contracts.settings import ContractsSettings
from dc_contracts.templates import build_from_template, resolve_version
from dc_contracts.validation_client import save_mode_embed
from dc_contracts.versions import (
    Referrer,
    delete_contract,
    get_version_by_semver,
    restore_contract,
    visible_states,
)

router = PolicyRouter(prefix="/api/v1/contracts")

AUDIT_NAME_MAX_CHARS = 200

_BINARY = {"schema": {"type": "string", "format": "binary"}}
_DOCUMENT_MEDIA_TYPES = {media_type: _BINARY for media_type in sorted(ALLOWED_MEDIA_TYPES)}


class ContractVersionSummaryOut(BaseModel):
    number: int
    state: str


class ContractLatestVersionOut(BaseModel):
    """The contract's Latest published version (lifecycle metadata, not document content)."""

    version_id: uuid.UUID
    semver: str


class ContractTemplateOriginOut(BaseModel):
    """The template version a contract was copied from; the content is not linked."""

    template_id: uuid.UUID
    version_no: int


class ContractVersionOriginOut(BaseModel):
    """The contract version or draft a contract was copied from; the content is not linked.

    ``version_id`` is provenance only: it names a version or draft of the source contract at the
    time of the copy.
    """

    contract_id: uuid.UUID
    version_id: uuid.UUID


class ContractMetadataOut(BaseModel):
    id: uuid.UUID
    folder_id: uuid.UUID
    organization_id: uuid.UUID | None
    odcs_id: str
    notation: str
    name: str | None
    status: str | None
    # Content-derived fields: null when the caller holds contract:read-metadata only (D-36).
    api_version: str | None
    domain: str | None
    tags: list[str] | None
    media_type: str | None
    content_sha256: str | None
    size_bytes: int | None
    content_redacted: bool
    template: ContractTemplateOriginOut | None
    origin: ContractVersionOriginOut | None
    # The row's number and lifecycle state; the row is the head: the Latest once one exists, else
    # the newest draft.
    version: ContractVersionSummaryOut
    # Null before the first publish; when set, ``version`` describes this same row.
    latest_version: ContractLatestVersionOut | None
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class ContractListOut(BaseModel):
    """A page of contracts. There is deliberately no total or count (it would leak existence)."""

    items: list[ContractMetadataOut]
    next_cursor: str | None


class ContractStructuralErrorOut(BaseModel):
    pointer: str
    message: str


class ContractStructuralReportOut(BaseModel):
    valid: bool
    error_count: int
    errors: list[ContractStructuralErrorOut]


class ContractDraftRefOut(BaseModel):
    """The draft a create response refers to: its id, revision and ETag."""

    id: uuid.UUID
    revision: int
    etag: str


class ContractCreatedOut(BaseModel):
    contract: ContractMetadataOut
    version: ContractVersionSummaryOut
    draft: ContractDraftRefOut
    structural_validation: ContractStructuralReportOut
    # The advisory save-mode report from validation (VAL-06, D-134): status ``completed`` with the
    # findings, or ``{"status": "unavailable"}``. Never blocks the create.
    validation: dict[str, Any]


class ContractFromTemplateIn(BaseModel):
    """Create a contract from a published template version."""

    # extra="forbid": folder, organization, audit fields and every other server-owned value are a
    # 422, never silently ignored.
    model_config = ConfigDict(extra="forbid")

    template_id: uuid.UUID
    template_version: int | None = Field(default=None, ge=1, le=2_147_483_647)
    odcs_id: str = Field(min_length=1, max_length=MAX_ODCS_ID_LENGTH)
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("odcs_id")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("NUL (U+0000) characters are not allowed")
        return value


class ContractStarterIn(BaseModel):
    """Start a brand-new contract from the generated ODCS starter."""

    model_config = ConfigDict(extra="forbid")

    notation: Literal["odcs"] = "odcs"
    odcs_id: str = Field(min_length=1, max_length=MAX_ODCS_ID_LENGTH)
    name: Annotated[str, StringConstraints(min_length=1, max_length=200), NameRule]

    @field_validator("odcs_id")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("NUL (U+0000) characters are not allowed")
        return value


class ContractFromVersionIn(BaseModel):
    """Create a contract from a version or draft of another contract.

    Exactly one of ``source_version`` (a SemVer of a published or unpublished version) and
    ``source_draft_id`` (drafts are addressed by id: two drafts may declare the same version).
    """

    model_config = ConfigDict(extra="forbid")

    source_contract_id: uuid.UUID
    source_version: str | None = Field(default=None, min_length=1, max_length=300)
    source_draft_id: uuid.UUID | None = None
    odcs_id: str = Field(min_length=1, max_length=MAX_ODCS_ID_LENGTH)

    @field_validator("source_version", "odcs_id")
    @classmethod
    def _no_nul(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("NUL (U+0000) characters are not allowed")
        return value

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ContractFromVersionIn:
        if (self.source_version is None) == (self.source_draft_id is None):
            raise ValueError("give exactly one of source_version and source_draft_id")
        return self


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def public_organization_id(contract: Contract) -> uuid.UUID | None:
    """The Organization id for the API: null for a contract directly at ROOT.

    ``contract.organization_id`` is the ODCS-id scope key (the ROOT folder id for a ROOT contract),
    which is not an Organization, so the API never exposes it as one.
    """
    if len(contract.ancestor_ids) == 1:
        return None
    return contract.organization_id


def _metadata(
    contract: Contract, version: ContractVersion, *, content_visible: bool
) -> ContractMetadataOut:
    """Metadata for one contract; content-derived fields only when ``content_visible`` (D-36)."""
    return ContractMetadataOut(
        id=contract.id,
        folder_id=contract.folder_id,
        organization_id=public_organization_id(contract),
        odcs_id=contract.odcs_id,
        notation=contract.notation,
        name=version.name,
        status=version.status,
        api_version=version.odcs_api_version if content_visible else None,
        domain=version.domain if content_visible else None,
        tags=list(version.tags) if content_visible else None,
        media_type=version.media_type if content_visible else None,
        content_sha256=version.content_sha256 if content_visible else None,
        size_bytes=version.size_bytes if content_visible else None,
        content_redacted=not content_visible,
        template=(
            ContractTemplateOriginOut(
                template_id=contract.template_id, version_no=contract.template_version
            )
            if contract.template_id is not None and contract.template_version is not None
            else None
        ),
        origin=(
            ContractVersionOriginOut(
                contract_id=contract.origin_contract_id, version_id=contract.origin_version_id
            )
            if contract.origin_contract_id is not None and contract.origin_version_id is not None
            else None
        ),
        version=ContractVersionSummaryOut(number=version.version_no, state=version.state),
        latest_version=(
            ContractLatestVersionOut(version_id=version.id, semver=version.semver)
            if contract.latest_version_id == version.id and version.semver is not None
            else None
        ),
        created_by=contract.created_by,
        created_at=contract.created_at,
        updated_by=contract.updated_by,
        updated_at=contract.updated_at,
    )


def _draft_ref(version: ContractVersion) -> ContractDraftRefOut:
    return ContractDraftRefOut(
        id=version.id, revision=version.revision, etag=etag_for_draft(version.revision)
    )


def _report_out(report: StructuralReport) -> ContractStructuralReportOut:
    return ContractStructuralReportOut(
        valid=report.valid,
        error_count=report.error_count,
        errors=[
            ContractStructuralErrorOut(pointer=e.pointer, message=e.message) for e in report.errors
        ],
    )


async def _path_folder_id(folder_id: uuid.UUID) -> uuid.UUID:
    return folder_id


async def contract_folder(
    contract_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    """Target dependency for contract reads: the folder that holds the contract.

    An unknown contract is the same 404 a caller without read access gets (D-08). A soft-deleted
    contract is unknown to every route that uses this resolver (D-55); only restore looks it up
    through ``contract_folder_any``.
    """
    folder_id = (
        await session.execute(
            select(Contract.folder_id).where(
                Contract.id == contract_id, Contract.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    # WR-04: the lookup is done; end its transaction so the pooled connection is not held idle in
    # transaction through the authorization call and the whole request (the session is reused,
    # a later read simply begins again).
    await session.rollback()
    if folder_id is None:
        raise _not_found()
    return folder_id


async def contract_folder_any(
    contract_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    """Target dependency for restore: the folder of the contract, deleted or not (D-56).

    The only resolver that does not hide a soft-deleted contract. An unknown id is the same 404.
    """
    folder_id = (
        await session.execute(select(Contract.folder_id).where(Contract.id == contract_id))
    ).scalar_one_or_none()
    if folder_id is None:
        raise _not_found()
    return folder_id


@router.post(
    "/folders/{folder_id}/contracts",
    status_code=201,
    response_model=ContractCreatedOut,
    dependencies=[require("contract:create", target=_path_folder_id)],
    openapi_extra={"requestBody": {"required": True, "content": _DOCUMENT_MEDIA_TYPES}},
    responses={
        404: {"description": "Folder not found, or not visible to the caller"},
        409: {
            "description": "Folder is archived, or the ODCS id already exists in the organization "
            "(or among the contracts directly at ROOT)"
        },
        413: {"description": "Document larger than the size cap"},
        415: {"description": "Content-Type is not YAML or JSON"},
        422: {
            "description": (
                "Malformed document, unsupported ODCS version, or failed apiVersion/kind/id gate"
            )
        },
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def create_contract(
    folder_id: uuid.UUID,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> ContractCreatedOut:
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)

    settings: ContractsSettings = request.app.state.settings
    raw = await read_body_capped(request, settings.max_document_bytes)
    parsed = await parse_upload(raw, request.headers.get("content-type", ""), settings)
    check_document_name(parsed.document)
    report = await structural_report(parsed)
    fields = extract_search_fields(parsed.document)

    contract, version = await insert_contract(uow, facts, parsed, fields, raw=raw)
    return await _created_response(request, response, uow, contract, version, facts, raw, report)


async def _created_response(
    request: Request,
    response: Response,
    uow: UnitOfWork,
    contract: Contract,
    version: ContractVersion,
    facts: FolderFacts,
    raw: bytes,
    report: StructuralReport,
) -> ContractCreatedOut:
    """The shared tail of the create routes: one audit event, commit, Location, embed, body.

    The commit comes before the answer (the client only sees 201 once the row and its event are
    durable) and the validation embed after it, exactly as each route did inline.
    """
    _audit_created(uow, contract, version, facts)
    await uow.commit()

    response.headers["Location"] = f"/api/v1/contracts/contracts/{contract.id}"
    summary = ContractVersionSummaryOut(number=version.version_no, state=version.state)
    validation = await save_mode_embed(request, contract.id, version.id, raw, version.media_type)
    return ContractCreatedOut(
        contract=_metadata(contract, version, content_visible=True),
        version=summary,
        draft=_draft_ref(version),
        structural_validation=_report_out(report),
        validation=validation,
    )


def _audit_created(
    uow: UnitOfWork,
    contract: Contract,
    version: ContractVersion,
    facts: FolderFacts,
    *,
    origin_version_state: str | None = None,
) -> None:
    """Record ``contract.created``: two rows (contract and version 1), exactly one event.

    The event carries metadata, the content hash, the size and the template origin only: never the
    document, whose customProperties may hold secrets or personal data (T-01-45). A contract copied
    from another carries the origin ids and the state the source version had at the copy.
    """
    organization_id = public_organization_id(contract)
    after: dict[str, Any] = {
        "id": str(contract.id),
        "folder_id": str(contract.folder_id),
        "organization_id": str(organization_id) if organization_id is not None else None,
        "odcs_id": contract.odcs_id,
        "api_version": version.odcs_api_version,
        "media_type": version.media_type,
        "content_sha256": version.content_sha256,
        "size_bytes": version.size_bytes,
        "version_no": version.version_no,
        "state": version.state,
        # Free text from the submitted document: bounded so one event can never outgrow the NATS
        # max_payload and stall the relay (WR-01). The full name stays in the contract row.
        "name": version.name[:AUDIT_NAME_MAX_CHARS] if version.name is not None else None,
        "template_id": str(contract.template_id) if contract.template_id is not None else None,
        "template_version": contract.template_version,
        "origin_contract_id": (
            str(contract.origin_contract_id) if contract.origin_contract_id is not None else None
        ),
        "origin_version_id": (
            str(contract.origin_version_id) if contract.origin_version_id is not None else None
        ),
        "origin_version_state": origin_version_state,
    }
    uow.audit(
        "contract.created",
        "contract",
        contract.id,
        before=None,
        after=after,
        scope_path=facts.ancestor_ids,
        aggregate_version=1,
    )


def require_writable_folder(facts: FolderFacts) -> None:
    """An archived subtree is read-only: 409 ``folder-archived`` for every contract write."""
    if facts.archived:
        raise ProblemError(
            409,
            "folder-archived",
            "Conflict",
            "The folder is archived; its subtree is read-only.",
        )


@router.post(
    "/folders/{folder_id}/contracts/from-template",
    status_code=201,
    response_model=ContractCreatedOut,
    dependencies=[require("contract:create", target=_path_folder_id)],
    responses={
        403: {"description": "The caller can read the folder but may not create contracts in it"},
        404: {
            "description": (
                "Folder, template or version not found, or the template is not held by the "
                "folder or one of its ancestors"
            )
        },
        409: {
            "description": "Folder is archived, or the ODCS id already exists in the organization "
            "(or among the contracts directly at ROOT)"
        },
        413: {"description": "The resulting document is larger than the size cap"},
        422: {
            "description": (
                "Invalid body, a forbidden override (apiVersion, kind, id), a deprecated "
                "template version, or a failed apiVersion/kind/id gate"
            )
        },
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def create_contract_from_template(
    folder_id: uuid.UUID,
    body: ContractFromTemplateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> ContractCreatedOut:
    """Start a contract from a template version: the content is copied, the origin recorded.

    Later template edits, publishes or deprecations never change the contract. The resulting
    document goes through the same ingest gate and size cap as any uploaded contract, and the ODCS
    id is unique within the Organization (409). The stored document is the template's parsed
    document re-serialised as JSON (YAML comments are not carried).
    """
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)

    settings: ContractsSettings = request.app.state.settings
    template_version = await resolve_version(
        uow.session, facts, body.template_id, body.template_version
    )
    # Parsing and serialising are CPU-bound: keep them off the event loop (T-02-50).
    raw = await asyncio.to_thread(
        build_from_template, template_version, odcs_id=body.odcs_id, overrides=body.overrides
    )
    parsed = await parse_upload(raw, "application/json", settings)
    check_document_name(parsed.document)
    report = await structural_report(parsed)
    fields = extract_search_fields(parsed.document)

    contract, version = await insert_contract(
        uow,
        facts,
        parsed,
        fields,
        raw=raw,
        template_id=template_version.template_id,
        template_version=template_version.version_no,
    )
    return await _created_response(request, response, uow, contract, version, facts, raw, report)


@router.post(
    "/folders/{folder_id}/contracts/starter",
    status_code=201,
    response_model=ContractCreatedOut,
    dependencies=[require("contract:create", target=_path_folder_id)],
    responses={
        403: {"description": "The caller can read the folder but may not create contracts in it"},
        404: {"description": "Folder not found, or not visible to the caller"},
        409: {
            "description": "Folder is archived, or the ODCS id already exists in the organization "
            "(or among the contracts directly at ROOT)"
        },
        422: {"description": "Invalid body"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def create_contract_from_starter(
    folder_id: uuid.UUID,
    body: ContractStarterIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> ContractCreatedOut:
    """Start a contract from a generated ODCS starter document.

    The document is built on the server from a dictionary and serialised with ``yaml.safe_dump``
    (version ``0.1.0``, status ``draft``), so caller text can never add or override a key. It goes
    through the same ingest gate as any uploaded contract; schema warnings are reported, not
    blocking.
    """
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)

    settings: ContractsSettings = request.app.state.settings
    governance = await effective_governance(
        request.app.state.settings_client, parse_bearer(request), settings
    )
    raw, media_type = await asyncio.to_thread(
        get_adapter(body.notation).starter,
        odcs_id=body.odcs_id,
        name=body.name,
        api_version=governance.default_odcs_api_version,
    )
    parsed = await parse_upload(raw, media_type, settings)
    check_document_name(parsed.document)
    report = await structural_report(parsed)
    fields = extract_search_fields(parsed.document)

    contract, version = await insert_contract(uow, facts, parsed, fields, raw=raw)
    return await _created_response(request, response, uow, contract, version, facts, raw, report)


async def _check_source(
    request: Request, principal: Principal, permission: str, folder_id: uuid.UUID
) -> Decision:
    """The decision at the source folder; the one 404 unless ``permission`` is allowed."""
    authorizer = request.app.state.authorizer_factory(request)
    try:
        decision: Decision = await authorizer.check(principal, permission, folder_id)
    except AuthorizerUnavailable as exc:
        raise ProblemError(
            503,
            "service-unavailable",
            "Service Unavailable",
            "Authorization is temporarily unavailable.",
        ) from exc
    if decision.outcome is not Outcome.ALLOW:
        raise _not_found()
    return decision


@router.post(
    "/folders/{folder_id}/contracts/from-version",
    status_code=201,
    response_model=ContractCreatedOut,
    dependencies=[require("contract:create", target=_path_folder_id)],
    responses={
        403: {"description": "The caller can read the folder but may not create contracts in it"},
        404: {
            "description": (
                "Folder, source contract, version or draft not found, or not readable by the "
                "caller (one response for every reason)"
            )
        },
        409: {
            "description": "Folder is archived, or the ODCS id already exists in the organization "
            "(or among the contracts directly at ROOT)"
        },
        413: {"description": "The copied document is larger than the size cap"},
        422: {
            "description": (
                "Invalid body, the source's top-level id is not a single-line scalar "
                "(copy-field-not-editable)"
            )
        },
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def create_contract_from_version(
    folder_id: uuid.UUID,
    body: ContractFromVersionIn,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> ContractCreatedOut:
    """Start a contract from a version or draft of another contract the caller can read.

    Readable means ``contract:read-published`` at the source folder for a published version,
    ``contract:publish`` there for an unpublished one, and ``contract:read`` for a draft. Every
    refusal is the same 404. The stored bytes are the source's with only the top-level ``id``
    token replaced; the origin is recorded, the content is never linked.
    """
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)

    session = uow.session
    # A soft-deleted source is unknown, like an unreadable one (D-55).
    source = (
        await session.execute(
            select(Contract).where(
                Contract.id == body.source_contract_id, Contract.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise _not_found()

    source_version: ContractVersion | None
    if body.source_version is not None:
        source_decision = await _check_source(
            request, principal, "contract:read-published", source.folder_id
        )
        source_version = await get_version_by_semver(
            session, source.id, body.source_version, states=frozenset({"published", "unpublished"})
        )
        permissions = source_decision.permissions
        if source_version is not None and source_version.state not in visible_states(permissions):
            # An unpublished source needs contract:publish there: record the refusal (WR-01).
            raise await recorded_refusal(
                request, "contract:publish", source.folder_id, principal=principal
            )
    elif body.source_draft_id is not None:
        source_decision = await _check_source(request, principal, "contract:read", source.folder_id)
        source_version = await get_draft(session, source.id, body.source_draft_id)
    else:  # pragma: no cover - the model validator guarantees exactly one source
        raise _not_found()
    if source_version is None:
        raise _not_found()

    settings: ContractsSettings = request.app.state.settings
    # Only the top-level id token changes; the bytes and media type are the source's (CONT-06).
    raw = await asyncio.to_thread(
        get_adapter(source.notation).copy_with,
        bytes(source_version.raw_document),
        source_version.media_type,
        odcs_id=body.odcs_id,
        version=None,
    )
    parsed = await parse_upload(raw, source_version.media_type, settings)
    check_document_name(parsed.document)
    report = await structural_report(parsed)
    fields = extract_search_fields(parsed.document)

    contract, version = await insert_contract(
        uow,
        facts,
        parsed,
        fields,
        raw=raw,
        origin_contract_id=source.id,
        origin_version_id=source_version.id,
    )
    _audit_created(uow, contract, version, facts, origin_version_state=source_version.state)
    await uow.commit()
    # WR-04 (D-171): the copy read the source's content, so the source contract gets one access
    # event, scoped to the source folder so its own auditors see who copied it.
    source_facts = source_decision.folder
    source_scope = source_facts.ancestor_ids if source_facts is not None else ()
    if source_version.state == "draft":
        record_content_read(
            request,
            contract_id=source.id,
            ref="draft",
            media_type=source_version.media_type,
            draft_id=source_version.id,
            scope_path=source_scope,
        )
    else:
        semver = parts_of(source_version).text
        record_content_read(
            request,
            contract_id=source.id,
            ref=semver,
            media_type=source_version.media_type,
            version_id=source_version.id,
            semver=semver,
            scope_path=source_scope,
        )

    response.headers["Location"] = f"/api/v1/contracts/contracts/{contract.id}"
    summary = ContractVersionSummaryOut(number=version.version_no, state=version.state)
    validation = await save_mode_embed(request, contract.id, version.id, raw, version.media_type)
    return ContractCreatedOut(
        contract=_metadata(contract, version, content_visible=True),
        version=summary,
        draft=_draft_ref(version),
        structural_validation=_report_out(report),
        validation=validation,
    )


async def _load(session: AsyncSession, contract_id: uuid.UUID) -> tuple[Contract, ContractVersion]:
    found = await get_contract_with_version(session, contract_id)
    if found is None:  # pragma: no cover - contract_folder already answered 404
        raise _not_found()
    return found


@router.get(
    "/contracts",
    response_model=ContractListOut,
    dependencies=[require("contract:list")],
    responses={
        422: {"description": "Invalid limit, cursor or filter value"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def list_contracts(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    folder_id: Annotated[uuid.UUID | None, Query()] = None,
    include_subtree: Annotated[bool, Query()] = False,
    odcs_id: Annotated[str | None, Query(max_length=MAX_ODCS_ID_LENGTH)] = None,
    name: Annotated[str | None, Query()] = None,
    domain: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    tags: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=MAX_Q_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
    sort: Annotated[Literal["id", "name"], Query()] = "id",
) -> ContractListOut:
    """List and search contracts: filters, ``q`` text match, keyset pagination.

    ``sort`` is ``id`` (the default, creation order) or ``name`` (lower-cased name, an empty name
    sorted by its odcs id, then id); ``next_cursor`` is only valid with the ``sort`` it came from.

    ``require("contract:list")`` admits every authenticated caller; the identity scopes below trim
    the SQL, so a caller without access gets an empty page. A row appears when the caller
    holds ``contract:read-metadata`` there, or ``contract:read-published`` and the contract has a
    Latest. Every row is the contract's head: the Latest once one exists (for every caller), else
    the newest draft. Its content-derived fields are filled only where the caller holds
    ``contract:read`` or ``contract:read-published``, and content filters match only those
    rows, so a published-only caller can never match a draft's values.
    """
    reject_nul(odcs_id=odcs_id, name=name, domain=domain, status=status, tags=tags, q=q)
    after: uuid.UUID | None = None
    after_key: str | None = None
    if cursor is not None:
        if sort == "name":
            after_key, after = decode_name_cursor(cursor)
        else:
            after = decode_cursor(cursor)
    authorizer = request.app.state.authorizer_factory(request)
    # WR-01: the scope calls run after require(), so an identity outage here must be the same 503
    # every other route answers, never an unhandled 500 (and never an unfiltered page).
    try:
        metadata_scope = await authorizer.accessible_folders(principal, "contract:read-metadata")
        content_scope = await authorizer.accessible_folders(principal, "contract:read")
        published_scope = await authorizer.accessible_folders(principal, "contract:read-published")
    except AuthorizerUnavailable as exc:
        raise ProblemError(
            503,
            "service-unavailable",
            "Service Unavailable",
            "Authorization is temporarily unavailable.",
        ) from exc
    filters = ContractSearchFilters(
        folder_id=folder_id,
        include_subtree=include_subtree,
        odcs_id=odcs_id,
        name=name,
        domain=domain,
        status=status,
        tags=list(tags or []),
        q=q,
        limit=limit,
        cursor=after,
        sort=sort,
        cursor_key=after_key,
    )
    page = await search_contract_page(
        session,
        filters,
        metadata_scope=metadata_scope,
        content_scope=content_scope,
        published_scope=published_scope,
    )
    next_cursor: str | None = None
    if page.has_more:
        last_id = page.rows[-1][0].id
        if sort == "name" and page.last_sort_key is not None:
            next_cursor = encode_name_cursor(page.last_sort_key, last_id)
        else:
            next_cursor = encode_cursor(last_id)
    return ContractListOut(
        items=[
            _metadata(contract, version, content_visible=visible)
            for contract, version, visible in page.rows
        ],
        next_cursor=next_cursor,
    )


@router.delete(
    "/contracts/{contract_id}",
    status_code=204,
    response_class=Response,
    dependencies=[require("contract:delete", target=contract_folder)],
    responses={
        403: {"description": "The caller can read the contract but may not delete it"},
        404: {
            "description": (
                "Contract not found, already deleted, or not visible to the caller "
                "(one response for every reason)"
            )
        },
        409: {
            "description": (
                "The contract's folder is archived (folder-archived), or another contract's "
                "published version references it (contract-referenced; names only the referrers "
                "the caller can see and counts the rest), or a card has an active pin on one of "
                "its versions (contract-pinned; `reasons` names every pinning card)"
            )
        },
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def delete_contract_route(
    contract_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> Response:
    """Soft-delete a contract: hidden from everyone, history and ODCS id kept.

    Every route then answers the unknown-contract 404 for it and it leaves every list. The ODCS
    id stays taken in the Organization (a create with it is 409), and an Organization or Global
    admin can restore it. A contract with drafts only can be deleted.
    """
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)

    # Which referrers the caller may be told about (named by the reference guard, D-54).
    authorizer = request.app.state.authorizer_factory(request)
    try:
        metadata_scope: FolderScope = await authorizer.accessible_folders(
            principal, "contract:read-metadata"
        )
        published_scope: FolderScope = await authorizer.accessible_folders(
            principal, "contract:read-published"
        )
    except AuthorizerUnavailable as exc:
        raise ProblemError(
            503,
            "service-unavailable",
            "Service Unavailable",
            "Authorization is temporarily unavailable.",
        ) from exc

    def visible(referrer: Referrer) -> bool:
        return metadata_scope.covers(
            referrer.folder_id, referrer.ancestor_ids
        ) or published_scope.covers(referrer.folder_id, referrer.ancestor_ids)

    await delete_contract(uow, contract_id, visible=visible)
    # Commit before answering: the client only sees 204 once the row and its event are durable.
    await uow.commit()
    return Response(status_code=204)


@router.post(
    "/contracts/{contract_id}/restore",
    response_model=ContractMetadataOut,
    dependencies=[require("contract:restore", target=contract_folder_any)],
    responses={
        404: {
            "description": (
                "Contract not found, or the caller is not an Organization or Global admin "
                "(one response for every reason)"
            )
        },
        409: {
            "description": (
                "The contract's folder is archived (folder-archived), or one of its published "
                "versions links to a deleted contract (contract-referenced; counted in "
                "`deleted_target_count`, never named)"
            )
        },
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def restore_contract_route(
    contract_id: uuid.UUID,
    request: Request,
    uow: UnitOfWorkDep,
) -> ContractMetadataOut:
    """Restore a soft-deleted contract: Organization and Global admins only.

    Everything is visible again as it was. Restoring a contract that is not deleted is a 200 no-op
    without an event. Everyone else gets the unknown-contract 404, so a deleted contract's
    existence is never revealed to them.
    """
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)

    _, changed = await restore_contract(uow, contract_id)
    if changed:
        await uow.commit()
    # A fresh read of the head row: the unit of work has expired its instances after commit.
    found = await get_contract_with_version(uow.session, contract_id)
    if found is None:  # pragma: no cover - the row was just locked
        raise _not_found()
    contract, version = found
    return _metadata(contract, version, content_visible=True)


@router.get(
    "/contracts/{contract_id}",
    response_model=ContractMetadataOut,
    dependencies=[require("contract:view", target=contract_folder)],
    responses={
        404: {
            "description": (
                "Contract not found, not visible to the caller, or (for a caller who may read "
                "published versions only) without a published version"
            )
        }
    },
)
async def get_contract(
    contract_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> ContractMetadataOut:
    """Contract metadata of the head row; content-derived fields are null without ``contract:read``.

    The head row is the Latest once the contract has one, else the newest draft.
    ``contract:view`` admits every role that can see contracts; a caller who holds
    ``contract:read-published`` only gets the Latest (published content they may fetch anyway),
    and the unknown-contract 404 for a contract without one.
    """
    contract, version = await _load(session, contract_id)
    permissions = current_decision(request).permissions
    if "contract:read-metadata" in permissions:
        return _metadata(contract, version, content_visible="contract:read" in permissions)
    if "contract:read-published" in permissions and contract.latest_version_id == version.id:
        return _metadata(contract, version, content_visible=True)
    # A published-only reader and a contract without a Latest: record the refusal (WR-01).
    raise await recorded_refusal(request, "contract:read-metadata")


@router.get(
    "/contracts/{contract_id}/document",
    response_class=Response,
    dependencies=[require("contract:read", target=contract_folder)],
    responses={
        200: {
            "description": (
                "The exact bytes that were submitted, with the submitted media type. An Accept "
                "preference for the other format (application/json or application/yaml) returns "
                "a converted rendering, which is not byte-faithful."
            ),
            "content": _DOCUMENT_MEDIA_TYPES,
        },
        404: {"description": "Contract not found, or not visible to the caller"},
        406: {"description": "Accept names no representation this service can produce"},
    },
)
async def get_contract_document(
    contract_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> Response:
    _, version = await _load(session, contract_id)  # the head row, superseded by drafts/versions
    # Authorization already ran (the policy dependency), so Accept is only looked at for a caller
    # who may read the contract. The default is the submitted bytes; only an explicit preference
    # for the other format converts.
    target = negotiate(request.headers.get("accept"), version.media_type)
    if target == "original":
        content, media_type = version.raw_document, version.media_type
    else:
        # Parsing and rendering are CPU-bound: keep them off the event loop (T-01-52).
        content, media_type = await asyncio.to_thread(
            render, version.raw_document, version.media_type, target
        )
    # One best-effort access event per content 200 (D-171); sync and non-raising.
    record_content_read(
        request,
        contract_id=contract_id,
        ref="head",
        media_type=media_type,
        version_id=version.id,
        semver=version.semver,
    )
    # The Content-Type is set as a header so Starlette cannot append a charset to it, and nosniff
    # keeps a browser from reinterpreting stored bytes (T-01-47). The representation depends on
    # Accept, so caches must key on it.
    return Response(
        content=content,
        headers={
            "Content-Type": media_type,
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Vary": "Accept",
        },
    )


__all__ = [
    "ContractCreatedOut",
    "ContractDraftRefOut",
    "ContractFromTemplateIn",
    "ContractFromVersionIn",
    "ContractLatestVersionOut",
    "ContractListOut",
    "ContractMetadataOut",
    "ContractStarterIn",
    "ContractStructuralReportOut",
    "ContractTemplateOriginOut",
    "ContractVersionOriginOut",
    "ContractVersionSummaryOut",
    "contract_folder",
    "contract_folder_any",
    "require_writable_folder",
    "router",
]
