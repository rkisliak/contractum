"""Pinned-version subscriptions: the domain service behind the card subscription routes (SUB-03).

A card records which published versions it depends on. Pinning never needs more than the acting
principal already has: the principal's own ``contract:read-published`` decision at the contract's
folder is what allows a pin (D-86), and a pin records dependency, never access (D-87, D-88), so no
fetch route consults pins and the card adds no authority.

Lock order for every write here is the one every lifecycle write uses: the contract row first
(``versions.lock_contract``), then the card ``FOR SHARE`` (an archive takes it ``FOR UPDATE``), then
the pin rows. A subscribe therefore cannot slip in between an unpublish or delete and its guard.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from dc_auth.authorizer import Authorizer, Outcome
from dc_auth.principal import Principal
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.cards import archived_problem
from dc_contracts.models import CardSubscription, Contract, ContractVersion, SubscriberCard
from dc_contracts.pins import active_pin
from dc_contracts.versions import (
    _version_not_published,
    get_version_by_semver,
    lock_contract,
    visible_states,
)

# D-83 seam, switched off: "a card grants access to the versions it pins". It is a code constant,
# not a setting, so no deployment can switch it on. Nothing reads it as a grant. Turning it on is a
# reviewed code change, and the contract policy (``contract.subscription_policy``, ``open`` only)
# must first move off ``open`` so that access is something an owner chose.
CARD_GRANTS_ACCESS: Final = False


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def subscription_snapshot(sub: CardSubscription, semver: str) -> dict[str, Any]:
    """Allow-listed, JSON-safe audit view of a pin: its id, the audited fields and the SemVer."""
    return {"id": str(sub.id), **UnitOfWork.snapshot(sub), "semver": semver}


async def _share_locked_card(session: AsyncSession, card_id: uuid.UUID) -> SubscriberCard:
    """Select the card ``FOR SHARE`` (an archive waits for us) and refresh any stale copy."""
    card = (
        await session.execute(
            select(SubscriberCard)
            .where(SubscriberCard.id == card_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if card is None:
        raise _not_found()
    return card


async def _resolve_pinnable_version(
    session: AsyncSession,
    authorizer: Authorizer,
    principal: Principal,
    contract_id: uuid.UUID,
    ref: str,
) -> tuple[Contract, ContractVersion]:
    """The version a new pin may name, with the contract locked (D-86, D-90).

    The acting principal's own read access decides first, before any lock is held over an identity
    call: a caller who cannot read the contract gets the unknown-contract 404. Then, under the
    contract lock: the policy seam, the SemVer lookup (``latest`` and non-SemVer references are
    404), an unpublished version (409 for a caller who may see unpublished versions, else 404) and
    a deprecated one (409).
    """
    folder_id = (
        await session.execute(
            select(Contract.folder_id).where(
                Contract.id == contract_id, Contract.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if folder_id is None:
        raise _not_found()
    decision = await authorizer.check(principal, "contract:read-published", folder_id, fresh=True)
    if decision.outcome is not Outcome.ALLOW:
        raise _not_found()
    contract = await lock_contract(session, contract_id)
    if contract.subscription_policy != "open":
        raise ProblemError(403, "forbidden", "Forbidden", "This contract does not accept pins.")
    version = await get_version_by_semver(
        session, contract_id, ref, states=frozenset({"published", "unpublished"})
    )
    if version is None:
        raise _not_found()
    if version.state == "unpublished":
        if "unpublished" in visible_states(decision.permissions):
            raise _version_not_published()
        raise _not_found()
    if version.deprecated_at is not None:
        raise ProblemError(
            409, "version-deprecated", "Conflict", "A deprecated version cannot be newly pinned."
        )
    return contract, version


async def subscribe(
    uow: UnitOfWork,
    authorizer: Authorizer,
    principal: Principal,
    card: SubscriberCard,
    contract_id: uuid.UUID,
    ref: str,
) -> tuple[CardSubscription, bool]:
    """Pin ``ref`` of the contract to the card; returns ``(pin, created)``.

    Pinning a version the card already actively pins is a no-op (no row, no event). Two concurrent
    identical subscribes are serialised by the contract lock: under READ COMMITTED the second sees
    the first's committed pin and is a no-op, so ``uq_card_subscription_active`` is only the
    database backstop (an IntegrityError there is a bug, not a mapped answer).
    """
    session = uow.session
    contract, version = await _resolve_pinnable_version(
        session, authorizer, principal, contract_id, ref
    )
    locked = await _share_locked_card(session, card.id)
    if locked.archived_at is not None:
        raise archived_problem()
    existing = await active_pin(session, locked.id, version.id)
    if existing is not None:
        return existing, False
    pin = CardSubscription(
        card_id=locked.id, contract_id=contract.id, version_id=version.id, mode="pinned"
    )
    session.add(pin)
    await session.flush()
    await session.refresh(pin)
    uow.audit(
        "card.subscribed",
        "card",
        locked.id,
        before=None,
        after=subscription_snapshot(pin, version.semver or ""),
        scope_path=locked.ancestor_ids,
    )
    return pin, True


def _ended() -> ProblemError:
    return ProblemError(
        409, "subscription-ended", "Conflict", "This subscription has already ended."
    )


async def _card_pin(
    session: AsyncSession, card_id: uuid.UUID, subscription_id: uuid.UUID, *, lock: bool
) -> CardSubscription:
    """A pin of this card (404 for any other card's pin), optionally ``FOR UPDATE``."""
    stmt = select(CardSubscription).where(
        CardSubscription.id == subscription_id, CardSubscription.card_id == card_id
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (
        await session.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if row is None:
        raise _not_found()
    return row


async def _semver_of(session: AsyncSession, version_id: uuid.UUID) -> str:
    semver = (
        await session.execute(
            select(ContractVersion.semver).where(ContractVersion.id == version_id)
        )
    ).scalar_one_or_none()
    return semver or ""


async def _end_pin(
    session: AsyncSession,
    uow: UnitOfWork,
    pin: CardSubscription,
    reason: str,
    *,
    note: str | None = None,
    replaced_by: uuid.UUID | None = None,
) -> None:
    """End a pin through the ORM (so the audit columns are stamped) and refresh it."""
    pin.ended_at = func.now()
    pin.ended_by = uow.actor.id
    pin.end_reason = reason
    pin.end_note = note
    pin.replaced_by_id = replaced_by
    await session.flush()
    await session.refresh(pin)


async def move_subscription(
    uow: UnitOfWork,
    authorizer: Authorizer,
    principal: Principal,
    card: SubscriberCard,
    subscription_id: uuid.UUID,
    ref: str,
) -> tuple[CardSubscription, bool]:
    """Replace an active pin by a pin of another version of the same contract (D-89).

    Returns ``(active pin, changed)``. The target obeys the same rules as a new pin (the acting
    principal's own read access, not deprecated, published). Moving to the pinned version is a
    no-op. Moving to a version the card already pins ends the old pin as ``moved`` with
    ``replaced_by_id`` set to the existing pin and adds no row (A6). One ``card.subscription_moved``
    event: before is the old pin, after is the pin now active.
    """
    session = uow.session
    pin = await _card_pin(session, card.id, subscription_id, lock=False)
    if pin.ended_at is not None:
        raise _ended()
    contract, version = await _resolve_pinnable_version(
        session, authorizer, principal, pin.contract_id, ref
    )
    locked = await _share_locked_card(session, card.id)
    if locked.archived_at is not None:
        raise archived_problem()
    old = await _card_pin(session, locked.id, subscription_id, lock=True)
    if old.ended_at is not None:
        raise _ended()
    if old.version_id == version.id:
        return old, False
    before = subscription_snapshot(old, await _semver_of(session, old.version_id))
    target = await active_pin(session, locked.id, version.id)
    if target is None:
        target = CardSubscription(
            card_id=locked.id, contract_id=contract.id, version_id=version.id, mode="pinned"
        )
        session.add(target)
        await session.flush()
        await session.refresh(target)
    await _end_pin(session, uow, old, "moved", replaced_by=target.id)
    uow.audit(
        "card.subscription_moved",
        "card",
        locked.id,
        before=before,
        after=subscription_snapshot(target, version.semver or ""),
        scope_path=locked.ancestor_ids,
    )
    return target, True


async def unsubscribe(
    uow: UnitOfWork, card: SubscriberCard, subscription_id: uuid.UUID, note: str | None
) -> tuple[CardSubscription, bool]:
    """End an active pin as ``unsubscribed`` (never a delete); an ended pin is a no-op."""
    session = uow.session
    await _card_pin(session, card.id, subscription_id, lock=False)
    locked = await _share_locked_card(session, card.id)
    if locked.archived_at is not None:
        raise archived_problem()
    row = await _card_pin(session, locked.id, subscription_id, lock=True)
    if row.ended_at is not None:
        return row, False
    semver = await _semver_of(session, row.version_id)
    before = subscription_snapshot(row, semver)
    await _end_pin(session, uow, row, "unsubscribed", note=note)
    uow.audit(
        "card.unsubscribed",
        "card",
        locked.id,
        before=before,
        after=subscription_snapshot(row, semver),
        scope_path=locked.ancestor_ids,
    )
    return row, True


async def remove_subscription(
    uow: UnitOfWork, contract_id: uuid.UUID, subscription_id: uuid.UUID, reason: str
) -> tuple[CardSubscription, bool]:
    """End a card's pin on this contract as ``removed_by_admin`` with the owner's reason (D-94).

    The caller already holds ``contract:publish`` at the contract's folder. Lock order is the
    lifecycle one: the contract row first, then the pin ``FOR UPDATE``. A pin of another contract
    or an unknown id is the unknown 404; an already ended pin is returned unchanged
    (``changed`` False: no event, no notice). The request writes no notice: the one
    removal event, scoped to the contract's folder path (the actor's scope),
    is the notice's carrier, and the card is told once that event is handled (D-99 as amended).
    """
    session = uow.session
    contract = await lock_contract(session, contract_id)
    row = (
        await session.execute(
            select(CardSubscription)
            .where(
                CardSubscription.id == subscription_id, CardSubscription.contract_id == contract_id
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found()
    if row.ended_at is not None:
        return row, False
    semver = await _semver_of(session, row.version_id)
    before = subscription_snapshot(row, semver)
    await _end_pin(session, uow, row, "removed_by_admin", note=reason)
    uow.audit(
        "card.subscription_removed",
        "card",
        row.card_id,
        before=before,
        after={**subscription_snapshot(row, semver), "reason": reason},
        scope_path=contract.ancestor_ids,
    )
    return row, True
