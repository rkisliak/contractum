"""Consumer fetch of published contract versions (API-01, API-02, API-03, D-48).

``GET /contracts/{contract_id}/versions/{ref}`` serves one published version by the ``latest``
alias or by its strict SemVer string. The default representation is the stored bytes, byte for
byte, with the stored media type; only an explicit ``Accept`` for the other format returns a
converted rendering, with its own ETag. ``GET .../versions`` lists every visible version with its
derived status and ``GET .../versions/{ref}/metadata`` describes one.
``GET .../versions/{ref}/validation-report`` serves the stored report the version was published
with (D-155): the same policy and visibility as the fetch, then validation's own authorised read.
``GET .../contracts/{contract_id}/diff`` is the classified changelog between two refs (D-156),
computed on the fly by ``diffs``.

Visibility is decided once, here, for all three routes: the row states come from the caller's
permissions (``visible_states``), a draft is never a version, and a caller without
``contract:read`` learns nothing about a contract that has no Latest (the same 404 as an unknown
contract), so a draft-only contract has no existence oracle. Anything that is not a visible row of
a readable contract (a draft, an unknown ref) is the same 404 ``not-found`` problem.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime
from functools import cmp_to_key, partial
from typing import Annotated, Any, Literal

import structlog
from dc_auth.deps import (
    UnitOfWorkDep,
    current_decision,
    parse_bearer,
    recorded_refusal,
    require,
)
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts import diffs
from dc_contracts.access_events import record_content_read
from dc_contracts.api.cards import (
    _MAX_TEXT,
    CardContactOut,
    CardSubscriptionOut,
    _checked_text,
    _subscription_out,
)
from dc_contracts.api.contracts import contract_folder, require_writable_folder
from dc_contracts.convert import negotiate, render
from dc_contracts.http_cache import deprecation_headers, if_none_match_hit, version_etag
from dc_contracts.lifecycle import (
    SemverParts,
    VersionStatus,
    compare_semver,
    derive_status,
    parse_semver,
    parts_of,
)
from dc_contracts.models import Contract, ContractVersion
from dc_contracts.pins import SubscriberRow, contract_subscribers, pin_with_semver
from dc_contracts.runtime_settings import effective_governance
from dc_contracts.search import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from dc_contracts.subscriptions import remove_subscription
from dc_contracts.validation_client import ValidationClient, unavailable
from dc_contracts.versions import (
    LifecycleBlocked,
    deprecate_version,
    latest_parts,
    list_versions,
    promote_version,
    read_contract,
    republish_version,
    resolve_ref,
    unpublish_version,
    visible_states,
)

router = PolicyRouter(prefix="/api/v1/contracts")
_log = structlog.get_logger("dc_contracts.api.versions")

_VERSION_MEDIA_TYPES: dict[str, dict[str, object]] = {
    "application/yaml": {},
    "application/x-yaml": {},
    "text/yaml": {},
    "application/json": {},
}


class ContractVersionMetadataOut(BaseModel):
    """One visible version of a contract, without its document."""

    version_id: uuid.UUID
    contract_id: uuid.UUID
    semver: str
    status: Literal["latest", "preview", "older"]
    lifecycle: Literal["published", "unpublished"]
    deprecated: bool
    deprecated_at: datetime | None
    sunset_date: date | None
    content_sha256: str
    etag: str
    size_bytes: int
    media_type: str
    odcs_api_version: str
    published_at: datetime | None
    published_by: str | None
    fetch_url: str
    # What the version was validated against (D-132, RULE-07); null for a version published
    # before the publish gate recorded it.
    validation_run_id: uuid.UUID | None
    pinned_rule_sets: list[dict[str, Any]] | None
    engine_version: str | None
    schema_sha: str | None


def _clean_reason(value: str) -> str:
    """The shared reason rule: trimmed, no control character (a newline is allowed), 1..500."""
    checked = _checked_text(value, allow="\n").strip()
    if len(checked) > _MAX_TEXT:
        raise ValueError(f"must be 1 to {_MAX_TEXT} characters")
    return checked


class ContractVersionDeprecateIn(BaseModel):
    """Deprecate a published version; the sunset date is today or later, UTC.

    ``reason`` is optional, and required while the governance setting "require a reason" is on
    1 to 500 characters, trimmed, recorded in the audit event and the
    notice sent to the pinning cards.
    """

    model_config = ConfigDict(extra="forbid")

    sunset_date: date
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return None if value is None else _clean_reason(value)


class ContractVersionUnpublishIn(BaseModel):
    """Optional body of an unpublish: why (required while the governance setting is on)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return None if value is None else _clean_reason(value)


