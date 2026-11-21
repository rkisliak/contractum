"""Card notices: lifecycle events recorded on the cards that depend on them (SUB-06, SUB-07).

The audience of a lifecycle event travels inside the operation's own audit event
(``notify_card_ids`` in its ``after``), computed under the contract lock, so a rolled-back
operation leaves no event and a committed one always yields its notices once its event is
handled. ``handle_payload`` is the transport-agnostic entry, like
``dc_audit.ingest.handle_payload`` (D-21): the JetStream consumer calls it (Plan 09), and the
tests call it through ``Stack.pump_notices``. It validates the envelope, lets the action's
handler name the audience, and materialises one ``card_notice`` per card with one pending
``card_notice_delivery`` per channel the card has enabled, exactly once per card and event
(``ON CONFLICT DO NOTHING`` on ``uq_card_notice_card_dedupe``).

This module never imports ``versions`` (``versions`` imports it), so there is no import cycle.
Notice payloads carry identifiers, SemVer and event fields only, never document content, contacts
or members (T-04.1-14).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cmp_to_key
from typing import TYPE_CHECKING, Any, Final, NamedTuple
from urllib.parse import urlencode

from dc_core.events import subject_for
from dc_odcs.diff import compare
from dc_odcs.diff.model import value_allowed
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text

from dc_contracts.lifecycle import SemverParts, compare_semver, parse_semver
from dc_contracts.models import (
    NOTICE_EVENT_TYPES,
    CardNotice,
    CardNoticeDelivery,
    CardSubscription,
    Contract,
    ContractVersion,
    SubscriberCard,
)
from dc_contracts.settings import ContractsSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOTICE_TYPES: tuple[str, ...] = NOTICE_EVENT_TYPES

# A payload may never carry these, at any depth (T-04.1-14, T-6-36); ``notice_payload`` refuses
# them. The second row is the free text and structured content of a contract document: a breaking
# change names an element and its constraint, never what the author wrote about it.
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "document",
        "raw_document",
        "contacts",
        "members",
        "description",
        "examples",
        "quality",
        "query",
        "customProperties",
    }
)

# The breaking notice looks at no more distinct pinned versions than this per event (T-6-38),
# newest first; the rest are counted in ``truncated_pins``.
MAX_BREAKING_PIN_VERSIONS: Final = 25
# Changes listed per pinned version; ``changes_total`` carries the number found (D-164).
MAX_BREAKING_CHANGES: Final = 50
BREAKING_EVENT_TYPE: Final = "contract.version.breaking_published"


class InvalidNoticeEvent(ValueError):
    """The envelope is malformed, is not contracts' own, or does not match its subject."""


class NoticePlan(NamedTuple):
    """What a handler decided one event means: who to tell and what to record."""

    card_ids: list[str]
    event_type: str
    contract_id: uuid.UUID
    version_id: uuid.UUID | None
    payload: dict[str, Any]
    # One event may yield several plans (the published notice and the breaking notice, one per
    # audience group); each carries its own key, unique per card (``uq_card_notice_card_dedupe``).
    dedupe_key: str


class _Envelope(BaseModel):
    """The envelope fields the materialiser needs; the rest is the audit store's business."""

    event_id: uuid.UUID
    occurred_at: datetime
    service: str
    aggregate_type: str
    aggregate_id: str
    action: str
    actor: dict[str, Any] | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


Handler = Callable[
    ["AsyncSession", _Envelope, ContractsSettings],
    Awaitable[list[NoticePlan]],
]


def _event_key(envelope: _Envelope) -> str:
    """The dedupe key of the one notice an event usually yields."""
    return f"event:{envelope.event_id}"


async def _audience(session: AsyncSession, *conditions: Any) -> list[str]:
    """Sorted distinct ids of non-archived cards with an active pin matching ``conditions``."""
    rows = await session.execute(
        select(CardSubscription.card_id)
        .join(SubscriberCard, SubscriberCard.id == CardSubscription.card_id)
        .where(
            CardSubscription.ended_at.is_(None),
            SubscriberCard.archived_at.is_(None),
            *conditions,
        )
        .distinct()
    )
    return sorted(str(card_id) for card_id in rows.scalars())


