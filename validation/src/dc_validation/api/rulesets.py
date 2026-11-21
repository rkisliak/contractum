"""Rule-set authoring routes: ``/api/v1/validation/...`` (RULE-01..04, D-113, D-114, D-116, D-117).

Policy Managers create a rule set in a folder, edit its one draft, check it on demand and publish
it as an immutable version. Authority comes from identity's PDP only: ``require()`` asks it, and
the folder facts it returns (never anything from the client) place a new rule set (D-113).

The draft save takes the whole draft as a JSON or YAML document and has no Pydantic body
parameter: the policy dependency authenticates and authorises first, and the handler reads the
capped body afterwards, so an unauthorised caller never makes the service buffer or parse anything.
A draft save never fails because of rule problems; the advisory check report rides on the answer.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from dc_auth.authorizer import FolderFacts
from dc_auth.deps import UnitOfWorkDep, authenticate, current_decision, parse_bearer, require
from dc_auth.principal import Principal, principal_to_actor
from dc_core.app import read_session
from dc_core.body import read_body_capped
from dc_core.names import NameRule
from dc_core.nul import find_unstorable, nul_problem
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from dc_odcs import ALLOWED_MEDIA_TYPES, load_json, load_yaml
from dc_rules.model import RuleDef, RuleSetContent
from fastapi import Depends, Path, Request, Response
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_validation.models import RuleSet, RuleSetVersion
from dc_validation.resolution import ResolvedRuleSet
from dc_validation.rulesets import (
    _content_of,
    available_rule_set,
    available_rule_sets,
    create_rule_set,
    delete_rule,
    delete_rule_set,
    deprecate_version,
    enforce_now,
    etag_for_rule_set,
    list_rule_sets,
    list_versions,
    not_found,
    publish_rule_set,
    put_rule,
    reschedule_enforcement,
    resolved_mandatory,
    rule_set_folder,
    run_check,
    save_draft,
    set_mandatory,
    set_paused,
)
from dc_validation.runtime_settings import effective_warn_days
from dc_validation.settings import ValidationSettings

router = PolicyRouter(prefix="/api/v1/validation")

# The database column is a 32-bit integer; a larger path value would raise a DataError (a 500).
VersionNo = Annotated[int, Path(ge=1, le=2_147_483_647)]
# The same pattern as ``dc_rules.model.RuleKey``: a bad path key is a 422 like a bad body key.
RuleKeyPath = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
# One rule: its condition, message and up to max_fixtures_per_kind fixtures of max_fixture_bytes
# each for pass and fail; the 64 KiB allows for everything else in the rule.
_RULE_BODY_SLACK = 64 * 1024

_RULE_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "description": "One rule definition with its `pass` and `fail` fixtures. `key` may be "
        "omitted; when given it must equal the key in the path.",
        "content": {
            media_type: {"schema": {"type": "object"}} for media_type in sorted(ALLOWED_MEDIA_TYPES)
        },
    }
}

_DRAFT_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "description": "The whole draft: an object with `rules` (list of rule definitions, each "
        "with its `pass` and `fail` fixtures) and an optional `description`.",
        "content": {
            media_type: {"schema": {"type": "object"}} for media_type in sorted(ALLOWED_MEDIA_TYPES)
        },
    }
}


class RuleSetCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(min_length=1, max_length=200), NameRule]
    description: str | None = Field(default=None, max_length=2000)


class RuleSetDraftIn(BaseModel):
    """The whole draft. Rule problems are never a reason to refuse a save; shape problems are."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=2000)
    rules: list[RuleDef]


class RuleSetMandatoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandatory: bool


class RuleSetPausedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paused: bool


class RuleSetEnforceAtIn(BaseModel):
    """The new enforcement date: any timezone-aware timestamp (a naive one is a 422)."""

    model_config = ConfigDict(extra="forbid")

    enforce_at: AwareDatetime


class ResolvedEntryOut(BaseModel):
    """One version of a mandatory set that applies at a folder, and the mode it runs in.

    ``paused`` is true for a paused platform set: every entry then has mode ``warn`` whatever its
    date says.
    """

    version_no: int
    mode: Literal["enforce", "warn"]
    enforce_at: datetime | None
    paused: bool = False


class RuleSetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope: str
    folder_id: uuid.UUID | None
    folder_kind: str | None
    name: str
    description: str | None
    publish_counter: int
    draft_revision: int
    mandatory: bool
    mandatory_set_at: datetime | None
    mandatory_set_by: str | None
    paused_at: datetime | None = None
    paused_by: str | None = None
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class RuleSetDraftOut(BaseModel):
    """The draft, plus the check report when one was run (``check`` is null on a plain read)."""

    rule_set: RuleSetOut
    rules: list[RuleDef]
    check: dict[str, Any] | None = None