class ContractVersionListOut(BaseModel):
    """Every visible version, highest SemVer precedence first (unpaginated)."""

    items: list[ContractVersionMetadataOut]


class ContractSubscriberCardOut(BaseModel):
    """What a contract's owners see of a consuming card: how to reach it, nothing more.

    ``subscription_id`` is the pin on this contract, the id the owner-side remove route takes.
    """

    subscription_id: uuid.UUID
    card_id: uuid.UUID
    name: str
    description: str
    contacts: list[CardContactOut]
    pinned_at: datetime
    pinned_by: str


class ContractSubscriberVersionOut(BaseModel):
    """One version of the contract and the cards that actively pin it on this page."""

    version_id: uuid.UUID
    semver: str
    deprecated: bool
    sunset_date: date | None
    cards: list[ContractSubscriberCardOut]


class ContractSubscribersOut(BaseModel):
    """A keyset page of the contract's active pins, grouped by version."""

    contract_id: uuid.UUID
    versions: list[ContractSubscriberVersionOut]
    next_cursor: str | None


class ContractSubscriptionRemoveIn(BaseModel):
    """Why an owner ends a card's pin: required, stripped, 1..500 characters."""

    model_config = ConfigDict(extra="forbid")

    reason: str

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _clean_reason(value)


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


class ContractValidationReportOut(BaseModel):
    """The stored validation report a version was published with.

    ``report`` is validation's report, passed through as JSON with its ``compatibility`` and
    ``ref_facts`` sections; ``pins`` are the rule-set versions it was validated against.
    """

    contract_id: uuid.UUID
    version_id: uuid.UUID
    semver: str
    run_id: uuid.UUID
    content_sha256: str
    engine_version: str
    dsl_version: str
    schema_sha: str
    validated_at: datetime
    pins: list[dict[str, Any]]
    report: dict[str, Any]


def report_etag(run_id: uuid.UUID) -> str:
    """The strong ETag of a stored report: the run is immutable, so its id identifies it."""
    return f'"run-{run_id}"'


# The answer depends on the caller's permission: shared caches stay away and every reuse
# revalidates (cheaply, by run id) so a revoked grant is honoured at once (T-6-28).
_REPORT_HEADERS = {"Cache-Control": "private, no-cache"}


def _report_not_recorded() -> ProblemError:
    """404 for a version published before the publish gate recorded its run (Pitfall 5)."""
    return ProblemError(
        404,
        "validation-report-not-recorded",
        "Not Found",
        "This version was published before validation reports were recorded.",
    )


async def _require_visible_contract(request: Request, contract: Contract) -> None:
    """404 for a caller without ``contract:read`` when the contract has no Latest.

    Such a contract is, to a published-only reader, indistinguishable from one that does not
    exist: otherwise the empty list (or a different status) would reveal that a draft-only
    contract is there (research Pitfall 9). A ``contract:read`` holder may see the empty list.
    The refusal asks the PDP for ``contract:read`` first, so it is recorded as a denial (WR-01).
    """
    if "contract:read" not in current_decision(request).permissions and (
        contract.latest_version_id is None
    ):
        raise await recorded_refusal(request, "contract:read")


