"""What a folder still holds in contracts, and the permanent removal of its leftovers.

Quick 261119-0v1 (deleting a folder, identity orchestrates). ``folder_usage`` answers two kinds
of counts for one folder:

* **Blockers** (live things): contracts that are not soft-deleted, in any lifecycle state drafts
  included; every template (templates have no soft delete); subscriber cards that are not archived.
* **Purges** (leftovers): contracts already soft-deleted, and archived subscriber cards.

``purge_folder`` permanently removes the leftovers, and only when no blocker is left (409
``folder-not-empty`` otherwise, so a direct call can never purge next to live contracts). It is
idempotent: a second call finds nothing and removes nothing.

What is deleted, in dependency order, all inside one transaction with the delete-guard bypass
``dc.folder_purge`` switched on for that transaction only (migration 0017; the triggers still
refuse the delete unless the parent contract is soft-deleted or the card is archived and the pin
has ended, so a live contract's immutable rows are never reachable):

1. the Latest pointer of each purged contract is cleared (it is a composite foreign key into the
   versions about to go);
2. ``contract_reference``, ``contract_ruleset_attachment`` rows of the purged contracts;
3. ended ``card_subscription`` rows (history) of the purged contracts and of the purged cards,
   from any card or contract: a pin of another card on a deleted contract has ended already, since
   a delete is refused while a pin is active;
4. the notices, deliveries, webhook secret and members of the purged cards;
5. every ``contract_version`` of the purged contracts (drafts, published, unpublished);
6. ``origin_contract_id``/``origin_version_id`` of any contract that was copied from a purged
   contract are cleared (provenance only; the copy lives on, the provenance link is lost);
7. the contracts and the cards themselves.

``validation_run`` rows in the validation service and audit events are history and stay; notices
other cards received about a purged contract stay too (no foreign key).
The write is one counts-only audit event (``folder.purged``), no document content.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import CursorResult, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.models import (
    CardMember,
    CardNotice,
    CardNoticeDelivery,
    CardSubscription,
    CardWebhookSecret,
    Contract,
    ContractReference,
    ContractRuleSetAttachment,
    ContractVersion,
    SubscriberCard,
    Template,
)

PURGE_SETTING = "dc.folder_purge"


async def _affected(session: AsyncSession, stmt: Any) -> int:
    """Rows a DML statement touched."""
    result = cast("CursorResult[Any]", await session.execute(stmt))
    return int(result.rowcount)


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int((await session.execute(stmt)).scalar_one())


async def folder_usage(session: AsyncSession, folder_id: uuid.UUID) -> dict[str, int]:
    """Blocker and purge counts for ``folder_id`` (counts only; no visibility trimming)."""
    contracts = select(func.count()).select_from(Contract).where(Contract.folder_id == folder_id)
    cards = (
        select(func.count())
        .select_from(SubscriberCard)
        .where(SubscriberCard.folder_id == folder_id)
    )
    return {
        "contracts": await _count(session, contracts.where(Contract.deleted_at.is_(None))),
        "deleted_contracts": await _count(
            session, contracts.where(Contract.deleted_at.is_not(None))
        ),
        "templates": await _count(
            session,
            select(func.count()).select_from(Template).where(Template.folder_id == folder_id),
        ),
        "subscriber_cards": await _count(
            session, cards.where(SubscriberCard.archived_at.is_(None))
        ),
        "archived_subscriber_cards": await _count(
            session, cards.where(SubscriberCard.archived_at.is_not(None))
        ),
    }


def _not_empty(usage: dict[str, int]) -> ProblemError:
    blockers = [
        {"kind": kind, "count": usage[kind]}
        for kind in ("contracts", "templates", "subscriber_cards")
        if usage[kind] > 0
    ]
    return ProblemError(
        409,
        "folder-not-empty",
        "Conflict",
        "The folder is not empty. Move or delete what it contains first.",
        blockers=blockers,
    )


async def purge_folder(
    uow: UnitOfWork, folder_id: uuid.UUID, scope_path: tuple[uuid.UUID, ...]
) -> dict[str, int]:
    """Permanently remove the soft-deleted contracts and archived cards of one folder.

    Returns the removed counts and records one ``folder.purged`` event (counts only), even when
    nothing was there to remove (the call itself is the audited act). Locks the folder's contract
    and card rows ``FOR UPDATE`` first, so a restore or an archive undo cannot interleave.
    """
    session = uow.session
    contract_ids = (
        (
            await session.execute(
                select(Contract.id)
                .where(Contract.folder_id == folder_id)
                .order_by(Contract.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    card_ids = (
        (
            await session.execute(
                select(SubscriberCard.id)
                .where(SubscriberCard.folder_id == folder_id)
                .order_by(SubscriberCard.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    usage = await folder_usage(session, folder_id)
    if usage["contracts"] or usage["templates"] or usage["subscriber_cards"]:
        raise _not_empty(usage)

    # Only deleted contracts and archived cards are left in the folder now.
    versions = 0
    subscriptions = 0
    origin_cleared = 0
    if contract_ids or card_ids:
        # Transaction-local: the guard triggers read it, and it dies with this transaction.
        await session.execute(text("SELECT set_config(:name, 'on', true)"), {"name": PURGE_SETTING})
        active_pin = await _count(
            session,
            select(func.count())
            .select_from(CardSubscription)
            .where(
                CardSubscription.ended_at.is_(None),
                (CardSubscription.contract_id.in_(contract_ids))
                | (CardSubscription.card_id.in_(card_ids)),
            ),
        )
        if active_pin:  # unreachable by the delete and archive guards; refuse rather than guess
            raise ProblemError(
                409, "conflict", "Conflict", "A deleted contract or archived card is still pinned."
            )
        await session.execute(
            update(Contract)
            .where(Contract.id.in_(contract_ids))
            .values(latest_version_id=None, origin_contract_id=None, origin_version_id=None)
        )
        await session.execute(
            delete(ContractReference).where(ContractReference.source_contract_id.in_(contract_ids))
        )
        await session.execute(
            delete(ContractRuleSetAttachment).where(
                ContractRuleSetAttachment.contract_id.in_(contract_ids)
            )
        )
        subscriptions = await _affected(
            session,
            delete(CardSubscription).where(
                CardSubscription.contract_id.in_(contract_ids)
                | CardSubscription.card_id.in_(card_ids)
            ),
        )
        notice_ids = select(CardNotice.id).where(CardNotice.card_id.in_(card_ids))
        await session.execute(
            delete(CardNoticeDelivery).where(CardNoticeDelivery.notice_id.in_(notice_ids))
        )
        await session.execute(delete(CardNotice).where(CardNotice.card_id.in_(card_ids)))
        await session.execute(
            delete(CardWebhookSecret).where(CardWebhookSecret.card_id.in_(card_ids))
        )
        await session.execute(delete(CardMember).where(CardMember.card_id.in_(card_ids)))
        versions = await _affected(
            session,
            delete(ContractVersion).where(ContractVersion.contract_id.in_(contract_ids)),
        )
        # Copies elsewhere keep their own rows; only the provenance link to a purged source goes.
        origin_cleared = await _affected(
            session,
            update(Contract)
            .where(Contract.origin_contract_id.in_(contract_ids))
            .values(origin_contract_id=None, origin_version_id=None),
        )
        await session.execute(delete(Contract).where(Contract.id.in_(contract_ids)))
        await session.execute(delete(SubscriberCard).where(SubscriberCard.id.in_(card_ids)))

    counts = {
        "deleted_contracts": len(contract_ids),
        "contract_versions": versions,
        "archived_subscriber_cards": len(card_ids),
        "card_subscriptions": subscriptions,
        "origin_cleared": origin_cleared,
    }
    uow.audit(
        "folder.purged",
        "folder",
        folder_id,
        before=None,
        after=counts,
        scope_path=scope_path,
    )
    await uow.commit()
    return counts
