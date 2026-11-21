"""Leaf queries over a card's pins (SUB-03, SUB-04).

This module never imports ``versions``: the lifecycle code (unpublish, delete) and the subscribe
code both read pins, and a leaf with models-only imports keeps that import graph acyclic. Every
read overwrites a stale identity-map copy, because pins change under locks held by other requests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import cmp_to_key
from typing import Any, Final, Literal

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.lifecycle import compare_semver, parse_semver
from dc_contracts.models import CardSubscription, ContractVersion, SubscriberCard

# The card read embeds at most this many rows; the paged route gives the complete record (A13).
SUBSCRIPTIONS_EMBED_LIMIT: Final = 500

PinState = Literal["active", "ended", "all"]


async def active_pin(
    session: AsyncSession, card_id: uuid.UUID, version_id: uuid.UUID
) -> CardSubscription | None:
    """The card's active pin on exactly this version, if any."""
    return (
        await session.execute(
            select(CardSubscription)
            .where(
                CardSubscription.card_id == card_id,
                CardSubscription.version_id == version_id,
                CardSubscription.ended_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def card_subscriptions(
    session: AsyncSession,
    card_id: uuid.UUID,
    *,
    state: PinState,
    cursor: uuid.UUID | None,
    limit: int,
) -> list[tuple[CardSubscription, str]]:
    """Up to ``limit`` pins of the card, newest first, each with its version's SemVer.

    Ids are uuidv7 and unique, so ``ORDER BY id DESC`` is total and stable; the keyset cursor is
    the last id already returned (``id < cursor``). One statement, so one snapshot: a concurrent
    move shows the old pin or the new one as active, never both and never neither.
    """
    query = (
        select(CardSubscription, ContractVersion.semver)
        .join(ContractVersion, ContractVersion.id == CardSubscription.version_id)
        .where(CardSubscription.card_id == card_id)
    )
    if state == "active":
        query = query.where(CardSubscription.ended_at.is_(None))
    elif state == "ended":
        query = query.where(CardSubscription.ended_at.is_not(None))
    if cursor is not None:
        query = query.where(CardSubscription.id < cursor)
    rows = await session.execute(
        query.order_by(CardSubscription.id.desc())
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    return [(sub, semver or "") for sub, semver in rows.all()]


@dataclass(frozen=True)
class SubscriberRow:
    """One active pin as the contract's owners see it (D-102): the owner-view columns only."""

    subscription_id: uuid.UUID
    version_id: uuid.UUID
    semver: str
    deprecated: bool
    sunset_date: date | None
    card_id: uuid.UUID
    name: str
    description: str
    contacts: list[dict[str, Any]]
    pinned_at: datetime
    pinned_by: str


async def contract_subscribers(
    session: AsyncSession, contract_id: uuid.UUID, *, cursor: uuid.UUID | None, limit: int
) -> list[SubscriberRow]:
    """Up to ``limit + 1`` active pins on the contract in subscription-id (uuidv7) order.

    The extra row tells the caller whether another page exists. Only the owner-view columns are
    selected: never the card's members, channels or webhook URL, and never its pins on other
    contracts (T-04.1-16). One statement, so one snapshot.
    """
    query = (
        select(
            CardSubscription.id,
            CardSubscription.version_id,
            ContractVersion.semver,
            ContractVersion.deprecated_at.is_not(None),
            ContractVersion.sunset_date,
            SubscriberCard.id,
            SubscriberCard.name,
            SubscriberCard.description,
            SubscriberCard.contacts,
            CardSubscription.created_at,
            CardSubscription.created_by,
        )
        .join(ContractVersion, ContractVersion.id == CardSubscription.version_id)
        .join(SubscriberCard, SubscriberCard.id == CardSubscription.card_id)
        .where(CardSubscription.contract_id == contract_id, CardSubscription.ended_at.is_(None))
    )
    if cursor is not None:
        query = query.where(CardSubscription.id > cursor)
    rows = await session.execute(query.order_by(CardSubscription.id).limit(limit + 1))
    return [
        SubscriberRow(
            subscription_id=r[0],
            version_id=r[1],
            semver=r[2] or "",
            deprecated=bool(r[3]),
            sunset_date=r[4],
            card_id=r[5],
            name=r[6],
            description=r[7],
            contacts=list(r[8]),
            pinned_at=r[9],
            pinned_by=r[10],
        )
        for r in rows.all()
    ]


async def pin_with_semver(
    session: AsyncSession, subscription_id: uuid.UUID
) -> tuple[CardSubscription, str]:
    """One pin and its version's SemVer (the pin must exist)."""
    row = (
        await session.execute(
            select(CardSubscription, ContractVersion.semver)
            .join(ContractVersion, ContractVersion.id == CardSubscription.version_id)
            .where(CardSubscription.id == subscription_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    return row[0], row[1] or ""


@dataclass(frozen=True)
class PinnedVersion:
    """One active pin as a lifecycle refusal names it (D-95)."""

    subscription_id: uuid.UUID
    version_id: uuid.UUID
    semver: str
    pinned_at: datetime
    pinned_by: str


@dataclass(frozen=True)
class PinnedCard:
    """A card with at least one active pin that blocks a lifecycle operation (D-95)."""

    card_id: uuid.UUID
    name: str
    contacts: list[dict[str, Any]]
    pins: list[PinnedVersion]


def _by_semver_descending(a: PinnedVersion, b: PinnedVersion) -> int:
    pa, pb = parse_semver(a.semver), parse_semver(b.semver)
    if pa is None or pb is None:  # pragma: no cover - stored versions are strict SemVer
        return (a.semver < b.semver) - (a.semver > b.semver)
    return -compare_semver(pa, pb)


async def _pinned_cards(
    session: AsyncSession, *conditions: ColumnElement[bool]
) -> list[PinnedCard]:
    """Active pins of non-archived cards matching ``conditions``, grouped per card.

    One statement. Cards are ordered by ``(lower(name), id)`` and each card's pins by SemVer
    precedence, highest first. There is no visibility filter: a card is named whatever its
    organization, because the pin sits on the owner's own contract and the owner must be able to
    reach the consumer (D-95, a deliberate exception to D-54).
    """
    rows = await session.execute(
        select(
            CardSubscription.id,
            CardSubscription.version_id,
            ContractVersion.semver,
            CardSubscription.created_at,
            CardSubscription.created_by,
            SubscriberCard.id,
            SubscriberCard.name,
            SubscriberCard.contacts,
        )
        .join(ContractVersion, ContractVersion.id == CardSubscription.version_id)
        .join(SubscriberCard, SubscriberCard.id == CardSubscription.card_id)
        .where(
            CardSubscription.ended_at.is_(None), SubscriberCard.archived_at.is_(None), *conditions
        )
    )
    grouped: dict[uuid.UUID, PinnedCard] = {}
    for r in rows.all():
        card = grouped.setdefault(
            r[5], PinnedCard(card_id=r[5], name=r[6], contacts=list(r[7]), pins=[])
        )
        card.pins.append(
            PinnedVersion(
                subscription_id=r[0],
                version_id=r[1],
                semver=r[2] or "",
                pinned_at=r[3],
                pinned_by=r[4],
            )
        )
    for card in grouped.values():
        card.pins.sort(key=cmp_to_key(_by_semver_descending))
    return sorted(grouped.values(), key=lambda c: (c.name.lower(), str(c.card_id)))


async def pinned_cards_for_version(
    session: AsyncSession, version_id: uuid.UUID
) -> list[PinnedCard]:
    """The cards with an active pin on exactly this version (called under the version lock)."""
    return await _pinned_cards(session, CardSubscription.version_id == version_id)


async def pinned_cards_for_contract(
    session: AsyncSession, contract_id: uuid.UUID
) -> list[PinnedCard]:
    """The cards with an active pin on any version of the contract (called under its lock)."""
    return await _pinned_cards(session, CardSubscription.contract_id == contract_id)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def pinned_card_out(card: PinnedCard) -> dict[str, Any]:
    """The JSON shape of one named card in a 409 ``reasons`` entry: every contact, every pin."""
    return {
        "card_id": str(card.card_id),
        "name": card.name,
        "contacts": [
            {"name": c.get("name"), "kind": c["kind"], "value": c["value"]} for c in card.contacts
        ],
        "pins": [
            {
                "subscription_id": str(p.subscription_id),
                "version_id": str(p.version_id),
                "semver": p.semver,
                "pinned_at": _iso(p.pinned_at),
                "pinned_by": p.pinned_by,
            }
            for p in card.pins
        ],
    }