def _metadata_out(
    contract_id: uuid.UUID, version: ContractVersion, latest: SemverParts | None
) -> ContractVersionMetadataOut:
    """The one place a version row becomes its public metadata (list and single read agree)."""
    parts = parts_of(version)
    status: VersionStatus = derive_status(parts, latest, is_latest=version.is_latest)
    assert version.state in ("published", "unpublished")  # noqa: S101 - drafts are never listed
    return ContractVersionMetadataOut(
        version_id=version.id,
        contract_id=contract_id,
        semver=parts.text,
        status=status,
        lifecycle="published" if version.state == "published" else "unpublished",
        deprecated=version.deprecated_at is not None,
        deprecated_at=version.deprecated_at,
        sunset_date=version.sunset_date,
        content_sha256=version.content_sha256,
        etag=version_etag(version.content_sha256),
        size_bytes=version.size_bytes,
        media_type=version.media_type,
        odcs_api_version=version.odcs_api_version,
        published_at=version.published_at,
        published_by=version.published_by,
        fetch_url=f"/api/v1/contracts/contracts/{contract_id}/versions/{parts.text}",
        validation_run_id=version.validation_run_id,
        pinned_rule_sets=version.pinned_rule_sets,
        engine_version=version.engine_version,
        schema_sha=version.schema_sha,
    )


def _by_precedence_descending(a: ContractVersion, b: ContractVersion) -> int:
    return compare_semver(parts_of(b), parts_of(a))


async def _visible_version(
    request: Request, session: AsyncSession, contract_id: uuid.UUID, ref: str
) -> tuple[Contract, ContractVersion]:
    """The contract and the visible version ``ref`` names, or the one 404 for everything else."""
    contract = await read_contract(session, contract_id)
    await _require_visible_contract(request, contract)
    version = await resolve_ref(session, contract_id, ref)
    if version is None:
        raise _not_found()
    if version.state not in visible_states(current_decision(request).permissions):
        # An unpublished version is visible with contract:publish only: record the refusal.
        raise await recorded_refusal(request, "contract:publish")
    return contract, version


def _add_deprecation_headers(response: Response, version: ContractVersion) -> None:
    if version.deprecated_at is not None and version.sunset_date is not None:
        response.headers.update(deprecation_headers(version.deprecated_at, version.sunset_date))


def _fetch_headers(version: ContractVersion, media_type: str | None, target: str) -> dict[str, str]:
    """The one place the fetch response headers are built, for the 200 and the 304 alike.

    ``media_type`` is None for a 304: a not-modified answer carries the validators and cache
    directives the 200 would have (RFC 9110 section 15.4.5) but no content headers. A deprecated
    version also carries ``Deprecation`` and ``Sunset`` (RFC 9745, RFC 8594) on both. The
    representation depends on Accept, so caches key on it; ``no-cache`` makes every reuse
    revalidate against the ETag, and ``private`` keeps shared caches away from authenticated
    content. Content-Type is a header, not a Response argument, so Starlette cannot append a
    charset; nosniff keeps a browser from reinterpreting stored bytes.
    """
    headers = {
        "Vary": "Accept",
        "ETag": version_etag(version.content_sha256, target),
        "Cache-Control": "private, no-cache",
    }
    if version.deprecated_at is not None and version.sunset_date is not None:
        # Deprecation changes metadata, not bytes, so a 304 must refresh it (research Pitfall 8).
        headers.update(deprecation_headers(version.deprecated_at, version.sunset_date))
    if media_type is not None:
        headers["Content-Type"] = media_type
        headers["Content-Disposition"] = "inline"
        headers["X-Content-Type-Options"] = "nosniff"
    return headers


def _contact_out(contact: dict[str, object]) -> CardContactOut:
    return CardContactOut.model_validate(
        {"name": contact.get("name"), "kind": contact["kind"], "value": contact["value"]}
    )