class RuleSetVersionOut(BaseModel):
    id: uuid.UUID
    rule_set_id: uuid.UUID
    version_no: int
    dsl_version: str
    rules: list[RuleDef]
    content_sha256: str
    rule_keys: list[str]
    deprecated_at: datetime | None
    deprecated_by: str | None
    enforce_at: datetime | None
    published_by: str
    published_at: datetime


class RuleSetListOut(BaseModel):
    items: list[RuleSetOut]


class RuleSetVersionSummaryOut(BaseModel):
    """A published version without its rules (the list shape)."""

    version_no: int
    content_sha256: str
    rule_keys: list[str]
    deprecated_at: datetime | None
    enforce_at: datetime | None
    published_at: datetime
    published_by: str


class RuleSetVersionListOut(BaseModel):
    items: list[RuleSetVersionSummaryOut]


class AvailableRuleSetOut(BaseModel):
    """A published rule set as a contract author below its folder sees it (no draft field)."""

    id: uuid.UUID
    scope: str
    folder_id: uuid.UUID | None
    folder_kind: str | None
    name: str
    description: str | None
    latest_version: int
    latest_active_version: int | None
    versions: list[RuleSetVersionSummaryOut]
    mandatory: bool
    paused: bool = False
    paused_at: datetime | None = None
    resolved: list[ResolvedEntryOut]


class AvailableRuleSetListOut(BaseModel):
    items: list[AvailableRuleSetOut]


def _archived() -> ProblemError:
    return ProblemError(
        409, "folder-archived", "Conflict", "The folder is archived; its subtree is read-only."
    )


def _rule_set_out(rule_set: RuleSet) -> RuleSetOut:
    return RuleSetOut.model_validate(rule_set)


def _draft_out(rule_set: RuleSet, check: dict[str, Any] | None) -> RuleSetDraftOut:
    return RuleSetDraftOut(
        rule_set=_rule_set_out(rule_set),
        rules=[RuleDef.model_validate(rule) for rule in rule_set.draft_rules],
        check=check,
    )


def _version_out(version: RuleSetVersion) -> RuleSetVersionOut:
    return RuleSetVersionOut(
        id=version.id,
        rule_set_id=version.rule_set_id,
        version_no=version.version_no,
        dsl_version=version.dsl_version,
        rules=[RuleDef.model_validate(rule) for rule in version.rules],
        content_sha256=version.content_sha256,
        rule_keys=list(version.rule_keys),
        deprecated_at=version.deprecated_at,
        deprecated_by=version.deprecated_by,
        enforce_at=version.enforce_at,
        published_by=version.created_by,
        published_at=version.created_at,
    )


def _version_summary(version: RuleSetVersion) -> RuleSetVersionSummaryOut:
    return RuleSetVersionSummaryOut(
        version_no=version.version_no,
        content_sha256=version.content_sha256,
        rule_keys=list(version.rule_keys),
        deprecated_at=version.deprecated_at,
        enforce_at=version.enforce_at,
        published_at=version.created_at,
        published_by=version.created_by,
    )


def _resolved_by_set(entries: list[ResolvedRuleSet]) -> dict[uuid.UUID, list[ResolvedEntryOut]]:
    grouped: dict[uuid.UUID, list[ResolvedEntryOut]] = {}
    for entry in entries:
        grouped.setdefault(entry.rule_set_id, []).append(
            ResolvedEntryOut(
                version_no=entry.version_no,
                mode=entry.mode,
                enforce_at=entry.enforce_at,
                paused=entry.paused,
            )
        )
    return grouped


def _available_out(
    rule_set: RuleSet,
    versions: list[RuleSetVersion],
    resolved: dict[uuid.UUID, list[ResolvedEntryOut]],
) -> AvailableRuleSetOut:
    active = [v.version_no for v in versions if v.deprecated_at is None]
    return AvailableRuleSetOut(
        id=rule_set.id,
        scope=rule_set.scope,
        folder_id=rule_set.folder_id,
        folder_kind=rule_set.folder_kind,
        name=rule_set.name,
        description=rule_set.description,
        latest_version=max(v.version_no for v in versions),
        latest_active_version=max(active) if active else None,
        versions=[_version_summary(v) for v in versions],
        mandatory=rule_set.mandatory,
        paused=rule_set.paused_at is not None,
        paused_at=rule_set.paused_at,
        resolved=resolved.get(rule_set.id, []) if rule_set.mandatory else [],
    )


def _caller_folder(request: Request) -> FolderFacts:
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise not_found()
    return facts


