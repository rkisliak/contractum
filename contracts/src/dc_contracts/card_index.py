"""The all-cards index behind ``GET /api/v1/contracts/cards`` (SUB-01 list across folders).

Visibility is the card-read rule turned into SQL: a card is listed when the caller can read its
folder (``accessible_folders(folder:read)``, a compact ``FolderScope`` through
:func:`dc_contracts.search.scope_clause`) or is a linked member of it (one of the caller's
grantees in ``card_member``). Nothing else ever appears, so the totals are counts of cards the
caller could open one by one and never reveal a hidden card.

A pinned contract's name is shown only when the caller could list that contract
(``contract:read-metadata``, ``contract:read`` or ``contract:read-published`` scope, not deleted);
the contract id and the pinned SemVer are part of the card read already (D-88, D-102).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from dc_auth.authorizer import FolderScope
from sqlalchemy import (
    ColumnElement,
    and_,
    distinct,
    exists,
    false,
    func,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.models import (
    CardMember,
    CardSubscription,
    Contract,
    ContractVersion,
    SubscriberCard,
)
from dc_contracts.search import like_escape, scope_clause

# How many active pins each listed card shows inline; the rest is a count.
PIN_PREVIEW_LIMIT = 5


@dataclass(frozen=True)
class CardIndexFilters:
    folder_id: uuid.UUID | None = None
    subtree: bool = True
    q: str | None = None
    include_archived: bool = False
    has_active_subscriptions: bool | None = None
    cursor: uuid.UUID | None = None
    limit: int = 50


@dataclass(frozen=True)
class CardIndexScopes:
    """Everything identity told us about the caller, fetched before any query."""

    folder_read: FolderScope
    grantees: frozenset[tuple[str, str]]
    contract_names: tuple[FolderScope, ...] = ()


@dataclass(frozen=True)
class PinPreview:
    contract_id: uuid.UUID
    version_id: uuid.UUID
    semver: str
    contract_name: str | None


@dataclass
class CardIndexRow:
    card: SubscriberCard
    active_subscription_count: int
    pins: list[PinPreview] = field(default_factory=list)


@dataclass(frozen=True)
class CardIndexPage:
    rows: list[CardIndexRow]
    next_cursor: uuid.UUID | None
    total_cards: int
    total_folders: int


def _visible(scopes: CardIndexScopes) -> ColumnElement[bool] | None:
    folder = scope_clause(
        scopes.folder_read,
        folder_col=SubscriberCard.folder_id,
        ancestors_col=SubscriberCard.ancestor_ids,
    )
    member = (
        exists(
            select(CardMember.card_id).where(
                CardMember.card_id == SubscriberCard.id,
                tuple_(CardMember.member_type, CardMember.member_id).in_(sorted(scopes.grantees)),
            )
        )
        if scopes.grantees
        else None
    )
    clauses = [clause for clause in (folder, member) if clause is not None]
    return or_(*clauses) if clauses else None


def _active_pins(card_id: Any) -> ColumnElement[bool]:
    return and_(CardSubscription.card_id == card_id, CardSubscription.ended_at.is_(None))


def _where(filters: CardIndexFilters, visible: ColumnElement[bool]) -> list[ColumnElement[bool]]:
    where: list[ColumnElement[bool]] = [visible]
    if not filters.include_archived:
        where.append(SubscriberCard.archived_at.is_(None))
    if filters.folder_id is not None:
        if filters.subtree:
            where.append(SubscriberCard.ancestor_ids.contains([filters.folder_id]))
        else:
            where.append(SubscriberCard.folder_id == filters.folder_id)
    if filters.q:
        pattern = f"%{like_escape(filters.q)}%"
        where.append(
            or_(
                SubscriberCard.name.ilike(pattern, escape="\\"),
                SubscriberCard.description.ilike(pattern, escape="\\"),
            )
        )
    if filters.has_active_subscriptions is not None:
        active = exists(select(CardSubscription.id).where(_active_pins(SubscriberCard.id)))
        where.append(active if filters.has_active_subscriptions else ~active)
    return where


async def list_visible_cards(
    session: AsyncSession, scopes: CardIndexScopes, filters: CardIndexFilters
) -> CardIndexPage:
    """One keyset page (uuidv7 id order) of the cards the caller may read, with totals."""
    visible = _visible(scopes)
    if visible is None:
        return CardIndexPage(rows=[], next_cursor=None, total_cards=0, total_folders=0)
    where = _where(filters, visible)

    totals = (
        await session.execute(
            select(func.count(), func.count(distinct(SubscriberCard.folder_id))).where(*where)
        )
    ).one()

    active_count = (
        select(func.count())
        .where(_active_pins(SubscriberCard.id))
        .correlate(SubscriberCard)
        .scalar_subquery()
    )
    page_where = list(where)
    if filters.cursor is not None:
        page_where.append(SubscriberCard.id > filters.cursor)
    result = await session.execute(
        select(SubscriberCard, active_count.label("active_count"))
        .where(*page_where)
        .order_by(SubscriberCard.id)
        .limit(filters.limit + 1)
    )
    fetched = [(card, int(count)) for card, count in result.all()]
    next_cursor = None
    if len(fetched) > filters.limit:
        fetched = fetched[: filters.limit]
        next_cursor = fetched[-1][0].id
    rows = [CardIndexRow(card=card, active_subscription_count=count) for card, count in fetched]
    if rows:
        await _attach_pins(session, scopes, rows)
    return CardIndexPage(
        rows=rows,
        next_cursor=next_cursor,
        total_cards=int(totals[0]),
        total_folders=int(totals[1]),
    )


async def _attach_pins(
    session: AsyncSession, scopes: CardIndexScopes, rows: list[CardIndexRow]
) -> None:
    """The newest ``PIN_PREVIEW_LIMIT`` active pins of every card on the page, in one statement."""
    names = [
        clause
        for scope in scopes.contract_names
        if (
            clause := scope_clause(
                scope, folder_col=Contract.folder_id, ancestors_col=Contract.ancestor_ids
            )
        )
        is not None
    ]
    name_visible: ColumnElement[bool] = (
        and_(or_(*names), Contract.deleted_at.is_(None)) if names else false()
    )
    ranked = (
        select(
            CardSubscription.card_id.label("card_id"),
            CardSubscription.contract_id.label("contract_id"),
            CardSubscription.version_id.label("version_id"),
            ContractVersion.semver.label("semver"),
            ContractVersion.name.label("name"),
            name_visible.label("name_visible"),
            func.row_number()
            .over(partition_by=CardSubscription.card_id, order_by=CardSubscription.id.desc())
            .label("rank"),
        )
        .join(ContractVersion, ContractVersion.id == CardSubscription.version_id)
        .join(Contract, Contract.id == CardSubscription.contract_id)
        .where(
            CardSubscription.card_id.in_([row.card.id for row in rows]),
            CardSubscription.ended_at.is_(None),
        )
        .subquery()
    )
    result = await session.execute(
        select(ranked)
        .where(ranked.c.rank <= PIN_PREVIEW_LIMIT)
        .order_by(ranked.c.card_id, ranked.c.rank)
    )
    by_card = {row.card.id: row for row in rows}
    for pin in result.mappings():
        by_card[pin["card_id"]].pins.append(
            PinPreview(
                contract_id=pin["contract_id"],
                version_id=pin["version_id"],
                semver=pin["semver"] or "",
                contract_name=pin["name"] if pin["name_visible"] else None,
            )
        )


__all__ = [
    "PIN_PREVIEW_LIMIT",
    "CardIndexFilters",
    "CardIndexPage",
    "CardIndexRow",
    "CardIndexScopes",
    "PinPreview",
    "list_visible_cards",
]