async def audience_for_version(session: AsyncSession, version_id: uuid.UUID) -> list[str]:
    """The cards with an active pin on exactly this version (one indexed read, no write)."""
    return await _audience(session, CardSubscription.version_id == version_id)


async def audience_for_contract(session: AsyncSession, contract_id: uuid.UUID) -> list[str]:
    """The cards with an active pin on any version of this contract, each listed once."""
    return await _audience(session, CardSubscription.contract_id == contract_id)


async def pins_for_contract(session: AsyncSession, contract_id: uuid.UUID) -> list[dict[str, str]]:
    """Every active pin on this contract as ``{card_id, version_id}`` (identifiers only, D-163).

    Sorted, so the snapshot in the publish event is deterministic. Archived cards have no active
    pin (archiving ends them) and are left out like in ``audience_for_contract``.
    """
    rows = await session.execute(
        select(CardSubscription.card_id, CardSubscription.version_id)
        .join(SubscriberCard, SubscriberCard.id == CardSubscription.card_id)
        .where(
            CardSubscription.ended_at.is_(None),
            SubscriberCard.archived_at.is_(None),
            CardSubscription.contract_id == contract_id,
        )
        .distinct()
    )
    return sorted(
        ({"card_id": str(card), "version_id": str(version)} for card, version in rows.all()),
        key=lambda pin: (pin["card_id"], pin["version_id"]),
    )


def _assert_identifiers_only(value: Any) -> None:
    if isinstance(value, dict):
        bad = _FORBIDDEN_PAYLOAD_KEYS & value.keys()
        if bad:
            raise ValueError(f"a notice payload must not carry {sorted(bad)}")
        for item in value.values():
            _assert_identifiers_only(item)
    elif isinstance(value, list):
        for item in value:
            _assert_identifiers_only(item)


def notice_payload(
    *,
    contract_id: uuid.UUID | str,
    odcs_id: str,
    version_id: uuid.UUID | str | None,
    semver: str | None,
    **extra: Any,
) -> dict[str, Any]:
    """The allow-listed notice payload: contract and version identifiers, SemVer, event fields."""
    payload: dict[str, Any] = {"contract": {"id": str(contract_id), "odcs_id": odcs_id}}
    if version_id is not None:
        payload["version"] = {"id": str(version_id), "semver": semver}
    payload.update(extra)
    _assert_identifiers_only(payload)
    return payload


_MATERIALISE = text(
    """
    WITH n AS (
        INSERT INTO card_notice
            (card_id, event_type, contract_id, version_id, payload, dedupe_key,
             source_event_id, occurred_at)
        SELECT c, CAST(:event_type AS text), CAST(:contract_id AS uuid),
               CAST(:version_id AS uuid), CAST(:payload AS jsonb), CAST(:dedupe_key AS text),
               CAST(:source_event_id AS uuid), CAST(:occurred_at AS timestamptz)
        FROM unnest(CAST(:card_ids AS uuid[])) AS c
        ON CONFLICT ON CONSTRAINT uq_card_notice_card_dedupe DO NOTHING
        RETURNING id, card_id
    ), d AS (
        INSERT INTO card_notice_delivery (notice_id, channel)
        SELECT n.id, ch.channel
        FROM n
        JOIN subscriber_card k ON k.id = n.card_id
        CROSS JOIN LATERAL (
            VALUES ('email', k.email_enabled), ('webhook', k.webhook_enabled)
        ) AS ch(channel, enabled)
        WHERE ch.enabled
        RETURNING 1
    )
    SELECT count(*) FROM n
    """
)


async def materialise(
    session: AsyncSession,
    *,
    card_ids: list[str],
    event_type: str,
    contract_id: uuid.UUID | str,
    version_id: uuid.UUID | str | None,
    payload: dict[str, Any],
    dedupe_key: str,
    occurred_at: datetime,
    source_event_id: uuid.UUID | str | None = None,
) -> int:
    """Record one notice per card and one pending delivery per channel the card has enabled.

    One statement. The delivery insert reads only the notice rows the first insert RETURNED, so a
    dedupe conflict (the same event handled again) creates neither a notice nor a delivery row.
    Returns the number of notices inserted.
    """
    unique_cards = sorted(set(card_ids))
    if not unique_cards:
        return 0
    result = await session.execute(
        _MATERIALISE,
        {
            "event_type": event_type,
            "contract_id": str(contract_id),
            "version_id": None if version_id is None else str(version_id),
            "payload": json.dumps(payload, separators=(",", ":")),
            "dedupe_key": dedupe_key,
            "source_event_id": None if source_event_id is None else str(source_event_id),
            "occurred_at": occurred_at,
            "card_ids": unique_cards,
        },
    )
    return int(result.scalar_one())


