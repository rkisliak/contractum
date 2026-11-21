"""Template authoring routes: ``/api/v1/contracts/templates`` (TMPL-01, RBAC-06, D-35 to D-37).

A template is an editable draft plus immutable, numbered published versions. Like contract
create, the write routes take the raw document bytes (YAML or JSON) with no Pydantic body
parameter: the policy dependency authenticates and authorises first and the handler reads the
body afterwards. The display ``name`` is a query parameter because the body is the document.

A caller who may not read a template gets 404 with the same body whether or not it exists.
``folder_id``, ``ancestor_ids`` and ``organization_id`` come from identity's folder facts.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Annotated, NamedTuple

from dc_auth.deps import (
    UnitOfWorkDep,
    authenticate,
    current_decision,
    recorded_refusal,
    require,
)
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.names import NameRule
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from dc_odcs import ALLOWED_MEDIA_TYPES
from fastapi import Depends, Path, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.api.contracts import ContractStructuralReportOut, _report_out
from dc_contracts.convert import negotiate, render
from dc_contracts.ingest import parse_upload, read_body_capped, structural_report
from dc_contracts.models import Template, TemplateVersion
from dc_contracts.search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    decode_cursor,
    encode_cursor,
    reject_nul,
    scope_clause,
)
from dc_contracts.settings import ContractsSettings
from dc_contracts.templates import (
    available_templates,
    create_template,
    deprecate_version,
    not_found,
    publish_template,
    resolve_version,
    template_folder,
    update_draft,
)

router = PolicyRouter(prefix="/api/v1/contracts")

NAME_MAX_CHARS = 200

_BINARY = {"schema": {"type": "string", "format": "binary"}}
_DOCUMENT_MEDIA_TYPES = {media_type: _BINARY for media_type in sorted(ALLOWED_MEDIA_TYPES)}
_WRITE_BODY = {"requestBody": {"required": True, "content": _DOCUMENT_MEDIA_TYPES}}

# The database column is a 32-bit integer; a larger path value would raise a DataError (a 500).
VersionNo = Annotated[int, Path(ge=1, le=2_147_483_647)]

NameQuery = Annotated[
    str,
    Query(min_length=1, max_length=NAME_MAX_CHARS, description="Display name of the template"),
    NameRule,
]


class ContractTemplateOut(BaseModel):
    """Template metadata. Every draft-derived field is optional: a reader of published versions
    only (a Global reader) gets them null with ``draft_redacted`` true, and ``name``,
    ``updated_by`` and ``updated_at`` come from the latest published version, never the draft."""

    id: uuid.UUID
    folder_id: uuid.UUID
    organization_id: uuid.UUID | None
    name: str
    publish_counter: int
    latest_version_no: int | None
    draft_redacted: bool
    draft_api_version: str | None
    draft_media_type: str | None
    draft_content_sha256: str | None
    draft_size_bytes: int | None
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class ContractTemplateListOut(BaseModel):
    """A page of templates. There is deliberately no total or count (it would leak existence)."""

    items: list[ContractTemplateOut]
    next_cursor: str | None


class ContractTemplateVersionOut(BaseModel):
    id: uuid.UUID
    template_id: uuid.UUID
    version_no: int
    name: str
    odcs_api_version: str
    media_type: str
    content_sha256: str
    size_bytes: int
    deprecated: bool
    deprecated_at: datetime | None
    deprecated_by: str | None
    created_by: str
    created_at: datetime


class ContractTemplateVersionListOut(BaseModel):
    """Published versions in ascending order; there is no total or count."""

    items: list[ContractTemplateVersionOut]


class ContractAvailableTemplateOut(BaseModel):
    """A published template usable at a folder: only non-deprecated versions are listed."""

    template_id: uuid.UUID
    name: str
    folder_id: uuid.UUID
    latest_version_no: int
    version_nos: list[int]


class ContractAvailableTemplateListOut(BaseModel):
    """A page of available templates. There is deliberately no total or count."""

    items: list[ContractAvailableTemplateOut]
    next_cursor: str | None


class ContractTemplateWriteOut(BaseModel):
    template: ContractTemplateOut
    structural_validation: ContractStructuralReportOut


class ContractTemplatePublishOut(BaseModel):
    template: ContractTemplateOut
    version: ContractTemplateVersionOut
    structural_validation: ContractStructuralReportOut


class _PublishedFacts(NamedTuple):
    """The only name and edit attribution a published-only reader may see (A4)."""

    name: str
    by: str
    at: datetime


async def _published_facts(
    session: AsyncSession, template_ids: list[uuid.UUID]
) -> dict[uuid.UUID, _PublishedFacts]:
    """Name, publisher and publish time of each template's highest published version.

    ``Template.name`` and ``updated_*`` follow unpublished draft edits, so a reader who may not
    see drafts gets these from the latest immutable version instead.
    """
    if not template_ids:
        return {}
    rows = await session.execute(
        select(
            TemplateVersion.template_id,
            TemplateVersion.name,
            TemplateVersion.created_by,
            TemplateVersion.created_at,
        )
        .where(TemplateVersion.template_id.in_(template_ids))
        .ext(distinct_on(TemplateVersion.template_id))
        .order_by(TemplateVersion.template_id, TemplateVersion.version_no.desc())
    )
    return {row[0]: _PublishedFacts(row[1], row[2], row[3]) for row in rows}


def _template_out(
    template: Template,
    *,
    drafts_visible: bool = True,
    published: _PublishedFacts | None = None,
) -> ContractTemplateOut:
    """``published`` is required when ``drafts_visible`` is false: nothing draft-derived leaks."""
    if drafts_visible:
        name, updated_by, updated_at = template.name, template.updated_by, template.updated_at
    else:
        if published is None:  # pragma: no cover - a visible template has a published version
            raise not_found()
        name, updated_by, updated_at = published.name, published.by, published.at
    return ContractTemplateOut(
        id=template.id,
        folder_id=template.folder_id,
        organization_id=template.organization_id,
        name=name,
        publish_counter=template.publish_counter,
        latest_version_no=template.publish_counter or None,
        draft_redacted=not drafts_visible,
        draft_api_version=template.draft_api_version if drafts_visible else None,
        draft_media_type=template.draft_media_type if drafts_visible else None,
        draft_content_sha256=template.draft_content_sha256 if drafts_visible else None,
        draft_size_bytes=template.draft_size_bytes if drafts_visible else None,
        created_by=template.created_by,
        created_at=template.created_at,
        updated_by=updated_by,
        updated_at=updated_at,
    )


def _version_out(version: TemplateVersion) -> ContractTemplateVersionOut:
    return ContractTemplateVersionOut(
        id=version.id,
        template_id=version.template_id,
        version_no=version.version_no,
        name=version.name,
        odcs_api_version=version.odcs_api_version,
        media_type=version.media_type,
        content_sha256=version.content_sha256,
        size_bytes=version.size_bytes,
        deprecated=version.deprecated_at is not None,
        deprecated_at=version.deprecated_at,
        deprecated_by=version.deprecated_by,
        created_by=version.created_by,
        created_at=version.created_at,
    )


def _archived() -> ProblemError:
    return ProblemError(
        409, "folder-archived", "Conflict", "The folder is archived; its subtree is read-only."
    )


def _drafts_visible(request: Request) -> bool:
    """Whether the caller holds template:read here (a manager), as opposed to published-only."""
    return "template:read" in current_decision(request).permissions


async def _require_published_access(
    request: Request, template: Template, drafts_visible: bool
) -> None:
    """A published-only reader (A4) gets the 404 of an unknown id for a never-published template.

    The PDP is asked for ``template:read`` first, so the refusal is a recorded denial (WR-01).
    """
    if not drafts_visible and template.publish_counter == 0:
        raise await recorded_refusal(request, "template:read")


async def _path_folder_id(folder_id: uuid.UUID) -> uuid.UUID:
    return folder_id


async def _load_template(session: AsyncSession, template_id: uuid.UUID) -> Template:
    template = (
        await session.execute(select(Template).where(Template.id == template_id))
    ).scalar_one_or_none()
    if template is None:  # pragma: no cover - template_folder already answered 404
        raise not_found()
    return template


async def _load_version(
    session: AsyncSession, template_id: uuid.UUID, version_no: int
) -> TemplateVersion:
    version = (
        await session.execute(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version_no == version_no,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise not_found()
    return version


async def _document_response(request: Request, raw: bytes, stored_media_type: str) -> Response:
    """Stored bytes by default; only an explicit Accept for the other format converts."""
    target = negotiate(request.headers.get("accept"), stored_media_type)
    if target == "original":
        content, media_type = raw, stored_media_type
    else:
        # Parsing and rendering are CPU-bound: keep them off the event loop (T-01-52).
        content, media_type = await asyncio.to_thread(render, raw, stored_media_type, target)
    return Response(
        content=content,
        headers={
            "Content-Type": media_type,
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Vary": "Accept",
        },
    )


_WRITE_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {"description": "Template or folder not found, or not visible to the caller"},
    409: {"description": "Folder is archived"},
    413: {"description": "Document larger than the size cap"},
    415: {"description": "Content-Type is not YAML or JSON"},
    422: {
        "description": "Malformed document, unsupported ODCS version, or failed apiVersion/kind/id "
        "gate"
    },
    503: {"description": "Authorization is temporarily unavailable"},
}


@router.post(
    "/folders/{folder_id}/templates",
    status_code=201,
    response_model=ContractTemplateWriteOut,
    dependencies=[require("template:create", target=_path_folder_id)],
    openapi_extra=_WRITE_BODY,
    responses={
        **_WRITE_RESPONSES,
        403: {"description": "The caller can read the folder but may not create templates in it"},
    },
)
async def create_template_route(
    folder_id: uuid.UUID,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    name: NameQuery,
) -> ContractTemplateWriteOut:
    """Create a template draft in a folder of any kind from a raw ODCS 3.1/3.2 document."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise not_found()
    if facts.archived:
        raise _archived()
    reject_nul(name=name)
    settings: ContractsSettings = request.app.state.settings
    raw = await read_body_capped(request, settings.max_document_bytes)
    parsed = await parse_upload(raw, request.headers.get("content-type", ""), settings)
    report = await structural_report(parsed)

    template = await create_template(uow, facts, parsed, raw, name=name)
    # Commit before answering: the client only sees 201 once the row and its event are durable.
    await uow.commit()

    response.headers["Location"] = f"/api/v1/contracts/templates/{template.id}"
    return ContractTemplateWriteOut(
        template=_template_out(template), structural_validation=_report_out(report)
    )