async def _path_folder_id(folder_id: uuid.UUID) -> uuid.UUID:
    return folder_id


async def _load_rule_set(session: AsyncSession, rule_set_id: uuid.UUID) -> RuleSet:
    rule_set = (
        await session.execute(select(RuleSet).where(RuleSet.id == rule_set_id))
    ).scalar_one_or_none()
    if rule_set is None:  # pragma: no cover - rule_set_folder already answered 404
        raise not_found()
    return rule_set


def _pointer(loc: tuple[Any, ...]) -> str:
    parts = (str(p).replace("~", "~0").replace("/", "~1") for p in loc)
    return "/" + "/".join(parts)


def _malformed(detail: str) -> ProblemError:
    return ProblemError(422, "malformed-document", "Unprocessable Content", detail)


def _parse_draft_text(text: str, media_type: str) -> Any:
    if media_type == "application/json":
        try:
            return load_json(text)
        except ValueError as exc:  # JSONDecodeError and JsonStrictError are ValueErrors
            raise _malformed(f"Invalid JSON: {exc}") from exc
        except RecursionError as exc:
            raise _malformed("Invalid JSON: nesting is too deep") from exc
    try:
        return load_yaml(text)
    except Exception as exc:  # PyYAML errors, alias refusals and recursion
        raise _malformed(f"Invalid YAML: {type(exc).__name__}") from exc


async def _parse_mapping(raw: bytes, content_type: str) -> dict[str, Any]:
    """Strict-parse a JSON or YAML body off the event loop into a mapping."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise ProblemError(
            415,
            "unsupported-media-type",
            "Unsupported media type",
            f"Content-Type {media_type or '(none)'!r} is not supported. Use one of: "
            f"{', '.join(sorted(ALLOWED_MEDIA_TYPES))}",
        )
    if not raw:
        raise _malformed("The request body is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _malformed(f"The document is not valid UTF-8 (byte offset {exc.start})") from exc
    text = text.removeprefix("\ufeff")
    data = await asyncio.to_thread(_parse_draft_text, text, media_type)
    if not isinstance(data, dict):
        raise _malformed("The document root must be a mapping")
    # The draft is stored as jsonb, which refuses U+0000 and lone surrogates: a 422, never a 500.
    found = await asyncio.to_thread(find_unstorable, data)
    if found is not None:
        raise nul_problem(found[0], slug="malformed-document", detail=found[1])
    return data


def _shape_problem(exc: ValidationError, detail: str) -> ProblemError:
    """422 with JSON-pointer errors that never echo the submitted input."""
    errors = [
        {"pointer": _pointer(tuple(err.get("loc", ()))), "detail": str(err.get("msg", ""))}
        for err in exc.errors()
    ]
    return ProblemError(422, "validation-error", "Unprocessable Content", detail, errors=errors)


async def _parse_draft(raw: bytes, content_type: str) -> RuleSetDraftIn:
    """Strict-parse the body off the event loop and validate it into the draft shape."""
    data = await _parse_mapping(raw, content_type)
    try:
        return await asyncio.to_thread(RuleSetDraftIn.model_validate, data)
    except ValidationError as exc:
        raise _shape_problem(exc, "The draft failed validation.") from exc


async def _parse_rule(raw: bytes, content_type: str, rule_key: str) -> RuleDef:
    """One rule from the body; the path key is authoritative (a missing body key is injected)."""
    data = await _parse_mapping(raw, content_type)
    body_key = data.get("key")
    if body_key is not None and body_key != rule_key:
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "The rule's key does not match the key in the path.",
            errors=[{"pointer": "/key", "detail": "must equal the key in the path"}],
        )
    try:
        return await asyncio.to_thread(RuleDef.model_validate, {**data, "key": rule_key})
    except ValidationError as exc:
        raise _shape_problem(exc, "The rule failed validation.") from exc


async def _advisory_check(request: Request, rules_json: list[dict[str, Any]]) -> dict[str, Any]:
    """The check report as a dict, or ``{"status": "unavailable"}`` when the sandbox cannot run."""
    settings: ValidationSettings = request.app.state.settings
    content: RuleSetContent = _content_of(rules_json)
    try:
        report = await run_check(request.app.state.sandbox, content, settings.limits())
    except ProblemError as exc:
        if exc.slug == "validation-unavailable":
            return {"status": "unavailable"}
        raise
    return report.model_dump(mode="json", exclude_none=True)


_WRITE_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {"description": "Rule set or folder not found, or not visible to the caller"},
    409: {"description": "Folder is archived, or the rule set name is taken"},
    503: {"description": "Authorization is temporarily unavailable"},
}


@router.post(
    "/folders/{folder_id}/rule-sets",
    status_code=201,
    response_model=RuleSetOut,
    dependencies=[require("ruleset:create", target=_path_folder_id)],
    responses={
        **_WRITE_RESPONSES,
        403: {"description": "The caller can read the folder but may not create rule sets in it"},
        422: {"description": "Invalid name or description"},
    },
)
async def create_rule_set_route(
    folder_id: uuid.UUID,
    body: RuleSetCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> RuleSetOut:
    """Create a rule set with an empty draft in a folder of any kind."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise not_found()
    if facts.archived:
        raise _archived()
    rule_set = await create_rule_set(uow, facts, body.name, body.description)
    out = _rule_set_out(rule_set)
    # Commit before answering: the client only sees 201 once the row and its event are durable.
    await uow.commit()
    response.headers["Location"] = f"/api/v1/validation/rule-sets/{out.id}"
    response.headers["ETag"] = etag_for_rule_set(out.draft_revision)
    return out