def _group_by_version(rows: list[SubscriberRow]) -> list[ContractSubscriberVersionOut]:
    """Group one page's pins by version: SemVer precedence descending, cards by pin time then id."""
    grouped: dict[uuid.UUID, list[SubscriberRow]] = {}
    for row in rows:
        grouped.setdefault(row.version_id, []).append(row)

    def by_precedence_descending(a: list[SubscriberRow], b: list[SubscriberRow]) -> int:
        parts_a, parts_b = parse_semver(a[0].semver), parse_semver(b[0].semver)
        assert parts_a is not None and parts_b is not None  # noqa: S101 - stored rows are strict
        return compare_semver(parts_b, parts_a)

    ordered = sorted(grouped.values(), key=cmp_to_key(by_precedence_descending))
    return [
        ContractSubscriberVersionOut(
            version_id=group[0].version_id,
            semver=group[0].semver,
            deprecated=group[0].deprecated,
            sunset_date=group[0].sunset_date,
            cards=[
                ContractSubscriberCardOut(
                    subscription_id=row.subscription_id,
                    card_id=row.card_id,
                    name=row.name,
                    description=row.description,
                    contacts=[_contact_out(c) for c in row.contacts],
                    pinned_at=row.pinned_at,
                    pinned_by=row.pinned_by,
                )
                for row in sorted(group, key=lambda r: (r.pinned_at, str(r.card_id)))
            ],
        )
        for group in ordered
    ]