async def notify_card(
    session: AsyncSession,
    *,
    card_id: uuid.UUID | str,
    event_type: str,
    contract_id: uuid.UUID | str,
    version_id: uuid.UUID | str | None,
    payload: dict[str, Any],
    dedupe_key: str,
    occurred_at: datetime,
    source_event_id: uuid.UUID | str | None = None,
) -> int:
    """Record a notice on one card (used by the sunset scheduler and the card's own events)."""
    return await materialise(
        session,
        card_ids=[str(card_id)],
        event_type=event_type,
        contract_id=contract_id,
        version_id=version_id,
        payload=payload,
        dedupe_key=dedupe_key,
        occurred_at=occurred_at,
        source_event_id=source_event_id,
    )


async def _odcs_id(session: AsyncSession, contract_id: uuid.UUID) -> str:
    odcs_id = (
        await session.execute(select(Contract.odcs_id).where(Contract.id == contract_id))
    ).scalar_one_or_none()
    if odcs_id is None:
        raise InvalidNoticeEvent("the event names a contract that does not exist")
    return str(odcs_id)


def _contract_id(envelope: _Envelope) -> uuid.UUID:
    try:
        return uuid.UUID(envelope.aggregate_id)
    except ValueError:
        raise InvalidNoticeEvent("the event's aggregate is not a contract id") from None


def _uuid_field(mapping: Mapping[str, Any], key: str, what: str) -> uuid.UUID:
    """``mapping[key]`` as a UUID, or ``InvalidNoticeEvent`` (acknowledged once, never retried)."""
    try:
        return uuid.UUID(str(mapping[key]))
    except (KeyError, ValueError):
        raise InvalidNoticeEvent(f"the event does not carry a valid {what} id") from None


def _notify_ids(envelope: _Envelope) -> list[str] | None:
    """The audience a lifecycle event names; ``None`` for an event written before this phase."""
    ids = (envelope.after or {}).get("notify_card_ids")
    if not isinstance(ids, list):
        return None
    return [str(item) for item in ids]


async def _deprecated(
    session: AsyncSession, envelope: _Envelope, settings: ContractsSettings
) -> list[NoticePlan]:
    card_ids = _notify_ids(envelope)
    if card_ids is None:
        return []
    after = envelope.after or {}
    contract_id = _contract_id(envelope)
    version_id = _uuid_field(after, "id", "version")
    payload = notice_payload(
        contract_id=contract_id,
        odcs_id=await _odcs_id(session, contract_id),
        version_id=version_id,
        semver=after.get("semver"),
        sunset_date=after.get("sunset_date"),
        deprecated_at=after.get("deprecated_at"),
        **({"reason": after["reason"]} if after.get("reason") else {}),
    )
    return [
        NoticePlan(
            card_ids,
            "contract.version.deprecated",
            contract_id,
            version_id,
            payload,
            _event_key(envelope),
        )
    ]


async def _published(
    session: AsyncSession, envelope: _Envelope, settings: ContractsSettings
) -> list[NoticePlan]:
    card_ids = _notify_ids(envelope)
    if card_ids is None:
        return []
    after = envelope.after or {}
    contract_id = _contract_id(envelope)
    version_id = _uuid_field(after, "id", "version")
    payload = notice_payload(
        contract_id=contract_id,
        odcs_id=await _odcs_id(session, contract_id),
        version_id=version_id,
        semver=after.get("semver"),
        status=after.get("status"),
    )
    return [
        NoticePlan(
            card_ids,
            "contract.version.published",
            contract_id,
            version_id,
            payload,
            _event_key(envelope),
        )
    ]


