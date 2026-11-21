"""Template domain service: create, publish and (later) edit and deprecate (TMPL-01, D-37).

A template is one editable draft (``Template.draft_*``, byte-faithful) plus immutable numbered
versions (``TemplateVersion``). Publishing locks the template row, so concurrent publishes
serialise: version numbers have no gaps or duplicates, and a second publish of the same draft is
an idempotent no-op that returns the version the first one created.

Templates are also *used* below their folder (TMPL-02, CONT-02, D-37, D-38): ``available_templates``
and ``resolve_version`` are keyed on the target folder's ancestor ids from identity's folder facts,
never on the caller's grant chain, so a block-inheritance flag cannot hide an Organization or ROOT
template. ``build_from_template`` rebuilds a document as a copy with a new id.

Every write goes through the caller's ``UnitOfWork`` and records exactly one metadata-only audit
event (never document content). Callers authorise first; ``folder_id``, ``ancestor_ids`` and
``organization_id`` come only from identity's folder facts.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Any

from dc_auth.authorizer import FolderFacts
from dc_core.app import read_session
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from dc_odcs import ParsedContract, StructuralReport, load_json, load_yaml
from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.convert import JSON_TYPE
from dc_contracts.ingest import parse_upload, structural_report
from dc_contracts.models import Template, TemplateVersion
from dc_contracts.settings import ContractsSettings

# Top-level keys a create-from-template request may not replace (T-02-47): the version and kind of
# the standard, and the contract id, which is always the caller's new ODCS id.
FORBIDDEN_OVERRIDES: frozenset[str] = frozenset({"apiVersion", "kind", "id"})

_BOM = "\ufeff"

# Free text is bounded in events so one event can never outgrow the NATS max_payload (WR-01).
AUDIT_NAME_MAX_CHARS = 200


def not_found() -> ProblemError:
    """The one 404 for an unknown template and for one the caller may not read (D-35)."""
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


async def template_folder(
    template_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    """Route target resolver: the folder that holds the template (unknown id -> same 404)."""
    folder_id = (
        await session.execute(select(Template.folder_id).where(Template.id == template_id))
    ).scalar_one_or_none()
    if folder_id is None:
        raise not_found()
    return folder_id


def _bounded(name: str) -> str:
    return name[:AUDIT_NAME_MAX_CHARS]


def template_snapshot(template: Template) -> dict[str, Any]:
    """Allow-listed audit view of a template; the name is bounded, documents are never included."""
    snapshot = UnitOfWork.snapshot(template)
    snapshot["name"] = _bounded(template.name)
    return snapshot


def version_snapshot(version: TemplateVersion) -> dict[str, Any]:
    snapshot = UnitOfWork.snapshot(version)
    snapshot["name"] = _bounded(version.name)
    return snapshot


async def create_template(
    uow: UnitOfWork, facts: FolderFacts, parsed: ParsedContract, raw: bytes, *, name: str
) -> Template:
    """Insert a template with its first draft; one ``template.created`` event (counter 0)."""
    session = uow.session
    template = Template(
        folder_id=facts.id,
        ancestor_ids=list(facts.ancestor_ids),
        organization_id=facts.organization_id,  # None for ROOT, which has no organization
        name=name,
        draft_raw_document=raw,
        draft_media_type=parsed.media_type,
        draft_content_sha256=parsed.content_sha256,
        draft_size_bytes=parsed.size_bytes,
        draft_document=parsed.document,
        draft_api_version=parsed.api_version,
        publish_counter=0,
    )
    session.add(template)
    await session.flush()
    await session.refresh(template)
    uow.audit(
        "template.created",
        "template",
        template.id,
        before=None,
        after=template_snapshot(template),
        scope_path=facts.ancestor_ids,
        aggregate_version=0,
    )
    return template


async def locked_template(session: AsyncSession, template_id: uuid.UUID) -> Template:
    """The template row under ``SELECT ... FOR UPDATE`` (serialises writers); 404 when unknown."""
    template = (
        await session.execute(select(Template).where(Template.id == template_id).with_for_update())
    ).scalar_one_or_none()
    if template is None:
        raise not_found()
    return template


async def latest_version(
    session: AsyncSession, template_id: uuid.UUID, *, include_deprecated: bool
) -> TemplateVersion | None:
    """The highest-numbered version, optionally ignoring deprecated ones."""
    stmt = select(TemplateVersion).where(TemplateVersion.template_id == template_id)
    if not include_deprecated:
        stmt = stmt.where(TemplateVersion.deprecated_at.is_(None))
    stmt = stmt.order_by(TemplateVersion.version_no.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def publish_template(
    uow: UnitOfWork, template_id: uuid.UUID, settings: ContractsSettings
) -> tuple[Template, TemplateVersion, bool, StructuralReport]:
    """Publish the draft as the next version; ``bool`` is True when a version was created.

    The draft bytes are re-run through the D-19 gate (cheap, and the stored draft was gated at
    save time with the same code). A draft identical to the latest non-deprecated version is not
    published again: that version is returned with ``False`` and no event is recorded.
    """
    session = uow.session
    template = await locked_template(session, template_id)
    parsed = await parse_upload(template.draft_raw_document, template.draft_media_type, settings)
    report = await structural_report(parsed)

    current = await latest_version(session, template.id, include_deprecated=False)
    if current is not None and current.content_sha256 == template.draft_content_sha256:
        return template, current, False, report

    before = template_snapshot(template)
    template.publish_counter += 1
    version = TemplateVersion(
        template_id=template.id,
        version_no=template.publish_counter,
        odcs_api_version=template.draft_api_version,
        raw_document=template.draft_raw_document,
        media_type=template.draft_media_type,
        content_sha256=template.draft_content_sha256,
        size_bytes=template.draft_size_bytes,
        document=template.draft_document,
        name=template.name,
    )
    session.add(version)
    await session.flush()
    await session.refresh(template)
    await session.refresh(version)
    uow.audit(
        "template.published",
        "template",
        template.id,
        before=before,
        after={**template_snapshot(template), "version": version_snapshot(version)},
        scope_path=template.ancestor_ids,
        aggregate_version=version.version_no,
    )
    return template, version, True, report


async def update_draft(
    uow: UnitOfWork,
    template_id: uuid.UUID,
    parsed: ParsedContract,
    raw: bytes,
    *,
    name: str | None,
) -> tuple[Template, bool]:
    """Replace the draft bytes (and optionally the name); ``bool`` is True when anything changed.

    Unchanged bytes (same hash and media type) and an unchanged or absent name are a no-op that
    records no event. Last write wins: there is no draft revision in Phase 2 (OQ7).
    """
    session = uow.session
    template = await locked_template(session, template_id)
    new_name = template.name if name is None else name
    if (
        template.draft_content_sha256 == parsed.content_sha256
        and template.draft_media_type == parsed.media_type
        and template.name == new_name
    ):
        return template, False
    before = template_snapshot(template)
    template.name = new_name
    template.draft_raw_document = raw
    template.draft_media_type = parsed.media_type
    template.draft_content_sha256 = parsed.content_sha256
    template.draft_size_bytes = parsed.size_bytes
    template.draft_document = parsed.document
    template.draft_api_version = parsed.api_version
    await session.flush()
    await session.refresh(template)
    uow.audit(
        "template.draft_updated",
        "template",
        template.id,
        before=before,
        after=template_snapshot(template),
        scope_path=template.ancestor_ids,
        aggregate_version=template.publish_counter,
    )
    return template, True


async def deprecate_version(
    uow: UnitOfWork, template_id: uuid.UUID, version_no: int, *, actor_id: str
) -> tuple[TemplateVersion, bool]:
    """Hide a published version from new use; ``bool`` is True when it was newly deprecated.

    The template row is locked first (the same order as publish), so a deprecation and a publish
    of the same template cannot interleave. An already deprecated version is a no-op, no event.
    The content stays readable and contracts made from it keep their reference.
    """
    session = uow.session
    template = await locked_template(session, template_id)
    version = (
        await session.execute(
            select(TemplateVersion)
            .where(
                TemplateVersion.template_id == template.id,
                TemplateVersion.version_no == version_no,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if version is None:
        raise not_found()
    if version.deprecated_at is not None:
        return version, False
    before = version_snapshot(version)
    version.deprecated_at = func.now()
    version.deprecated_by = actor_id
    await session.flush()
    await session.refresh(version)
    uow.audit(
        "template.version_deprecated",
        "template",
        template.id,
        before=before,
        after=version_snapshot(version),
        scope_path=template.ancestor_ids,
        aggregate_version=version.version_no,
    )
    return version, True


async def available_templates(
    session: AsyncSession, facts: FolderFacts, *, limit: int, cursor: uuid.UUID | None
) -> tuple[list[tuple[Template, list[int]]], bool]:
    """Templates usable at a folder: those held by the folder or any ancestor (TMPL-02, D-37).

    Only templates with at least one non-deprecated version appear, each with its sorted
    non-deprecated version numbers. Ordered by template id with a keyset cursor; the ``limit + 1``
    row only answers "is there a next page". Keyed on ``facts.ancestor_ids``, never on the
    caller's grants, so block-inheritance flags cannot hide a template from a legitimate editor.
    """
    usable = (
        select(TemplateVersion.id)
        .where(
            TemplateVersion.template_id == Template.id,
            TemplateVersion.deprecated_at.is_(None),
        )
        .exists()
    )
    stmt = select(Template).where(Template.folder_id.in_(facts.ancestor_ids), usable)
    if cursor is not None:
        stmt = stmt.where(Template.id > cursor)
    stmt = stmt.order_by(Template.id).limit(limit + 1)
    templates = list((await session.execute(stmt)).scalars().all())
    has_more = len(templates) > limit
    templates = templates[:limit]
    if not templates:
        return [], False
    rows = (
        await session.execute(
            select(TemplateVersion.template_id, TemplateVersion.version_no)
            .where(
                TemplateVersion.template_id.in_([t.id for t in templates]),
                TemplateVersion.deprecated_at.is_(None),
            )
            .order_by(TemplateVersion.template_id, TemplateVersion.version_no)
        )
    ).all()
    by_template: dict[uuid.UUID, list[int]] = {t.id: [] for t in templates}
    for template_id, version_no in rows:
        by_template[template_id].append(version_no)
    return [(t, by_template[t.id]) for t in templates], has_more


async def resolve_version(
    session: AsyncSession,
    facts: FolderFacts,
    template_id: uuid.UUID,
    version_no: int | None,
    *,
    hide_deprecated: bool = False,
) -> TemplateVersion:
    """The template version to start a contract from, or a problem.

    The template must be held by the target folder or one of its ancestors (404 otherwise: keyed
    on the folder, never on the caller's grants, D-37). An explicit deprecated version is 422
    ``template-version-deprecated`` (A8); an unknown one is 404. Without a version number the
    latest non-deprecated version is used (404 when there is none). With ``hide_deprecated`` (the
    availability preview) a deprecated version is a 404 like an unknown one (A8).
    """
    template = (
        await session.execute(select(Template).where(Template.id == template_id))
    ).scalar_one_or_none()
    if template is None or template.folder_id not in facts.ancestor_ids:
        raise not_found()
    if version_no is None:
        latest = await latest_version(session, template.id, include_deprecated=False)
        if latest is None:
            raise not_found()
        return latest
    version = (
        await session.execute(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template.id,
                TemplateVersion.version_no == version_no,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise not_found()
    if version.deprecated_at is not None:
        if hide_deprecated:
            raise not_found()
        raise ProblemError(
            422,
            "template-version-deprecated",
            "Unprocessable Content",
            "This template version is deprecated and cannot start new contracts.",
        )
    return version


def build_from_template(
    version: TemplateVersion, *, odcs_id: str, overrides: Mapping[str, Any]
) -> bytes:
    """The new contract's document bytes: the template's document, overrides applied, new id.

    CPU-bound; callers run it with ``asyncio.to_thread``. The template's raw bytes are re-parsed
    with the strict ODCS loaders (original key order is kept, the jsonb copy is never used) and
    re-serialised as JSON, so YAML comments of the template are not carried (OQ8). Overrides
    replace top-level keys; ``apiVersion``, ``kind`` and ``id`` cannot be overridden (422).
    """
    forbidden = sorted(FORBIDDEN_OVERRIDES.intersection(overrides))
    if forbidden:
        raise ProblemError(
            422,
            "template-override-not-allowed",
            "Unprocessable Content",
            f"These top-level fields cannot be overridden: {', '.join(forbidden)}.",
        )
    text = version.raw_document.decode("utf-8")
    if text.startswith(_BOM):
        text = text[1:]
    document = load_json(text) if version.media_type == JSON_TYPE else load_yaml(text)
    for key, value in overrides.items():
        document[key] = value
    document["id"] = odcs_id
    try:
        return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    except UnicodeEncodeError as exc:  # a lone surrogate in an override cannot be stored as UTF-8
        raise ProblemError(
            422,
            "template-override-not-allowed",
            "Unprocessable Content",
            "An override value contains text that is not valid Unicode.",
        ) from exc
