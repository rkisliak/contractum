"""Validation run routes: the internal run route and the public on-demand upload route.

Internal: ``/internal/v1/validation/folders/{folder_id}/runs`` (D-125).

Never in OpenAPI and never routed by the gateway: contracts calls it over the compose network with
the **caller's own** forwarded bearer token, so validation checks ``contract:validate`` itself and
takes the folder's ancestry from the PDP's folder facts (never from the body). That is a fresh
decision (the method is POST) in both modes. The route has no Pydantic body parameter: the policy
authenticates and authorises first and the handler reads the capped body afterwards, so an
unauthorised caller never makes the service buffer or parse anything (research Pitfall 15).

Internal read: ``GET /internal/v1/validation/runs/{run_id}?contract_id=`` (D-155) serves a stored
publish run to the contracts report fetch. Validation authorises it itself:
``contract:read-published`` at the run's stored folder with the caller's forwarded token, the
stored contract id equal to the one sent and only publishable runs; everything else is the uniform
404 (T-6-26).

Public: ``POST /api/v1/validation/folders/{folder_id}/validate`` (VAL-08, D-126, D-127) validates an
uploaded document at the folder through the same pipeline in save mode, so nothing is stored.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from dc_auth.authorizer import FolderFacts
from dc_auth.deps import authenticate, current_decision, require
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.body import read_body_capped
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from dc_odcs import ALLOWED_MEDIA_TYPES, MAX_VERSION_CHARS
from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from dc_validation.facts import MAX_LINK_FACTS, MAX_LINK_KEY_CHARS
from dc_validation.report import ValidationPinOut, ValidationReportOut
from dc_validation.rulesets import not_found
from dc_validation.runs import (
    RunBaseline,
    RunRequest,
    ValidationAttachmentIn,
    ValidationRunIn,
    ValidationRunOut,
    ValidationRunReadOut,
    decode_request,
    execute_run,
    read_publishable_run,
    run_folder,
)
from dc_validation.settings import ValidationSettings

internal_router = PolicyRouter(prefix="/internal/v1/validation")
router = PolicyRouter(prefix="/api/v1/validation")

_BINARY = {"schema": {"type": "string", "format": "binary"}}
_DOCUMENT_MEDIA_TYPES = {media_type: _BINARY for media_type in sorted(ALLOWED_MEDIA_TYPES)}

# Compare an uploaded document with the previous one (D-148, Open Question 8): a JSON envelope with
# both documents as text, because multipart needs python-multipart, which is not in the lock.
PAIR_MEDIA_TYPE = "application/vnd.dc.validate-pair+json"

# base64 inflates by 4/3; the rest of the JSON envelope (ids, attachments, media type) gets 64 KiB.
_ENVELOPE_SLACK = 64 * 1024
# ``ref_facts`` on top (IN-02): at most MAX_LINK_FACTS entries, each a key of at most
# MAX_LINK_KEY_CHARS characters (contracts sends ASCII-escaped JSON, at most 12 bytes per character:
# a non-BMP character is two ``\uXXXX`` escapes), a pinned version of at most MAX_VERSION_CHARS
# plain characters and the fixed members (state, flags, punctuation).
_REF_FACTS_SLACK = MAX_LINK_FACTS * (12 * MAX_LINK_KEY_CHARS + MAX_VERSION_CHARS + 128)


def run_body_cap(settings: ValidationSettings) -> int:
    """The internal run body cap: document and baseline base64, the envelope and ``ref_facts``."""
    return 2 * math.ceil(settings.max_document_bytes * 4 / 3) + _ENVELOPE_SLACK + _REF_FACTS_SLACK


async def _path_folder_id(folder_id: uuid.UUID) -> uuid.UUID:
    return folder_id


def _pointer(loc: tuple[Any, ...]) -> str:
    parts = (str(p).replace("~", "~0").replace("/", "~1") for p in loc)
    return "/" + "/".join(parts) if loc else ""


def _shape_problem(exc: ValidationError) -> ProblemError:
    """422 with JSON-pointer errors and fixed texts: nothing submitted is echoed back."""
    errors = [
        {
            "pointer": _pointer(tuple(err.get("loc", ()))),
            "detail": f"Invalid value ({err.get('type', 'invalid')}).",
        }
        for err in exc.errors()
    ]
    return ProblemError(
        422, "validation-error", "Validation error", "The request did not validate.", errors=errors
    )


@internal_router.post(
    "/folders/{folder_id}/runs",
    response_model=ValidationRunOut,
    dependencies=[require("contract:validate", target=_path_folder_id)],
)
async def run_validation(
    folder_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> ValidationRunOut:
    """Validate one document at the folder; publish mode also stores the run."""
    settings: ValidationSettings = request.app.state.settings
    facts: FolderFacts | None = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise not_found()
    # two documents (the document and the baseline), each base64 encoded, the envelope and the
    # link facts: a maximal request contracts can build is never a 413 (IN-02)
    raw = await read_body_capped(request, run_body_cap(settings))
    try:
        body = await asyncio.to_thread(ValidationRunIn.model_validate_json, raw)
    except ValidationError as exc:
        raise _shape_problem(exc) from exc
    if len(body.attachments) > settings.max_attachments:
        raise ProblemError(
            422,
            "validation-error",
            "Validation error",
            "The request did not validate.",
            errors=[{"pointer": "/attachments", "detail": "Too many attachments."}],
        )
    authorizer = request.app.state.authorizer_factory(request)
    return await execute_run(
        request.app.state, principal, facts, decode_request(body), authorizer=authorizer
    )


@internal_router.get(
    "/runs/{run_id}",
    response_model=ValidationRunReadOut,
    dependencies=[require("contract:read-published", target=run_folder)],
)
async def read_run(
    run_id: uuid.UUID,
    contract_id: Annotated[uuid.UUID, Query()],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> ValidationRunReadOut:
    """One stored publish run for the contracts report fetch (D-155); the uniform 404 otherwise."""
    return await read_publishable_run(session, run_id, contract_id)


_MAX_VERSION_NO = 2_147_483_647
_MAX_RULE_SET_PARAM = 80  # a uuid, "@" and a 10 digit version number fit in 48


def _invalid_rule_set(index: int) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Validation error",
        "The request did not validate.",
        errors=[
            {
                "pointer": f"/query/rule_set/{index}",
                "detail": "Use <rule set id> or <rule set id>@<version number>.",
            }
        ],
    )


def parse_adhoc(values: Sequence[str]) -> list[ValidationAttachmentIn]:
    """``<uuid>`` is the latest version, ``<uuid>@<n>`` exactly version n; 422 otherwise.

    The 422 names the position of the bad value and never echoes it.
    """
    parsed: list[ValidationAttachmentIn] = []
    for index, value in enumerate(values):
        if len(value) > _MAX_RULE_SET_PARAM or not value.isascii():
            raise _invalid_rule_set(index)
        head, at, tail = value.partition("@")
        version: int | Literal["latest"] = "latest"
        if at:
            if not tail.isdecimal() or not 1 <= int(tail) <= _MAX_VERSION_NO:
                raise _invalid_rule_set(index)
            version = int(tail)
        try:
            rule_set_id = uuid.UUID(head)
        except ValueError:
            raise _invalid_rule_set(index) from None
        if head.lower() != str(rule_set_id):  # the hyphenated form only, not braces or urn:
            raise _invalid_rule_set(index)
        parsed.append(ValidationAttachmentIn(rule_set_id=rule_set_id, version=version))
    return parsed


class ValidatePairIn(BaseModel):
    """The pair upload: the document under test and the previous version it is compared with."""

    model_config = ConfigDict(extra="forbid", strict=True)

    document: str
    document_media_type: str = Field(max_length=200)
    previous_document: str
    previous_media_type: str = Field(max_length=200)


def _too_large() -> ProblemError:
    return ProblemError(
        413,
        "payload-too-large",
        "Payload Too Large",
        "The document exceeds the size limit.",
    )


def _document_bytes(text: str, max_bytes: int) -> bytes:
    """The UTF-8 bytes of an enveloped document; 413 above the cap, 422 if it is not text."""
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProblemError(
            422,
            "validation-error",
            "Validation error",
            "The request did not validate.",
            errors=[{"pointer": "/document", "detail": "The document is not valid text."}],
        ) from exc
    if len(raw) > max_bytes:
        raise _too_large()
    return raw


async def _read_pair(request: Request, settings: ValidationSettings) -> RunRequest:
    """Read and strictly parse the pair envelope off the event loop (T-6-14)."""
    # JSON escaping can double a document's size; each decoded document is capped on its own below.
    cap = 4 * settings.max_document_bytes + _ENVELOPE_SLACK
    raw = await read_body_capped(request, cap)
    try:
        body = await asyncio.to_thread(ValidatePairIn.model_validate_json, raw)
    except ValidationError as exc:
        raise _shape_problem(exc) from exc
    return RunRequest(
        mode="save",
        contract_id=None,
        draft_id=None,
        media_type=body.document_media_type,
        raw=_document_bytes(body.document, settings.max_document_bytes),
        baseline=RunBaseline(
            version_id=None,
            semver=None,
            media_type=body.previous_media_type,
            raw=_document_bytes(body.previous_document, settings.max_document_bytes),
        ),
        derive_floor=True,
    )


class ValidationOnDemandOut(BaseModel):
    """The answer to an uploaded document: the run's report and pins, with no stored run."""

    model_config = ConfigDict(extra="forbid")

    content_sha256: str
    engine_version: str
    dsl_version: str
    schema_sha: str
    pins: list[ValidationPinOut]
    report: ValidationReportOut