async def _promoted(
    session: AsyncSession, envelope: _Envelope, settings: ContractsSettings
) -> list[NoticePlan]:
    card_ids = _notify_ids(envelope)
    if card_ids is None:
        return []
    before = envelope.before or {}
    version = (envelope.after or {}).get("version") or {}
    if not isinstance(version, Mapping):
        raise InvalidNoticeEvent("the promotion event does not carry its version")
    contract_id = _contract_id(envelope)
    version_id = _uuid_field(version, "id", "version")
    previous_id = before.get("latest_version_id")
    previous = (
        None if previous_id is None else {"id": previous_id, "semver": before.get("latest_semver")}
    )
    payload = notice_payload(
        contract_id=contract_id,
        odcs_id=await _odcs_id(session, contract_id),
        version_id=version_id,
        semver=version.get("semver"),
        previous_latest=previous,
    )
    return [
        NoticePlan(
            card_ids,
            "contract.latest.changed",
            contract_id,
            version_id,
            payload,
            _event_key(envelope),
        )
    ]


async def _subscription_removed(
    session: AsyncSession, envelope: _Envelope, settings: ContractsSettings
) -> list[NoticePlan]:
    """The card whose pin an owner removed (aggregate is the card) is told why, once."""
    after = envelope.after or {}
    try:
        card_id = uuid.UUID(envelope.aggregate_id)
        contract_id = uuid.UUID(str(after["contract_id"]))
        version_id = uuid.UUID(str(after["version_id"]))
    except (KeyError, ValueError):
        raise InvalidNoticeEvent(
            "the removal event does not name its card, contract and version"
        ) from None
    payload = notice_payload(
        contract_id=contract_id,
        odcs_id=await _odcs_id(session, contract_id),
        version_id=version_id,
        semver=after.get("semver"),
        reason=after.get("reason"),
        removed_by=(envelope.actor or {}).get("id"),
    )
    return [
        NoticePlan(
            [str(card_id)],
            "card.subscription.removed",
            contract_id,
            version_id,
            payload,
            _event_key(envelope),
        )
    ]


# --- the breaking-change notice (SUB-08, D-162 to D-164) ---------------------------------------


@dataclass(frozen=True)
class PinnedDiff:
    """The breaking changes between one pinned version and the new version (classifier order)."""

    version_id: uuid.UUID
    semver: str
    breaking_count: int
    changes: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class BreakingGroup:
    """Cards with the same set of affected pinned versions share one notice payload."""

    card_ids: tuple[str, ...]
    payload: dict[str, Any]


def _precedence(semver: str) -> SemverParts:
    parts = parse_semver(semver)
    return parts if parts is not None else SemverParts(semver, 0, 0, 0, "", "")


def _by_precedence(items: Mapping[uuid.UUID, str], *, newest_first: bool) -> list[uuid.UUID]:
    """The ids ordered by SemVer precedence (ties by id, so the order is total)."""

    def cmp(a: uuid.UUID, b: uuid.UUID) -> int:
        result = compare_semver(_precedence(items[a]), _precedence(items[b]))
        return result if result != 0 else (a.int > b.int) - (a.int < b.int)

    return sorted(items, key=cmp_to_key(cmp), reverse=newest_first)


def cap_pinned_versions(semvers: Mapping[uuid.UUID, str]) -> tuple[list[uuid.UUID], int]:
    """At most ``MAX_BREAKING_PIN_VERSIONS`` versions, newest first, and how many were left out."""
    ordered = _by_precedence(semvers, newest_first=True)
    kept = ordered[:MAX_BREAKING_PIN_VERSIONS]
    return kept, len(ordered) - len(kept)


def _change_entry(change: Mapping[str, Any]) -> dict[str, Any]:
    """One change as the notice carries it: code, name-keyed path, attribute and, for a value
    attribute on the classifier's allow-list only, the before and after values."""
    entry: dict[str, Any] = {
        "code": change["code"],
        "path": change["path"],
        "attribute": change.get("attribute"),
    }
    if value_allowed(str(change.get("element_kind")), change.get("attribute")):
        entry["before"] = change.get("before")
        entry["after"] = change.get("after")
    return entry