@router.get(
    "/rule-sets/{rule_set_id}",
    response_model=RuleSetDraftOut,
    dependencies=[require("ruleset:read", target=rule_set_folder)],
    responses={404: {"description": "Rule set not found, or not visible to the caller"}},
)
async def get_rule_set(
    rule_set_id: uuid.UUID,
    response: Response,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> RuleSetDraftOut:
    """The draft and its ETag (for ``If-Match``); no check is run."""
    rule_set = await _load_rule_set(session, rule_set_id)
    response.headers["ETag"] = etag_for_rule_set(rule_set.draft_revision)
    return _draft_out(rule_set, None)


@router.put(
    "/rule-sets/{rule_set_id}/draft",
    response_model=RuleSetDraftOut,
    dependencies=[require("ruleset:update", target=rule_set_folder)],
    openapi_extra=_DRAFT_BODY,
    responses={
        **_WRITE_RESPONSES,
        412: {"description": "If-Match does not match the draft's current ETag"},
        413: {"description": "Draft larger than the size cap"},
        415: {"description": "Content-Type is not YAML or JSON"},
        422: {"description": "Malformed document or a draft of the wrong shape"},
        428: {"description": "If-Match is required"},
    },
)
async def update_rule_set_draft(
    rule_set_id: uuid.UUID, request: Request, response: Response, uow: UnitOfWorkDep
) -> RuleSetDraftOut:
    """Replace the draft under ``If-Match``; the advisory check report rides on the answer."""
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    settings: ValidationSettings = request.app.state.settings
    raw = await read_body_capped(request, settings.max_draft_bytes)
    parsed = await _parse_draft(raw, request.headers.get("content-type", ""))

    rule_set, changed = await save_draft(
        uow, rule_set_id, parsed.description, parsed.rules, request.headers.get("if-match")
    )
    out = _draft_out(rule_set, None)
    stored_rules = list(rule_set.draft_rules)
    if changed:
        await uow.commit()
    else:
        # SEC-2: an identical save writes nothing, but the rule_set row is still locked
        # `FOR UPDATE` in an open transaction; end it before the (up to check_budget_s) check.
        await uow.session.rollback()
    # The check runs after the commit (or the rollback of a no-op save), with no lock held: it
    # never decides whether the save lands.
    out.check = await _advisory_check(request, stored_rules)
    response.headers["ETag"] = etag_for_rule_set(out.rule_set.draft_revision)
    return out


@router.put(
    "/rule-sets/{rule_set_id}/draft/rules/{rule_key}",
    response_model=RuleSetDraftOut,
    dependencies=[require("ruleset:update", target=rule_set_folder)],
    openapi_extra=_RULE_BODY,
    responses={
        **_WRITE_RESPONSES,
        412: {"description": "If-Match does not match the draft's current ETag"},
        413: {"description": "Rule larger than the size cap"},
        415: {"description": "Content-Type is not YAML or JSON"},
        422: {"description": "Malformed document, a rule of the wrong shape or a key mismatch"},
        428: {"description": "If-Match is required"},
    },
)
async def put_draft_rule(
    rule_set_id: uuid.UUID,
    rule_key: RuleKeyPath,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> RuleSetDraftOut:
    """Create or replace one rule in the draft under the draft's ``If-Match`` (path key wins)."""
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    settings: ValidationSettings = request.app.state.settings
    cap = settings.max_fixture_bytes * 2 * settings.max_fixtures_per_kind + _RULE_BODY_SLACK
    raw = await read_body_capped(request, cap)
    rule = await _parse_rule(raw, request.headers.get("content-type", ""), rule_key)

    rule_set, changed = await put_rule(uow, rule_set_id, rule, request.headers.get("if-match"))
    out = _draft_out(rule_set, None)
    stored_rules = list(rule_set.draft_rules)
    if changed:
        await uow.commit()
    else:
        await uow.session.rollback()  # SEC-2: release the rule_set lock of a no-op save first
    out.check = await _advisory_check(request, stored_rules)
    response.headers["ETag"] = etag_for_rule_set(out.rule_set.draft_revision)
    return out


@router.delete(
    "/rule-sets/{rule_set_id}/draft/rules/{rule_key}",
    response_model=RuleSetDraftOut,
    dependencies=[require("ruleset:update", target=rule_set_folder)],
    responses={
        **_WRITE_RESPONSES,
        412: {"description": "If-Match does not match the draft's current ETag"},
        422: {"description": "The key in the path is not a valid rule key"},
        428: {"description": "If-Match is required"},
    },
)
async def delete_draft_rule(
    rule_set_id: uuid.UUID,
    rule_key: RuleKeyPath,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> RuleSetDraftOut:
    """Remove one rule from the draft under ``If-Match``; 404 when the draft has no such key."""
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    rule_set = await delete_rule(uow, rule_set_id, rule_key, request.headers.get("if-match"))
    out = _draft_out(rule_set, None)
    stored_rules = list(rule_set.draft_rules)
    await uow.commit()
    out.check = await _advisory_check(request, stored_rules)
    response.headers["ETag"] = etag_for_rule_set(out.rule_set.draft_revision)
    return out


@router.post(
    "/rule-sets/{rule_set_id}/draft/check",
    response_model=RuleSetDraftOut,
    dependencies=[require("ruleset:read", target=rule_set_folder)],
    responses={
        404: {"description": "Rule set not found, or not visible to the caller"},
        503: {"description": "The rule sandbox is saturated (Retry-After: 1) or unavailable"},
    },
)
async def check_rule_set_draft(
    rule_set_id: uuid.UUID,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> RuleSetDraftOut:
    """Run the full check on the stored draft: read only, no write and no event."""
    rule_set = await _load_rule_set(session, rule_set_id)
    out = _draft_out(rule_set, None)
    settings: ValidationSettings = request.app.state.settings
    report = await run_check(
        request.app.state.sandbox, _content_of(list(rule_set.draft_rules)), settings.limits()
    )
    out.check = report.model_dump(mode="json", exclude_none=True)
    response.headers["ETag"] = etag_for_rule_set(rule_set.draft_revision)
    return out


@router.post(
    "/rule-sets/{rule_set_id}/publish",
    status_code=201,
    response_model=RuleSetVersionOut,
    dependencies=[require("ruleset:publish", target=rule_set_folder)],
    responses={
        200: {
            "description": "The draft is identical to the newest version; that version is "
            "returned and nothing is recorded",
            "model": RuleSetVersionOut,
        },
        **_WRITE_RESPONSES,
        412: {"description": "If-Match does not match the draft's current ETag"},
        422: {"description": "The draft did not pass its check (the body carries the report)"},
        428: {"description": "If-Match is required"},
        503: {"description": "The rule sandbox is saturated (Retry-After: 1) or unavailable"},
    },
)
async def publish_rule_set_route(
    rule_set_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(authenticate)],
) -> RuleSetVersionOut:
    """Check the draft and publish it as the next immutable version (201), or 200 if unchanged.

    No unit of work is open here: ``publish_rule_set`` holds no session or lock while the sandbox
    checks, and opens its own unit only to lock the row and insert the version.
    """
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    state = request.app.state
    version, created = await publish_rule_set(
        state.session_factory,
        state.sandbox,
        state.settings,
        principal_to_actor(principal),
        rule_set_id,
        request.headers.get("if-match"),
        warn_days=await effective_warn_days(
            state.settings_client, parse_bearer(request), state.settings
        ),
    )
    if created:
        response.headers["Location"] = (
            f"/api/v1/validation/rule-sets/{rule_set_id}/versions/{version.version_no}"
        )
    else:
        response.status_code = 200
    return _version_out(version)


@router.get(
    "/rule-sets/{rule_set_id}/versions/{version_no}",
    response_model=RuleSetVersionOut,
    dependencies=[require("ruleset:read", target=rule_set_folder)],
    responses={404: {"description": "Rule set or version not found, or not visible"}},
)
async def get_rule_set_version(
    rule_set_id: uuid.UUID,
    version_no: VersionNo,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> RuleSetVersionOut:
    version = (
        await session.execute(
            select(RuleSetVersion).where(
                RuleSetVersion.rule_set_id == rule_set_id,
                RuleSetVersion.version_no == version_no,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise not_found()
    return _version_out(version)


@router.get(
    "/folders/{folder_id}/rule-sets",
    response_model=RuleSetListOut,
    dependencies=[require("ruleset:read", target=_path_folder_id)],
    responses={
        403: {"description": "The caller can read the folder but may not author rule sets in it"},
        404: {"description": "Folder not found, or not visible to the caller"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def list_folder_rule_sets(
    folder_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> RuleSetListOut:
    """The rule sets held at this folder, for authors, with their draft state."""
    rows = await list_rule_sets(session, folder_id)
    return RuleSetListOut(items=[_rule_set_out(row) for row in rows])


@router.get(
    "/rule-sets/{rule_set_id}/versions",
    response_model=RuleSetVersionListOut,
    dependencies=[require("ruleset:read", target=rule_set_folder)],
    responses={404: {"description": "Rule set not found, or not visible to the caller"}},
)
async def list_rule_set_versions(
    rule_set_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> RuleSetVersionListOut:
    """Published versions, newest first; a rule set that was never published has none."""
    versions = await list_versions(session, rule_set_id)
    return RuleSetVersionListOut(items=[_version_summary(v) for v in versions])


@router.post(
    "/rule-sets/{rule_set_id}/versions/{version_no}/deprecate",
    response_model=RuleSetVersionOut,
    dependencies=[require("ruleset:deprecate", target=rule_set_folder)],
    responses={
        **_WRITE_RESPONSES,
        403: {"description": "The caller can read the rule set but may not deprecate versions"},
    },
)
async def deprecate_rule_set_version(
    rule_set_id: uuid.UUID, version_no: VersionNo, request: Request, uow: UnitOfWorkDep
) -> RuleSetVersionOut:
    """Mark a version deprecated (200); an already deprecated version is a no-op (200)."""
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    version, changed = await deprecate_version(uow, rule_set_id, version_no)
    out = _version_out(version)
    if changed:
        await uow.commit()
    return out


@router.delete(
    "/rule-sets/{rule_set_id}",
    status_code=204,
    response_class=Response,
    dependencies=[require("ruleset:update", target=rule_set_folder)],
    responses={
        **_WRITE_RESPONSES,
        409: {
            "description": "The folder is archived (folder-archived) or the rule set has "
            "published versions (rule-set-published)"
        },
    },
)
async def delete_rule_set_route(
    rule_set_id: uuid.UUID, request: Request, uow: UnitOfWorkDep
) -> Response:
    """Delete a rule set that was never published (204); a published one is kept (409)."""
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    await delete_rule_set(uow, rule_set_id)
    await uow.commit()
    return Response(status_code=204)


@router.put(
    "/rule-sets/{rule_set_id}/mandatory",
    response_model=RuleSetOut,
    dependencies=[require("ruleset:mandate", target=rule_set_folder)],
    responses={
        **_WRITE_RESPONSES,
        403: {"description": "The caller can read the rule set but may not mandate it"},
        422: {"description": "The rule set has no published version, or the body is invalid"},
    },
)
async def set_rule_set_mandatory(
    rule_set_id: uuid.UUID, body: RuleSetMandatoryIn, request: Request, uow: UnitOfWorkDep
) -> RuleSetOut:
    """Set or clear the mandatory flag; repeating the current state is a 200 no-op.

    Setting applies the set to its folder and everything below, whatever block flags say. Clearing
    is the audited break-glass; setting it again restores the version that was enforced.
    """
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    settings: ValidationSettings = request.app.state.settings
    warn_days = await effective_warn_days(
        request.app.state.settings_client, parse_bearer(request), settings
    )
    rule_set, changed = await set_mandatory(uow, rule_set_id, body.mandatory, warn_days)
    out = _rule_set_out(rule_set)
    if changed:
        await uow.commit()
    return out


@router.post(
    "/rule-sets/{rule_set_id}/versions/{version_no}/enforce-now",
    response_model=RuleSetVersionOut,
    dependencies=[require("ruleset:mandate", target=rule_set_folder)],
    responses={
        **_WRITE_RESPONSES,
        403: {"description": "The caller can read the rule set but may not mandate it"},
        409: {
            "description": "The folder is archived (folder-archived) or the version has no "
            "enforcement date because its rule set is not mandatory (rule-set-not-mandatory)"
        },
    },
)
async def enforce_rule_set_version_now(
    rule_set_id: uuid.UUID, version_no: VersionNo, request: Request, uow: UnitOfWorkDep
) -> RuleSetVersionOut:
    """Switch a warning version to enforce now; an already enforced one is a no-op."""
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    version, changed = await enforce_now(uow, rule_set_id, version_no)
    out = _version_out(version)
    if changed:
        await uow.commit()
    return out


@router.post(
    "/folders/{folder_id}/platform-rule-sets/{rule_set_id}/versions/{version_no}/enforce-now",
    response_model=RuleSetVersionOut,
    dependencies=[require("ruleset:mandate", target=_path_folder_id)],
    responses={
        **_WRITE_RESPONSES,
        403: {"description": "The caller can read the ROOT folder but may not mandate rule sets"},
        404: {
            "description": "The folder is not the ROOT folder, the rule set is not a platform "
            "rule set, or the version is unknown (one response)"
        },
        409: {
            "description": "The version has no enforcement date because its rule set is not "
            "mandatory (rule-set-not-mandatory)"
        },
    },
)
async def enforce_platform_rule_set_version_now(
    folder_id: uuid.UUID,
    rule_set_id: uuid.UUID,
    version_no: VersionNo,
    request: Request,
    uow: UnitOfWorkDep,
) -> RuleSetVersionOut:
    """Switch a seeded platform rule set's warning version to enforce now.

    Platform rule sets hold no folder, so the folder-scoped route cannot reach them and validation
    knows no fixed ROOT id: the caller names the ROOT folder in the path and ``ruleset:mandate`` is
    checked there (a Global Policy Manager or Global admin). Any other folder, or a rule set that
    is not a platform set, is the uniform 404. The write is the same audited ``enforce_now`` as
    the folder-scoped route (``ruleset.enforced_early``).
    """
    facts = current_decision(request).folder
    if facts is None or facts.kind != "root":
        raise not_found()
    scope = (
        await uow.session.execute(select(RuleSet.scope).where(RuleSet.id == rule_set_id))
    ).scalar_one_or_none()
    if scope != "platform":
        raise not_found()
    version, changed = await enforce_now(uow, rule_set_id, version_no)
    out = _version_out(version)
    if changed:
        await uow.commit()
    return out


@router.put(
    "/rule-sets/{rule_set_id}/versions/{version_no}/enforce-at",
    response_model=RuleSetVersionOut,
    dependencies=[require("ruleset:mandate", target=rule_set_folder)],
    responses={
        **_WRITE_RESPONSES,
        403: {"description": "The caller can read the rule set but may not mandate it"},
        409: {
            "description": "The folder is archived (folder-archived) or the version has no "
            "enforcement date because its rule set is not mandatory (rule-set-not-mandatory)"
        },
    },
)
async def reschedule_rule_set_version(
    rule_set_id: uuid.UUID,
    version_no: VersionNo,
    body: RuleSetEnforceAtIn,
    request: Request,
    uow: UnitOfWorkDep,
) -> RuleSetVersionOut:
    """Move a mandatory version's enforcement date to any timestamp.

    A future date warns until then, now or a past date enforces at once (a past date is stored as
    the database's now). The same effective date again is a 200 no-op. A change is audited as
    ``ruleset.enforcement_rescheduled``. ``enforce-now`` keeps working unchanged.
    """
    facts = current_decision(request).folder
    if facts is not None and facts.archived:
        raise _archived()
    version, changed = await reschedule_enforcement(uow, rule_set_id, version_no, body.enforce_at)
    out = _version_out(version)
    if changed:
        await uow.commit()
    return out


@router.put(
    "/folders/{folder_id}/platform-rule-sets/{rule_set_id}/versions/{version_no}/enforce-at",
    response_model=RuleSetVersionOut,
    dependencies=[require("ruleset:mandate", target=_path_folder_id)],
    responses={
        **_WRITE_RESPONSES,
        403: {"description": "The caller can read the ROOT folder but may not mandate rule sets"},
        404: {
            "description": "The folder is not the ROOT folder, the rule set is not a platform "
            "rule set, or the version is unknown (one response)"
        },
        409: {
            "description": "The version has no enforcement date because its rule set is not "
            "mandatory (rule-set-not-mandatory)"
        },
    },
)
async def reschedule_platform_rule_set_version(
    folder_id: uuid.UUID,
    rule_set_id: uuid.UUID,
    version_no: VersionNo,
    body: RuleSetEnforceAtIn,
    request: Request,
    uow: UnitOfWorkDep,
) -> RuleSetVersionOut:
    """Move a platform rule set version's enforcement date to any timestamp.

    Like ``enforce-now`` for platform sets: the caller names the ROOT folder in the path and
    ``ruleset:mandate`` is checked there; any other folder, a non-platform set or an unknown
    version is the uniform 404. A future date warns until then, now or a past date enforces at
    once. Audited as ``ruleset.enforcement_rescheduled``; a repeat of the effective date is a 200
    no-op.
    """
    facts = current_decision(request).folder
    if facts is None or facts.kind != "root":
        raise not_found()
    scope = (
        await uow.session.execute(select(RuleSet.scope).where(RuleSet.id == rule_set_id))
    ).scalar_one_or_none()
    if scope != "platform":
        raise not_found()
    version, changed = await reschedule_enforcement(uow, rule_set_id, version_no, body.enforce_at)
    out = _version_out(version)
    if changed:
        await uow.commit()
    return out


@router.put(
    "/folders/{folder_id}/platform-rule-sets/{rule_set_id}/paused",
    response_model=RuleSetOut,
    dependencies=[require("ruleset:mandate", target=_path_folder_id)],
    responses={
        **_WRITE_RESPONSES,
        403: {"description": "The caller can read the ROOT folder but may not mandate rule sets"},
        404: {
            "description": "The folder is not the ROOT folder, or the rule set is not a platform "
            "rule set or is unknown (one response)"
        },
        409: {"description": "The rule set is not mandatory (rule-set-not-mandatory)"},
    },
)
async def set_platform_rule_set_paused(
    folder_id: uuid.UUID,
    rule_set_id: uuid.UUID,
    body: RuleSetPausedIn,
    request: Request,
    uow: UnitOfWorkDep,
) -> RuleSetOut:
    """Pause or resume a built-in platform rule set.

    A paused set is warn only: its rules still run and report findings on every save, validation
    and publish (marked ``paused: true``), but never block publishing. Resume restores the mode the
    dates compute (an enforced version blocks again at once). Platform rule sets hold no folder, so
    the caller names the ROOT folder in the path and ``ruleset:mandate`` is checked there (Global
    admin or Global Policy Manager); any other folder, or a set that is not a platform set, is the
    uniform 404. Repeating the current state is a 200 no-op without an audit event; a change is
    audited as ``ruleset.paused`` or ``ruleset.resumed``.
    """
    facts = current_decision(request).folder
    if facts is None or facts.kind != "root":
        raise not_found()
    scope = (
        await uow.session.execute(select(RuleSet.scope).where(RuleSet.id == rule_set_id))
    ).scalar_one_or_none()
    if scope != "platform":
        raise not_found()
    rule_set, changed = await set_paused(uow, rule_set_id, body.paused)
    out = _rule_set_out(rule_set)
    if changed:
        await uow.commit()
    return out


_AVAILABLE_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {
        "description": "Folder not found or not visible to the caller, or the rule set is not "
        "published, not held by this folder or an ancestor, or unknown (one response)"
    },
    503: {"description": "Authorization is temporarily unavailable"},
}


@router.get(
    "/folders/{folder_id}/available-rule-sets",
    response_model=AvailableRuleSetListOut,
    dependencies=[require("ruleset:read-published", target=_path_folder_id)],
    responses=_AVAILABLE_RESPONSES,
)
async def list_available_rule_sets(
    folder_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> AvailableRuleSetListOut:
    """Published rule sets that apply at this folder: its own, its ancestors' and the platform's.

    The permission is checked at this folder, so anyone who can read it reads the rules that apply
    to it; drafts are never part of the answer.
    """
    facts = _caller_folder(request)
    found = await available_rule_sets(session, facts)
    resolved = _resolved_by_set(await resolved_mandatory(session, facts))
    return AvailableRuleSetListOut(items=[_available_out(r, v, resolved) for r, v in found])


@router.get(
    "/folders/{folder_id}/available-rule-sets/{rule_set_id}",
    response_model=AvailableRuleSetOut,
    dependencies=[require("ruleset:read-published", target=_path_folder_id)],
    responses=_AVAILABLE_RESPONSES,
)
async def get_available_rule_set(
    folder_id: uuid.UUID,
    rule_set_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> AvailableRuleSetOut:
    facts = _caller_folder(request)
    rule_set, versions = await available_rule_set(session, facts, rule_set_id)
    resolved = _resolved_by_set(await resolved_mandatory(session, facts))
    return _available_out(rule_set, versions, resolved)


@router.get(
    "/folders/{folder_id}/available-rule-sets/{rule_set_id}/versions/{version_no}",
    response_model=RuleSetVersionOut,
    dependencies=[require("ruleset:read-published", target=_path_folder_id)],
    responses=_AVAILABLE_RESPONSES,
)
async def get_available_rule_set_version(
    folder_id: uuid.UUID,
    rule_set_id: uuid.UUID,
    version_no: VersionNo,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> RuleSetVersionOut:
    """A published version with its rules and fixtures; a deprecated one stays readable."""
    _, versions = await available_rule_set(session, _caller_folder(request), rule_set_id)
    for version in versions:
        if version.version_no == version_no:
            return _version_out(version)
    raise not_found()