@router.post(
    "/folders/{folder_id}/validate",
    response_model=ValidationOnDemandOut,
    dependencies=[require("contract:validate", target=_path_folder_id)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                **_DOCUMENT_MEDIA_TYPES,
                PAIR_MEDIA_TYPE: {"schema": ValidatePairIn.model_json_schema()},
            },
        }
    },
    responses={
        404: {"description": "Folder not found, or not visible to the caller"},
        413: {"description": "A document larger than the size cap"},
        415: {"description": "Content-Type is not YAML, JSON or the validate-pair type"},
        422: {
            "description": (
                "Malformed or unsupported document, or a rule_set parameter that is not valid"
            )
        },
        503: {"description": "Authorization or the rule sandbox is temporarily unavailable"},
    },
)
async def validate_on_demand(
    folder_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    rule_set: Annotated[
        list[str] | None,
        Query(
            description=(
                "A published rule set to try as well, `<rule set id>` (its latest version) or "
                "`<rule set id>@<version number>`. Repeatable, at most DC_MAX_ADHOC_RULE_SETS."
            )
        ),
    ] = None,
) -> ValidationOnDemandOut:
    """Validate an uploaded contract at the folder without saving it."""
    settings: ValidationSettings = request.app.state.settings
    values = rule_set or []
    if len(values) > settings.max_adhoc_rule_sets:
        raise ProblemError(
            422,
            "validation-error",
            "Validation error",
            "The request did not validate.",
            errors=[{"pointer": "/query/rule_set", "detail": "Too many rule sets."}],
        )
    adhoc = parse_adhoc(values)
    facts: FolderFacts | None = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise not_found()
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() == PAIR_MEDIA_TYPE:
        run = await _read_pair(request, settings)
    else:
        raw = await read_body_capped(request, settings.max_document_bytes)
        run = RunRequest(
            mode="save", contract_id=None, draft_id=None, media_type=content_type, raw=raw
        )
    authorizer = request.app.state.authorizer_factory(request)
    out = await execute_run(
        request.app.state, principal, facts, run, adhoc=adhoc, authorizer=authorizer
    )
    return ValidationOnDemandOut(
        content_sha256=out.content_sha256,
        engine_version=out.engine_version,
        dsl_version=out.dsl_version,
        schema_sha=out.schema_sha,
        pins=out.pins,
        report=out.report,
    )