def _section(contract_id: uuid.UUID, diff: PinnedDiff, new_semver: str) -> dict[str, Any]:
    return {
        "version_id": str(diff.version_id),
        "semver": diff.semver,
        "breaking_count": diff.breaking_count,
        "changes_total": len(diff.changes),
        "changes": [_change_entry(c) for c in diff.changes[:MAX_BREAKING_CHANGES]],
        # D-156: the full, paged comparison; the notice itself stays bounded. The versions are
        # query-encoded: a raw "+" of build metadata would decode as a space (IN-01).
        "diff_url": (
            f"/api/v1/contracts/contracts/{contract_id}/diff?"
            + urlencode({"from": diff.semver, "to": new_semver})
        ),
    }


def plan_breaking_notices(
    *,
    pins: Mapping[str, Sequence[uuid.UUID]],
    diffs: Mapping[uuid.UUID, PinnedDiff],
    contract_id: uuid.UUID,
    odcs_id: str,
    new_version_id: uuid.UUID,
    new_semver: str,
    classifier_version: str,
    truncated_pins: int = 0,
) -> list[BreakingGroup]:
    """Group the cards by the pinned versions that break, and build each group's payload.

    ``pins`` maps a card to every version it pins on the contract; ``diffs`` holds the examined
    versions only. A card is affected when at least one of its pinned versions has a breaking
    change; its notice carries one section per such version, oldest first (SemVer precedence).
    Pure: no I/O, so the grouping, ordering and caps are unit tested.
    """
    semvers = {version: diff.semver for version, diff in diffs.items()}
    groups: dict[tuple[uuid.UUID, ...], list[str]] = {}
    for card_id in sorted(pins):
        # The group is the card's whole set of examined pinned versions, so cards that pin the
        # same versions share one materialise call; a card with no breaking pin is left out.
        examined = {v for v in pins[card_id] if v in diffs}
        if any(diffs[v].breaking_count > 0 for v in examined):
            key = tuple(_by_precedence({v: semvers[v] for v in examined}, newest_first=False))
            groups.setdefault(key, []).append(card_id)
    planned: list[BreakingGroup] = []
    for key, card_ids in groups.items():
        affected = [v for v in key if diffs[v].breaking_count > 0]
        payload = notice_payload(
            contract_id=contract_id,
            odcs_id=odcs_id,
            version_id=new_version_id,
            semver=new_semver,
            classifier_version=classifier_version,
            truncated_pins=truncated_pins,
            pinned_versions=[_section(contract_id, diffs[v], new_semver) for v in affected],
        )
        planned.append(BreakingGroup(tuple(card_ids), payload))
    return planned


def _diff_pinned(
    new_document: dict[str, Any], documents: Mapping[uuid.UUID, tuple[str, dict[str, Any]]]
) -> tuple[dict[uuid.UUID, PinnedDiff], str]:
    """Diff every pinned document against the new one (CPU work: call from a worker thread)."""
    classifier = ""
    diffs: dict[uuid.UUID, PinnedDiff] = {}
    for version_id, (semver, document) in documents.items():
        result = compare(document, new_document)
        classifier = result.classifier_version
        breaking = tuple(c.to_json() for c in result.changes if c.cls == "breaking")
        diffs[version_id] = PinnedDiff(
            version_id, semver, int(result.counts.get("breaking", 0)), breaking
        )
    return diffs, classifier