@router.get(
    "/contracts/{contract_id}/subscribers",
    response_model=ContractSubscribersOut,
    dependencies=[require("contract:read", target=contract_folder)],
    responses={
        401: {"description": "Missing or invalid credentials"},
        404: {
            "description": (
                "Contract not found or not visible; also for a Consumer or Policy Manager, who "
                "do not hold `contract:read`"
            )
        },
        422: {"description": "Invalid `limit` or `cursor`"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def list_contract_subscribers(
    contract_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(read_session)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> ContractSubscribersOut:
    """Who consumes each version: the cards with active pins, keyset-paged, grouped per page.

    The page follows subscription-id order, so walking every page visits each active pin exactly
    once; a version may continue on the next page (clients merge entries by ``version_id``). A card
    shows only its name, description, contacts and when and by whom it pinned: never its members,
    channels, webhook URL or its pins on other contracts, whatever its organization.
    """
    after = decode_cursor(cursor) if cursor is not None else None
    rows = await contract_subscribers(session, contract_id, cursor=after, limit=limit)
    kept = rows[:limit]
    next_cursor = encode_cursor(kept[-1].subscription_id) if len(rows) > limit else None
    return ContractSubscribersOut(
        contract_id=contract_id, versions=_group_by_version(kept), next_cursor=next_cursor
    )


@router.get(
    "/contracts/{contract_id}/versions",
    response_model=ContractVersionListOut,
    dependencies=[require("contract:read-published", target=contract_folder)],
    responses={
        404: {
            "description": (
                "Contract not found or not visible; also for a caller who may only read "
                "published versions when the contract has none"
            )
        },
    },
)
async def list_contract_versions(
    contract_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> ContractVersionListOut:
    """Every visible version of the contract, highest SemVer precedence first."""
    contract, rows = await list_versions(
        session, contract_id, states=visible_states(current_decision(request).permissions)
    )
    await _require_visible_contract(request, contract)
    latest = await latest_parts(session, contract)
    # Highest SemVer precedence first; equal precedence cannot occur (uq_contract_version_semver).
    ordered = sorted(rows, key=cmp_to_key(_by_precedence_descending))
    return ContractVersionListOut(items=[_metadata_out(contract_id, v, latest) for v in ordered])


@router.get(
    "/contracts/{contract_id}/versions/{ref}/metadata",
    response_model=ContractVersionMetadataOut,
    dependencies=[require("contract:read-published", target=contract_folder)],
    responses={
        404: {
            "description": (
                "Contract not found or not visible, no such visible version, or `ref` is "
                "neither `latest` nor a strict SemVer string"
            )
        },
    },
)
async def get_contract_version_metadata(
    contract_id: uuid.UUID,
    ref: str,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> ContractVersionMetadataOut:
    """The metadata of one visible version, by ``latest`` or SemVer."""
    contract, version = await _visible_version(request, session, contract_id, ref)
    _add_deprecation_headers(response, version)
    return _metadata_out(contract_id, version, await latest_parts(session, contract))


@router.get(
    "/contracts/{contract_id}/versions/{ref}",
    response_class=Response,
    dependencies=[require("contract:read-published", target=contract_folder)],
    responses={
        200: {
            "description": (
                "The exact bytes that were published, with the stored media type, a strong ETag "
                "and `Cache-Control: private, no-cache`. An Accept preference for the other "
                "format returns a converted rendering, which is not byte-faithful."
            ),
            "content": _VERSION_MEDIA_TYPES,
        },
        404: {
            "description": (
                "Contract not found or not visible, no such published version, or `ref` is "
                "neither `latest` nor a strict SemVer string"
            )
        },
        304: {
            "description": (
                "`If-None-Match` matched the representation's current ETag (weak comparison, a "
                "list or `*`); no body, same `ETag`, `Vary` and `Cache-Control` as the 200"
            )
        },
        406: {"description": "Accept names no representation this service can produce"},
    },
)
async def get_contract_version(
    contract_id: uuid.UUID,
    ref: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> Response:
    """One published version of the contract, by ``latest`` or SemVer."""
    _contract, version = await _visible_version(request, session, contract_id, ref)
    # Authorization already ran, so Accept is only looked at for a caller who may read it.
    target = negotiate(request.headers.get("accept"), version.media_type)
    # Revalidation comes after authorization, visibility and the 406 decision, so a conditional
    # request can never reveal what any of them hides; no rendering is done for a 304.
    if if_none_match_hit(
        request.headers.get("if-none-match"), version_etag(version.content_sha256, target)
    ):
        return Response(status_code=304, headers=_fetch_headers(version, None, target))
    if target == "original":
        content, media_type = version.raw_document, version.media_type
    else:
        # Parsing and rendering are CPU-bound: keep them off the event loop.
        content, media_type = await asyncio.to_thread(
            render, version.raw_document, version.media_type, target
        )
    # One best-effort access event per content 200 (never for the 304 above); ``ref`` is the
    # resolved value, never the raw client input. Sync and non-raising: it cannot affect the read.
    record_content_read(
        request,
        contract_id=contract_id,
        ref="latest" if ref == "latest" else parts_of(version).text,
        media_type=media_type,
        version_id=version.id,
        semver=version.semver,
    )
    return Response(content=content, headers=_fetch_headers(version, media_type, target))


@router.get(
    "/contracts/{contract_id}/versions/{ref}/validation-report",
    response_model=ContractValidationReportOut,
    dependencies=[require("contract:read-published", target=contract_folder)],
    responses={
        401: {"description": "Missing or invalid credentials"},
        304: {
            "description": (
                "`If-None-Match` matched the report's ETag (checked after authorization and "
                "visibility); no body, same `ETag` and `Cache-Control`"
            )
        },
        404: {
            "description": (
                "Contract not found or not visible, no such visible version (also for a Policy "
                "Manager without a contract role), or `ref` is neither `latest` nor a strict "
                "SemVer string; `validation-report-not-recorded` for a version published before "
                "the publish gate recorded its run"
            )
        },
        503: {"description": "Authorization or the validation service is unavailable"},
    },
)
async def get_contract_version_validation_report(
    contract_id: uuid.UUID,
    ref: str,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> ContractValidationReportOut | Response:
    """The stored report a visible version was published with, by ``latest`` or SemVer."""
    _contract, version = await _visible_version(request, session, contract_id, ref)
    run_id, version_id, semver = version.validation_run_id, version.id, parts_of(version).text
    content_sha256 = version.content_sha256
    # The rows are read: end the transaction before the network call holds the request.
    await session.rollback()
    if run_id is None:
        raise _report_not_recorded()
    etag = report_etag(run_id)
    # Revalidation comes after authorization and visibility, so a conditional request from a caller
    # who may not read the version still gets the 404; the run is immutable, so no remote call.
    if if_none_match_hit(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={**_REPORT_HEADERS, "ETag": etag})
    client: ValidationClient | None = getattr(request.app.state, "validation_client", None)
    if client is None:
        raise unavailable("Contract validation is not configured.")
    run = await client.get_run(run_id, contract_id, parse_bearer(request))
    if run is None:
        raise _not_found()
    if run.get("content_sha256") != content_sha256:
        # The run must be the one these bytes were published with; never serve another report.
        raise unavailable("Contract validation answered for different bytes.")
    response.headers.update({**_REPORT_HEADERS, "ETag": etag})
    return ContractValidationReportOut(
        contract_id=contract_id,
        version_id=version_id,
        semver=semver,
        run_id=run_id,
        content_sha256=content_sha256,
        engine_version=run["engine_version"],
        dsl_version=run["dsl_version"],
        schema_sha=run["schema_sha"],
        validated_at=run["created_at"],
        pins=run["pins"],
        report=run["report"],
    )


class ContractDiffSideOut(BaseModel):
    """One side of a diff: the ref as named and the version or draft it resolved to."""

    ref: str
    kind: Literal["version", "draft"]
    version_id: uuid.UUID
    semver: str | None
    content_sha256: str


class ContractDiffOut(BaseModel):
    """The classified changes between two refs, computed on the fly and never stored.

    ``changes`` items have the stored-report change shape: ``code``, ``class``,
    ``element_kind``, ``path``, ``pointer_before``, ``pointer_after``, ``attribute``, ``before``,
    ``after`` (values only for allow-listed attributes).
    """

    model_config = ConfigDict(populate_by_name=True)

    from_: ContractDiffSideOut = Field(alias="from")
    to: ContractDiffSideOut
    classifier_version: str
    required_bump: Literal["major", "minor", "patch", "none"]
    counts: dict[str, int]
    changes: list[dict[str, Any]]
    changes_total: int
    truncated: bool
    next_cursor: str | None


def _side_out(side: diffs.DiffSide) -> ContractDiffSideOut:
    return ContractDiffSideOut(
        ref=side.ref,
        kind=side.kind,
        version_id=side.version_id,
        semver=side.semver,
        content_sha256=side.content_sha256,
    )


_REF_QUERY = {"min_length": 1, "max_length": 200}


@router.get(
    "/contracts/{contract_id}/diff",
    response_model=ContractDiffOut,
    dependencies=[require("contract:read-published", target=contract_folder)],
    responses={
        304: {
            "description": (
                "`If-None-Match` matched the diff's ETag (checked after authorization and "
                "visibility); no body"
            )
        },
        401: {"description": "Missing or invalid credentials"},
        404: {
            "description": (
                "Contract not found or not visible, or a ref names no version the caller can see "
                "(an unpublished version or a draft for a Consumer, an unknown draft id, a ref "
                "that is neither `latest`, strict SemVer nor a draft id)"
            )
        },
        422: {"description": "Invalid `limit` (1..1000) or `cursor`, or a missing ref"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def get_contract_diff(
    contract_id: uuid.UUID,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(read_session)],
    from_ref: Annotated[
        str, Query(alias="from", description="SemVer, `latest` or a draft id", **_REF_QUERY)
    ],
    to_ref: Annotated[
        str, Query(alias="to", description="SemVer, `latest` or a draft id", **_REF_QUERY)
    ],
    limit: Annotated[int, Query(ge=1, le=diffs.MAX_DIFF_LIMIT)] = diffs.DEFAULT_DIFF_LIMIT,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
) -> ContractDiffOut | Response:
    """The classified changelog from ``from`` to ``to`` (either order), paged."""
    permissions = current_decision(request).permissions
    contract = await read_contract(session, contract_id)
    refuse = partial(recorded_refusal, request)
    before = await diffs.resolve_ref(session, contract, from_ref, permissions, refuse)
    after = await diffs.resolve_ref(session, contract, to_ref, permissions, refuse)
    # The documents are in hand: end the transaction before the CPU work below.
    await session.rollback()
    offset = diffs.decode_diff_cursor(cursor) if cursor is not None else 0
    etag = diffs.diff_etag(before.content_sha256, after.content_sha256)
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    # Revalidation after authorization and visibility; a 304 does no diff work.
    if if_none_match_hit(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    result, verdict = await asyncio.to_thread(diffs.compute, before.document, after.document)
    page = diffs.diff_page(result, verdict, offset, limit)
    response.headers.update(headers)
    return ContractDiffOut.model_validate(
        {
            "from": _side_out(before),
            "to": _side_out(after),
            "classifier_version": page.classifier_version,
            "required_bump": page.required_bump,
            "counts": page.counts,
            "changes": page.changes,
            "changes_total": page.changes_total,
            "truncated": page.truncated,
            "next_cursor": page.next_cursor,
        }
    )


_LIFECYCLE_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {
        "description": (
            "Contract or version not found, not visible to the caller, or `ref` is not a "
            "strict SemVer string (the `latest` alias is read-only)"
        )
    },
    409: {
        "description": (
            "The folder is archived, or the version's state does not allow the operation "
            "(version-not-published, latest-version-protected, version-pinned: a card still "
            "pins the version; `reasons` names every blocker and every pinning card; "
            "contract-referenced on republish: the version links to a deleted contract, counted "
            "in `deleted_target_count`)"
        )
    },
    503: {"description": "Authorization is temporarily unavailable"},
}


async def _require_reason(request: Request, reason: str | None) -> None:
    """G3: with "require a reason" on, a missing reason is 422 before any lock or write."""
    if reason is not None:
        return
    governance = await effective_governance(
        request.app.state.settings_client, parse_bearer(request), request.app.state.settings
    )
    if governance.require_lifecycle_reason:
        raise ProblemError(
            422,
            "lifecycle-reason-required",
            "Unprocessable Content",
            "A reason is required to unpublish or deprecate a version.",
            errors=[{"pointer": "/reason", "detail": "A reason is required."}],
        )


def _writable_facts(request: Request) -> None:
    """Refuse a lifecycle write in an archived folder (409)."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)


async def _metadata_after_write(
    session: AsyncSession, contract_id: uuid.UUID, version: ContractVersion
) -> ContractVersionMetadataOut:
    """The metadata of ``version`` against the contract's Latest as it stands now."""
    contract = await read_contract(session, contract_id)
    return _metadata_out(contract_id, version, await latest_parts(session, contract))


@router.post(
    "/contracts/{contract_id}/versions/{ref}/promote",
    response_model=ContractVersionMetadataOut,
    dependencies=[require("contract:publish", target=contract_folder)],
    responses=_LIFECYCLE_RESPONSES,
)
async def promote_contract_version(
    contract_id: uuid.UUID, ref: str, request: Request, uow: UnitOfWorkDep
) -> ContractVersionMetadataOut:
    """Make a published version (a Preview or an older one) the Latest; the Latest is a no-op."""
    _writable_facts(request)
    version, changed = await promote_version(uow, contract_id, ref)
    if changed:
        await uow.commit()
    return await _metadata_after_write(uow.session, contract_id, version)


async def _record_blocked_unpublish(
    uow: UnitOfWorkDep, contract_id: uuid.UUID, exc: LifecycleBlocked
) -> None:
    """Audit a refused unpublish so the pinning cards are told (D-99 as amended, D-101).

    The unit is rolled back first: that releases the contract and version locks the refused
    operation still holds, so recording on the same session cannot wait on itself (T-04.1-21).
    Any failure here is logged (ids and error class only) and swallowed: the client always gets
    the 409 (T-04.1-20).
    """
    try:
        await uow.session.rollback()
        ancestors = (
            await uow.session.execute(
                select(Contract.ancestor_ids).where(Contract.id == contract_id)
            )
        ).scalar_one_or_none()
        uow.audit(
            "contract.version_unpublish_blocked",
            "contract",
            contract_id,
            before=None,
            after={
                "version_id": str(exc.version_id),
                "semver": exc.semver,
                "notify_card_ids": sorted(str(i) for i in exc.pinned_card_ids),
            },
            scope_path=list(ancestors or ()),
        )
        await uow.commit()
    except Exception as failure:
        _log.warning(
            "blocked_unpublish_not_recorded",
            contract_id=str(contract_id),
            version_id=str(exc.version_id),
            error=type(failure).__name__,
        )


@router.post(
    "/contracts/{contract_id}/versions/{ref}/unpublish",
    response_model=ContractVersionMetadataOut,
    dependencies=[require("contract:publish", target=contract_folder)],
    responses={
        **_LIFECYCLE_RESPONSES,
        422: {
            "description": (
                "The body is not `{reason?}`, or a reason is required and missing "
                "(lifecycle-reason-required)"
            )
        },
    },
)
async def unpublish_contract_version(
    contract_id: uuid.UUID,
    ref: str,
    request: Request,
    uow: UnitOfWorkDep,
    body: ContractVersionUnpublishIn | None = None,
) -> ContractVersionMetadataOut:
    """Hide a non-Latest version from consumers; the row and its bytes stay.

    The body is optional (`{reason}`); it is required while the governance setting "require a
    reason" is on (422 `lifecycle-reason-required`, checked before any lock is taken).
    """
    _writable_facts(request)
    reason = body.reason if body is not None else None
    await _require_reason(request, reason)
    try:
        version, changed = await unpublish_version(uow, contract_id, ref, reason)
    except LifecycleBlocked as exc:
        if exc.pinned_card_ids:
            await _record_blocked_unpublish(uow, contract_id, exc)
        raise
    if changed:
        await uow.commit()
    return await _metadata_after_write(uow.session, contract_id, version)


@router.post(
    "/contracts/{contract_id}/versions/{ref}/republish",
    response_model=ContractVersionMetadataOut,
    dependencies=[require("contract:publish", target=contract_folder)],
    responses=_LIFECYCLE_RESPONSES,
)
async def republish_contract_version(
    contract_id: uuid.UUID, ref: str, request: Request, uow: UnitOfWorkDep
) -> ContractVersionMetadataOut:
    """Restore an unpublished version as a Preview or an older one, never as the Latest."""
    _writable_facts(request)
    version, changed = await republish_version(uow, contract_id, ref)
    if changed:
        await uow.commit()
    return await _metadata_after_write(uow.session, contract_id, version)


@router.post(
    "/contracts/{contract_id}/versions/{ref}/deprecate",
    response_model=ContractVersionMetadataOut,
    dependencies=[require("contract:publish", target=contract_folder)],
    responses={
        **_LIFECYCLE_RESPONSES,
        422: {
            "description": (
                "The body is not `{sunset_date: <date>, reason?}`, the sunset date is in the past "
                "(invalid-sunset-date), or a reason is required and missing "
                "(lifecycle-reason-required)"
            )
        },
    },
)
async def deprecate_contract_version(
    contract_id: uuid.UUID,
    ref: str,
    body: ContractVersionDeprecateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> ContractVersionMetadataOut:
    """Flag a published version as deprecated with a sunset date; it stays fetchable."""
    _writable_facts(request)
    await _require_reason(request, body.reason)
    version, changed = await deprecate_version(uow, contract_id, ref, body.sunset_date, body.reason)
    if changed:
        await uow.commit()
    _add_deprecation_headers(response, version)
    return await _metadata_after_write(uow.session, contract_id, version)


@router.post(
    "/contracts/{contract_id}/subscriptions/{subscription_id}/remove",
    response_model=CardSubscriptionOut,
    dependencies=[require("contract:publish", target=contract_folder)],
    responses={
        200: {"description": "The pin was ended, or had already ended (no change, no event)"},
        401: {"description": "Missing or invalid credentials"},
        403: {"description": "The caller can read the contract but may not publish it"},
        404: {
            "description": (
                "Contract not found or not visible (also for a Consumer or Policy Manager), or "
                "the subscription does not belong to this contract"
            )
        },
        409: {"description": "The contract's folder is archived (folder-archived)"},
        422: {"description": "The body is not `{reason}` with a reason of 1..500 characters"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def remove_contract_subscription(
    contract_id: uuid.UUID,
    subscription_id: uuid.UUID,
    body: ContractSubscriptionRemoveIn,
    request: Request,
    uow: UnitOfWorkDep,
) -> CardSubscriptionOut:
    """End a card's pin with a reason the card is told about; ending an ended pin is a no-op."""
    _writable_facts(request)
    pin, changed = await remove_subscription(uow, contract_id, subscription_id, body.reason)
    if changed:
        await uow.commit()
    stored, semver = await pin_with_semver(uow.session, pin.id)
    return _subscription_out(stored, semver)


__all__ = ["router"]