NameOptionalQuery = Annotated[
    str | None,
    Query(min_length=1, max_length=NAME_MAX_CHARS, description="New display name (optional)"),
    NameRule,
]


@router.get(
    "/templates",
    response_model=ContractTemplateListOut,
    dependencies=[require("template:list")],
    responses={422: {"description": "Invalid limit, cursor or filter value"}},
)
async def list_templates(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    folder_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> ContractTemplateListOut:
    """List the templates the caller may see, in id order with a keyset cursor.

    Managers (template:read) see every template in their scope; a published-only reader
    sees only templates with at least one published version, and never a draft field.
    """
    after = decode_cursor(cursor) if cursor is not None else None
    authorizer = request.app.state.authorizer_factory(request)
    read = await authorizer.accessible_folders(principal, "template:read")
    published = await authorizer.accessible_folders(principal, "template:read-published")
    read_clause = scope_clause(
        read, folder_col=Template.folder_id, ancestors_col=Template.ancestor_ids
    )
    published_clause = scope_clause(
        published, folder_col=Template.folder_id, ancestors_col=Template.ancestor_ids
    )
    visible = []
    if read_clause is not None:
        visible.append(read_clause)
    if published_clause is not None:
        visible.append(and_(published_clause, Template.publish_counter > 0))
    if not visible:  # no scope at all: an empty page without a query
        return ContractTemplateListOut(items=[], next_cursor=None)

    stmt = select(Template).where(or_(*visible))
    if folder_id is not None:
        stmt = stmt.where(Template.folder_id == folder_id)
    if after is not None:
        stmt = stmt.where(Template.id > after)
    stmt = stmt.order_by(Template.id).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    page = rows[:limit]
    # A row covered only by the published scope never exposes draft-derived fields.
    visible_drafts = {t.id: read.covers(t.folder_id, t.ancestor_ids) for t in page}
    published = await _published_facts(
        session, [i for i, shown in visible_drafts.items() if not shown]
    )
    return ContractTemplateListOut(
        items=[
            _template_out(t, drafts_visible=visible_drafts[t.id], published=published.get(t.id))
            for t in page
        ],
        next_cursor=encode_cursor(page[-1].id) if len(rows) > limit else None,
    )


@router.get(
    "/templates/{template_id}",
    response_model=ContractTemplateOut,
    dependencies=[require("template:read-published", target=template_folder)],
    responses={404: {"description": "Template not found, or not visible to the caller"}},
)
async def get_template(
    template_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> ContractTemplateOut:
    drafts_visible = _drafts_visible(request)
    template = await _load_template(session, template_id)
    await _require_published_access(request, template, drafts_visible)
    published = (
        None
        if drafts_visible
        else (await _published_facts(session, [template.id])).get(template.id)
    )
    return _template_out(template, drafts_visible=drafts_visible, published=published)


@router.get(
    "/templates/{template_id}/document",
    response_class=Response,
    dependencies=[require("template:read", target=template_folder)],
    responses={
        200: {
            "description": "The exact draft bytes with the submitted media type. An Accept "
            "preference for the other format returns a converted rendering.",
            "content": _DOCUMENT_MEDIA_TYPES,
        },
        404: {"description": "Template not found, or not visible to the caller"},
        406: {"description": "Accept names no representation this service can produce"},
    },
)
async def get_template_draft_document(
    template_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> Response:
    template = await _load_template(session, template_id)
    return await _document_response(request, template.draft_raw_document, template.draft_media_type)


@router.put(
    "/templates/{template_id}/draft",
    response_model=ContractTemplateWriteOut,
    dependencies=[require("template:update", target=template_folder)],
    openapi_extra=_WRITE_BODY,
    responses=_WRITE_RESPONSES,
)
async def update_template_draft(
    template_id: uuid.UUID, request: Request, uow: UnitOfWorkDep, name: NameOptionalQuery = None
) -> ContractTemplateWriteOut:
    """Replace the draft with a new raw document; unchanged bytes and name record nothing."""
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    reject_nul(name=name)
    settings: ContractsSettings = request.app.state.settings
    raw = await read_body_capped(request, settings.max_document_bytes)
    parsed = await parse_upload(raw, request.headers.get("content-type", ""), settings)
    report = await structural_report(parsed)

    template, changed = await update_draft(uow, template_id, parsed, raw, name=name)
    if changed:
        await uow.commit()
    return ContractTemplateWriteOut(
        template=_template_out(template), structural_validation=_report_out(report)
    )


@router.post(
    "/templates/{template_id}/publish",
    status_code=201,
    response_model=ContractTemplatePublishOut,
    dependencies=[require("template:publish", target=template_folder)],
    responses={
        200: {
            "description": "The draft is identical to the latest non-deprecated version; that "
            "version is returned and nothing is recorded",
            "model": ContractTemplatePublishOut,
        },
        **_WRITE_RESPONSES,
    },
)
async def publish_template_route(
    template_id: uuid.UUID, request: Request, response: Response, uow: UnitOfWorkDep
) -> ContractTemplatePublishOut:
    """Publish the draft as the next immutable version (201), or 200 when nothing changed."""
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    settings: ContractsSettings = request.app.state.settings
    template, version, created, report = await publish_template(uow, template_id, settings)
    if created:
        await uow.commit()
        response.headers["Location"] = (
            f"/api/v1/contracts/templates/{template.id}/versions/{version.version_no}/document"
        )
    else:
        response.status_code = 200
    return ContractTemplatePublishOut(
        template=_template_out(template),
        version=_version_out(version),
        structural_validation=_report_out(report),
    )


@router.get(
    "/templates/{template_id}/versions/{version_no}/document",
    response_class=Response,
    dependencies=[require("template:read-published", target=template_folder)],
    responses={
        200: {
            "description": "The exact bytes of the published version, with the submitted media "
            "type. An Accept preference for the other format returns a converted rendering.",
            "content": _DOCUMENT_MEDIA_TYPES,
        },
        404: {"description": "Template or version not found, or not visible to the caller"},
        406: {"description": "Accept names no representation this service can produce"},
    },
)
async def get_template_version_document(
    template_id: uuid.UUID,
    version_no: VersionNo,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> Response:
    await _require_published_access(
        request, await _load_template(session, template_id), _drafts_visible(request)
    )
    version = await _load_version(session, template_id, version_no)
    return await _document_response(request, version.raw_document, version.media_type)


@router.get(
    "/folders/{folder_id}/available-templates",
    response_model=ContractAvailableTemplateListOut,
    dependencies=[require("contract:create", target=_path_folder_id)],
    responses={
        403: {"description": "The caller can read the folder but may not create contracts in it"},
        404: {"description": "Folder not found, or not visible to the caller"},
        422: {"description": "Invalid limit or cursor"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def list_available_templates(
    folder_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> ContractAvailableTemplateListOut:
    """Published, non-deprecated templates of this folder and every ancestor.

    Computed from the folder's ancestors, so block-inheritance flags never hide a template from a
    caller who may create contracts here. Allowed at any folder kind; a contract may be started from
    an available template in any non-archived folder where the caller holds ``contract:create``.
    """
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise not_found()
    after = decode_cursor(cursor) if cursor is not None else None
    found, has_more = await available_templates(session, facts, limit=limit, cursor=after)
    return ContractAvailableTemplateListOut(
        items=[
            ContractAvailableTemplateOut(
                template_id=template.id,
                name=template.name,
                folder_id=template.folder_id,
                latest_version_no=version_nos[-1],
                version_nos=version_nos,
            )
            for template, version_nos in found
        ],
        next_cursor=encode_cursor(found[-1][0].id) if has_more else None,
    )


@router.get(
    "/folders/{folder_id}/available-templates/{template_id}/versions/{version_no}/document",
    response_class=Response,
    dependencies=[require("contract:create", target=_path_folder_id)],
    responses={
        200: {
            "description": "The exact bytes of the published version, with the submitted media "
            "type. An Accept preference for the other format returns a converted rendering.",
            "content": _DOCUMENT_MEDIA_TYPES,
        },
        403: {"description": "The caller can read the folder but may not create contracts in it"},
        404: {
            "description": "Folder, template or version not found, not available at this "
            "folder, or deprecated"
        },
        406: {"description": "Accept names no representation this service can produce"},
    },
)
async def preview_available_template_version(
    folder_id: uuid.UUID,
    template_id: uuid.UUID,
    version_no: VersionNo,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> Response:
    """Preview a version a caller could start a contract from (hidden once deprecated)."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise not_found()
    version = await resolve_version(session, facts, template_id, version_no, hide_deprecated=True)
    return await _document_response(request, version.raw_document, version.media_type)


__all__ = [
    "ContractAvailableTemplateListOut",
    "ContractAvailableTemplateOut",
    "ContractTemplateListOut",
    "ContractTemplateOut",
    "ContractTemplatePublishOut",
    "ContractTemplateVersionListOut",
    "ContractTemplateVersionOut",
    "ContractTemplateWriteOut",
    "router",
]


@router.get(
    "/templates/{template_id}/versions",
    response_model=ContractTemplateVersionListOut,
    dependencies=[require("template:read-published", target=template_folder)],
    responses={404: {"description": "Template not found, or not visible to the caller"}},
)
async def list_template_versions(
    template_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> ContractTemplateVersionListOut:
    await _require_published_access(
        request, await _load_template(session, template_id), _drafts_visible(request)
    )
    rows = (
        (
            await session.execute(
                select(TemplateVersion)
                .where(TemplateVersion.template_id == template_id)
                .order_by(TemplateVersion.version_no)
            )
        )
        .scalars()
        .all()
    )
    return ContractTemplateVersionListOut(items=[_version_out(v) for v in rows])


@router.post(
    "/templates/{template_id}/versions/{version_no}/deprecate",
    response_model=ContractTemplateVersionOut,
    dependencies=[require("template:deprecate", target=template_folder)],
    responses={
        404: {"description": "Template or version not found, or not visible to the caller"},
        409: {"description": "Folder is archived"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def deprecate_template_version(
    template_id: uuid.UUID, version_no: VersionNo, request: Request, uow: UnitOfWorkDep
) -> ContractTemplateVersionOut:
    """Hide a version from new use (200); an already deprecated version is a no-op (200)."""
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    version, changed = await deprecate_version(uow, template_id, version_no, actor_id=uow.actor.id)
    if changed:
        await uow.commit()
    return _version_out(version)