def _notify_pins(envelope: _Envelope) -> dict[str, list[uuid.UUID]] | None:
    """The pin snapshot of a publish event (card -> pinned version ids); ``None`` if absent."""
    raw = (envelope.after or {}).get("notify_pins")
    if not isinstance(raw, list):
        return None
    pins: dict[str, list[uuid.UUID]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise InvalidNoticeEvent("a pin in the publish event is malformed")
        card = _uuid_field(item, "card_id", "card")
        version = _uuid_field(item, "version_id", "version")
        pins.setdefault(str(card), []).append(version)
    return pins


async def _breaking_published(
    session: AsyncSession, envelope: _Envelope, settings: ContractsSettings
) -> list[NoticePlan]:
    """Tell each card pinned to an older version what the new version breaks for it (D-162).

    The audience is the pin snapshot in the event, so a redelivery plans the same notices. The
    diffs run here, in a worker thread, after the publish committed: the publish never waits for
    them, and at most ``MAX_BREAKING_PIN_VERSIONS`` distinct pinned versions are examined.
    """
    pins = _notify_pins(envelope)
    if not pins:
        return []
    after = envelope.after or {}
    contract_id = _contract_id(envelope)
    new_id = _uuid_field(after, "id", "version")
    pinned_ids = {v for versions in pins.values() for v in versions} - {new_id}
    if not pinned_ids:
        return []
    semver_rows = await session.execute(
        select(ContractVersion.id, ContractVersion.semver).where(
            ContractVersion.id.in_(pinned_ids), ContractVersion.contract_id == contract_id
        )
    )
    semvers = {row.id: str(row.semver) for row in semver_rows.all()}
    examined, truncated = cap_pinned_versions(semvers)
    if not examined:
        return []
    document_rows = await session.execute(
        select(ContractVersion.id, ContractVersion.document).where(
            ContractVersion.id.in_([*examined, new_id])
        )
    )
    documents = {row.id: row.document for row in document_rows.all()}
    new_document = documents.pop(new_id, None)
    if new_document is None:
        raise InvalidNoticeEvent("the event names a version that does not exist")
    diffs, classifier = await asyncio.to_thread(
        _diff_pinned,
        new_document,
        {v: (semvers[v], documents[v]) for v in examined if v in documents},
    )
    groups = plan_breaking_notices(
        pins=pins,
        diffs=diffs,
        contract_id=contract_id,
        odcs_id=await _odcs_id(session, contract_id),
        new_version_id=new_id,
        new_semver=str(after.get("semver")),
        classifier_version=classifier,
        truncated_pins=truncated,
    )
    return [
        NoticePlan(
            list(group.card_ids),
            BREAKING_EVENT_TYPE,
            contract_id,
            new_id,
            group.payload,
            f"{_event_key(envelope)}:breaking",
        )
        for group in groups
    ]


_RECENT_BLOCKED = text(
    """
    SELECT DISTINCT card_id::text FROM card_notice
    WHERE event_type = 'contract.version.unpublish_blocked'
      AND version_id = CAST(:version_id AS uuid)
      AND card_id = ANY(CAST(:card_ids AS uuid[]))
      AND occurred_at > CAST(:occurred_at AS timestamptz)
                        - make_interval(secs => CAST(:cooldown AS double precision))
    """
)


async def _unpublish_blocked(
    session: AsyncSession, envelope: _Envelope, settings: ContractsSettings
) -> list[NoticePlan]:
    """Tell the pinning cards the owner wants to retire the version, at most once per cooldown.

    A card that already holds such a notice for this version, recorded within
    ``blocked_notice_cooldown_s`` of this event's time, is left out (T-04.1-20): repeated attempts
    are all audited, but never flood a consumer. The window is measured by event time, so a
    redelivery decides the same way it did the first time.
    """
    after = envelope.after or {}
    card_ids = _notify_ids(envelope)
    if not card_ids:
        return []
    try:
        version_id = uuid.UUID(str(after["version_id"]))
    except (KeyError, ValueError):
        raise InvalidNoticeEvent("the blocked event does not name its version") from None
    contract_id = _contract_id(envelope)
    recent = {
        row[0]
        for row in await session.execute(
            _RECENT_BLOCKED,
            {
                "version_id": str(version_id),
                "card_ids": sorted(set(card_ids)),
                "occurred_at": envelope.occurred_at,
                "cooldown": settings.blocked_notice_cooldown_s,
            },
        )
    }
    remaining = [card_id for card_id in card_ids if card_id not in recent]
    if not remaining:
        return []
    payload = notice_payload(
        contract_id=contract_id,
        odcs_id=await _odcs_id(session, contract_id),
        version_id=version_id,
        semver=after.get("semver"),
    )
    return [
        NoticePlan(
            remaining,
            "contract.version.unpublish_blocked",
            contract_id,
            version_id,
            payload,
            _event_key(envelope),
        )
    ]


# Keyed by audit action; an action without an entry records nothing. An action may have several
# handlers: a publish records the published notice and, separately, the breaking notice.
EVENT_HANDLERS: dict[str, tuple[Handler, ...]] = {
    "contract.version_published": (_published, _breaking_published),
    "contract.version_promoted": (_promoted,),
    "contract.version_deprecated": (_deprecated,),
    "card.subscription_removed": (_subscription_removed,),
    "contract.version_unpublish_blocked": (_unpublish_blocked,),
}


async def handle_payload(
    session_factory: async_sessionmaker[AsyncSession],
    payload: bytes,
    *,
    subject: str,
    settings: ContractsSettings | None = None,
) -> int:
    """Validate one envelope and record its notices in one transaction; returns how many.

    The envelope must be contracts' own and match ``subject_for(service, aggregate_type, action)``
    exactly, otherwise ``InvalidNoticeEvent`` (a producer cannot make contracts notify as another
    service, T-04.1-39). An action with no handler returns 0; so does an event without an
    audience. Handling the same event again returns 0 and writes nothing.
    """
    try:
        envelope = _Envelope.model_validate_json(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValidationError) as exc:
        raise InvalidNoticeEvent("the envelope is malformed") from exc
    if envelope.service != "contracts":
        raise InvalidNoticeEvent("the envelope is not from the contracts service")
    try:
        expected = subject_for(envelope.service, envelope.aggregate_type, envelope.action)
    except ValueError as exc:
        raise InvalidNoticeEvent("the envelope does not name a valid subject") from exc
    if subject != expected:
        raise InvalidNoticeEvent("the envelope does not match the subject it arrived on")
    handlers = EVENT_HANDLERS.get(envelope.action)
    if handlers is None:
        return 0
    resolved = settings or ContractsSettings()
    created = 0
    async with session_factory() as session, session.begin():
        for handler in handlers:
            for plan in await handler(session, envelope, resolved):
                if not plan.card_ids:
                    continue
                created += await materialise(
                    session,
                    card_ids=plan.card_ids,
                    event_type=plan.event_type,
                    contract_id=plan.contract_id,
                    version_id=plan.version_id,
                    payload=plan.payload,
                    dedupe_key=plan.dedupe_key,
                    occurred_at=envelope.occurred_at,
                    source_event_id=envelope.event_id,
                )
    return created


async def list_notices(
    session: AsyncSession, card_id: uuid.UUID, *, cursor: uuid.UUID | None, limit: int
) -> tuple[list[tuple[CardNotice, list[CardNoticeDelivery]]], uuid.UUID | None]:
    """A page of the card's notices, newest first (uuidv7 id), each with its deliveries.

    Keyset on ``id < cursor``; the second return value is the cursor of the next page, or
    ``None`` on the last one. Deliveries come from one query, ordered by channel.
    """
    query = select(CardNotice).where(CardNotice.card_id == card_id)
    if cursor is not None:
        query = query.where(CardNotice.id < cursor)
    rows = list(
        (
            await session.execute(
                query.order_by(CardNotice.id.desc())
                .limit(limit + 1)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    page = rows[:limit]
    next_id = page[-1].id if len(rows) > limit else None
    by_notice: dict[uuid.UUID, list[CardNoticeDelivery]] = {notice.id: [] for notice in page}
    if page:
        deliveries = await session.execute(
            select(CardNoticeDelivery)
            .where(CardNoticeDelivery.notice_id.in_(by_notice))
            .order_by(CardNoticeDelivery.notice_id, CardNoticeDelivery.channel)
            .execution_options(populate_existing=True)
        )
        for delivery in deliveries.scalars():
            by_notice[delivery.notice_id].append(delivery)
    return [(notice, by_notice[notice.id]) for notice in page], next_id


__all__ = [
    "BREAKING_EVENT_TYPE",
    "EVENT_HANDLERS",
    "MAX_BREAKING_CHANGES",
    "MAX_BREAKING_PIN_VERSIONS",
    "NOTICE_TYPES",
    "BreakingGroup",
    "InvalidNoticeEvent",
    "NoticePlan",
    "PinnedDiff",
    "audience_for_contract",
    "audience_for_version",
    "cap_pinned_versions",
    "handle_payload",
    "list_notices",
    "materialise",
    "notice_payload",
    "notify_card",
    "pins_for_contract",
    "plan_breaking_notices",
]
