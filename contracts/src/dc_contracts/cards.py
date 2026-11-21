"""Subscriber cards: the domain service behind ``api/cards.py`` (SUB-01, SUB-04, D-82, D-104).

A card is owned by one folder of any kind (root, organization, project or team).
The route gates are principal-level
(``card:read`` / ``card:write``); this module then resolves what the caller may do with the card
(:func:`card_capability`) and the route answers one uniform 404 to everyone without a capability,
so nobody learns that a card exists (D-35, D-104).
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import structlog
from dc_auth.authorizer import (
    Authorizer,
    AuthorizerUnavailable,
    FolderFacts,
    GranteeAuthorizer,
    Outcome,
)
from dc_auth.principal import Principal
from dc_core.platform_settings import webhook_host_allowed
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.models import (
    UNIQUE_CARD_NAME_INDEX,
    CardMember,
    CardSubscription,
    CardWebhookSecret,
    SubscriberCard,
)
from dc_contracts.settings import ContractsSettings
from dc_contracts.webhooks import (
    InvalidWebhookUrl,
    SecretBox,
    check_resolved_host,
    generate_secret,
    validate_webhook_url,
)

_log = structlog.get_logger("dc_contracts.cards")

MAX_CONTACTS = 20


@dataclass(frozen=True)
class CardCapability:
    """What one caller may do with one card, from the folder decision (never from token claims)."""

    read: bool
    subscribe: bool
    manage: bool
    facts: FolderFacts | None


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def card_snapshot(card: SubscriberCard) -> dict[str, Any]:
    """Allow-listed, JSON-safe audit view of a card (``webhook_url`` is never in it)."""
    return UnitOfWork.snapshot(card)


async def card_has_member(
    session: AsyncSession, card_id: uuid.UUID, grantees: frozenset[tuple[str, str]]
) -> bool:
    """Is any of the caller's grantee identities linked to the card?"""
    if not grantees:
        return False
    found = await session.execute(
        select(
            select(CardMember.card_id)
            .where(
                CardMember.card_id == card_id,
                tuple_(CardMember.member_type, CardMember.member_id).in_(sorted(grantees)),
            )
            .exists()
        )
    )
    return bool(found.scalar_one())


Membership = Literal["unknown", "not_member", "member", "member_only", "unresolved"]


async def _membership_first(
    authorizer: GranteeAuthorizer,
    principal: Principal,
    card: SubscriberCard,
    session: AsyncSession,
) -> Membership:
    """Is the caller a linked member, and could the folder check still allow it (WR-02)?

    ``unknown`` when identity cannot list the caller's grantees (the folder check then decides,
    with the lookup's old failure rules); ``not_member``; ``member_only`` for a member whom
    ``accessible_folders(folder:read)`` excludes for certain (no scope over everything and the
    folder not among the exact ids: identity expands every grant below ROOT into ``folder_ids``),
    so the folder check would be NOT_FOUND; ``member`` when it might allow; ``unresolved`` when the
    scope cannot be read for a member (fails closed to the uniform 404, as before).
    ``accessible_folders`` never records a denial (trimming is not one, D-172).
    """
    try:
        grantees = await authorizer.grantees(principal)
    except AuthorizerUnavailable:
        return "unknown"
    if not await card_has_member(session, card.id, grantees):
        return "not_member"
    try:
        scope = await authorizer.accessible_folders(principal, "folder:read")
    except AuthorizerUnavailable as exc:
        _log.warning("card_access_unresolved", card_id=str(card.id), error=type(exc).__name__)
        return "unresolved"
    if not scope.all and card.folder_id not in scope.folder_ids:
        return "member_only"
    return "member"


async def card_capability(
    authorizer: Authorizer,
    principal: Principal,
    card: SubscriberCard,
    session: AsyncSession,
    *,
    fresh: bool = False,
    need: Literal["read", "subscribe", "manage"] = "manage",
) -> CardCapability:
    """Resolve the caller's capability on ``card``.

    The folder decision (``folder:read``) gives ``read`` for folder readers and ``manage`` for the
    creator or a holder of ``card:manage`` (A2: a creator who lost folder access is no longer a
    manager). Membership (D-85, D-104) adds ``read`` and ``subscribe`` for a principal in a linked
    group or a linked service account; it never adds ``manage``. ``need`` says which capability
    the caller is about to use: ``"manage"`` never asks about membership; ``"read"`` and
    ``"subscribe"`` ask first (below).

    The caller has not yet been shown the card, so an identity failure before it is known to see
    it (the folder decision, or the membership lookup for a caller the folder decision did not
    allow) fails closed to "no capability", the uniform 404, instead of 503: a 503 for a known id
    against a 404 for an unknown one would reveal which ids exist (D-35, D-104, D-62). Only a
    folder reader, who already sees the card, gets ``AuthorizerUnavailable`` from the lookup.

    A linked member who holds no folder role is answered without the folder check (WR-02): that
    check would be NOT_FOUND, and identity records every non-ALLOW check as a denial (D-172),
    so each successful member read would leave a false ``not_found`` denial in the trail. So for
    ``"read"`` and ``"subscribe"`` membership is asked first (:func:`_membership_first`), and the
    folder check is made only when membership does not already settle the answer. A caller who is
    not a member still gets the folder check, so a refused caller's denial is still recorded.
    """
    membership: Membership | None = None
    if need != "manage" and isinstance(authorizer, GranteeAuthorizer):
        membership = await _membership_first(authorizer, principal, card, session)
        if membership == "unresolved":
            return CardCapability(read=False, subscribe=False, manage=False, facts=None)
        if membership == "member_only":
            return CardCapability(read=True, subscribe=True, manage=False, facts=None)
    try:
        decision = await authorizer.check(principal, "folder:read", card.folder_id, fresh=fresh)
    except AuthorizerUnavailable as exc:
        _log.warning("card_access_unresolved", card_id=str(card.id), error=type(exc).__name__)
        return CardCapability(read=False, subscribe=False, manage=False, facts=None)
    folder_read = decision.outcome is Outcome.ALLOW
    manage = folder_read and (
        "card:manage" in decision.permissions or card.created_by == principal.id
    )
    is_member = membership == "member"
    lookup = (need == "read" and not folder_read) or (need == "subscribe" and not manage)
    if lookup and membership == "unknown" and isinstance(authorizer, GranteeAuthorizer):
        # The grantees could not be listed above: ask again, with the original failure rules.
        try:
            grantees = await authorizer.grantees(principal)
        except AuthorizerUnavailable as exc:
            if not folder_read:
                _log.warning(
                    "card_access_unresolved", card_id=str(card.id), error=type(exc).__name__
                )
                return CardCapability(read=False, subscribe=False, manage=False, facts=None)
            raise
        is_member = await card_has_member(session, card.id, grantees)
    return CardCapability(
        read=folder_read or is_member,
        subscribe=manage or is_member,
        manage=manage,
        facts=decision.folder,
    )


async def get_card(session: AsyncSession, card_id: uuid.UUID) -> SubscriberCard:
    card = (
        await session.execute(select(SubscriberCard).where(SubscriberCard.id == card_id))
    ).scalar_one_or_none()
    if card is None:
        raise _not_found()
    return card


def _name_taken() -> ProblemError:
    return ProblemError(
        409, "card-name-taken", "Conflict", "A card with this name already exists in the folder."
    )


def map_card_integrity_error(exc: IntegrityError) -> ProblemError | None:
    """409 ``card-name-taken`` for the partial unique index, keyed on its constraint name."""
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint == UNIQUE_CARD_NAME_INDEX:
        return _name_taken()
    return None


def archived_problem() -> ProblemError:
    return ProblemError(409, "card-archived", "Conflict", "The card is archived and cannot change.")


async def _locked_card(session: AsyncSession, card_id: uuid.UUID) -> SubscriberCard:
    """Select the card ``FOR UPDATE`` and refresh any copy already in the session."""
    card = (
        await session.execute(
            select(SubscriberCard)
            .where(SubscriberCard.id == card_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if card is None:
        raise _not_found()
    return card


async def _flush_card(session: AsyncSession, card: SubscriberCard) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        mapped = map_card_integrity_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise
    await session.refresh(card)


async def create_card(
    uow: UnitOfWork,
    facts: FolderFacts,
    *,
    name: str,
    description: str,
    contacts: list[dict[str, Any]],
    email_enabled: bool,
) -> SubscriberCard:
    """Insert a card owned by ``facts`` and record one ``card.created`` event (D-82, reversed).

    Any folder kind may own a card (quick 261119-wo3); a ROOT card has no Organization, so its
    ``organization_id`` is NULL. Archived folders are refused by the route before this runs.
    """
    session = uow.session
    card = SubscriberCard(
        folder_id=facts.id,
        ancestor_ids=list(facts.ancestor_ids),
        organization_id=facts.organization_id,
        name=name,
        description=description,
        contacts=contacts,
        email_enabled=email_enabled,
        webhook_enabled=False,
    )
    session.add(card)
    # No SAVEPOINT: the mapped 409 aborts the unit of work, which rolls back (see insert_contract).
    await _flush_card(session, card)
    uow.audit(
        "card.created",
        "card",
        card.id,
        before=None,
        after=card_snapshot(card),
        scope_path=card.ancestor_ids,
    )
    return card


_EDITABLE = ("name", "description", "contacts", "email_enabled")


def _invalid_webhook_url() -> ProblemError:
    return ProblemError(
        422, "invalid-webhook-url", "Unprocessable Content", "The webhook URL is not acceptable."
    )


def _webhook_unavailable() -> ProblemError:
    return ProblemError(
        503,
        "service-unavailable",
        "Service Unavailable",
        "Webhook delivery is not configured on this deployment.",
    )


def secret_box(settings: ContractsSettings) -> SecretBox:
    """The Fernet box, or 503 when the deployment has no ``webhook_secret_key`` (D-107)."""
    if settings.webhook_secret_key is None:
        raise _webhook_unavailable()
    return SecretBox(settings.webhook_secret_key)


async def has_webhook_secret(session: AsyncSession, card_id: uuid.UUID) -> bool:
    """Has the card ever been given a webhook secret? (Secret rows are never deleted.)"""
    found = await session.execute(
        select(
            select(CardWebhookSecret.card_id).where(CardWebhookSecret.card_id == card_id).exists()
        )
    )
    return bool(found.scalar_one())


async def _secret_row(
    session: AsyncSession, card_id: uuid.UUID, *, lock: bool = False
) -> CardWebhookSecret | None:
    query = (
        select(CardWebhookSecret)
        .where(CardWebhookSecret.card_id == card_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


def _webhook_audit(card: SubscriberCard, secret: CardWebhookSecret | None) -> dict[str, Any]:
    """Host and generation only: never the URL (it can carry tokens), the secret or its token."""
    if card.webhook_url is None:
        return {}
    host = urlsplit(card.webhook_url).hostname
    return {
        "webhook_host": host,
        "webhook_secret_generation": None if secret is None else secret.generation,
    }


@dataclass(frozen=True)
class WebhookHostPolicy:
    """The Global admin's allowed-hosts list as the PATCH route read it (quick 261120-0yd, D-25).

    The list is mandatory (quick 261120-s0p): ``hosts`` empty allows no host. ``known`` False means
    identity never answered or the one-time seed of the list is not confirmed yet, and then a URL
    cannot be judged (503 ``settings-unavailable``, fail closed).
    """

    hosts: tuple[str, ...]
    known: bool


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


async def _checked_webhook_url(
    url: str, settings: ContractsSettings, policy: WebhookHostPolicy | None = None
) -> None:
    """Validate a URL about to be stored; 422 ``invalid-webhook-url`` without echoing it.

    The allowed-hosts list is mandatory, checked after the URL shape and before DNS: every other
    rule (https, no userinfo, globally routable addresses) still applies to a listed host.
    """
    try:
        parsed = validate_webhook_url(url, settings)
        if _is_ip_literal(parsed.host):
            # The allow-list takes hostnames only, so an address can never be listed (and the
            # old empty list that let one through is gone): refuse it as an unacceptable URL.
            raise InvalidWebhookUrl("ip-literal")
        if policy is not None:
            if not policy.known:
                raise ProblemError(
                    503,
                    "settings-unavailable",
                    "Service Unavailable",
                    "The webhook host settings cannot be read right now; try again shortly.",
                )
            if not webhook_host_allowed(parsed.host, policy.hosts):
                raise ProblemError(
                    422,
                    "webhook-host-not-allowed",
                    "Unprocessable Content",
                    "This webhook host is not on the list of allowed hosts. "
                    "A Global admin adds hosts in Settings > Notifications.",
                )
        await check_resolved_host(parsed.host, parsed.port, settings)
    except InvalidWebhookUrl as exc:
        _log.info("webhook_url_rejected", reason=exc.code)
        raise _invalid_webhook_url() from None


async def update_card(
    uow: UnitOfWork,
    card_id: uuid.UUID,
    changes: dict[str, Any],
    *,
    settings: ContractsSettings,
    host_policy: WebhookHostPolicy | None = None,
) -> tuple[SubscriberCard, bool, str | None]:
    """Apply the fields of ``changes`` that differ; one ``card.updated`` event, or none.

    Returns ``(card, changed, new_secret)``. ``new_secret`` is the plaintext ``whsec_`` secret
    when this call created the card's first one (enabling the webhook channel); it is the only
    time the plaintext exists outside the encrypted column, and the caller returns it once. An
    archived card refuses every change (409 ``card-archived``); a request that changes nothing is
    a no-op without an event. The first-secret interactive check (D-79) is the route's job.
    """
    session = uow.session
    new_url = changes.get("webhook_url")
    url_checked = False
    if new_url is not None:
        # The DNS check needs no lock: do it before the card row is locked so a slow resolver
        # cannot queue other writers behind this one. Unknown, archived and unchanged cards fall
        # through to the locked path below, which keeps its error precedence.
        peek = (
            await session.execute(
                select(SubscriberCard.webhook_url, SubscriberCard.archived_at).where(
                    SubscriberCard.id == card_id
                )
            )
        ).one_or_none()
        if peek is not None and peek.archived_at is None and peek.webhook_url != new_url:
            await _checked_webhook_url(new_url, settings, host_policy)
            url_checked = True
    card = await _locked_card(session, card_id)
    if card.archived_at is not None:
        raise archived_problem()
    secret_row = await _secret_row(session, card.id)
    before = {**card_snapshot(card), **_webhook_audit(card, secret_row)}
    changed = False
    for field in _EDITABLE:
        if field in changes and getattr(card, field) != changes[field]:
            setattr(card, field, changes[field])
            changed = True

    if new_url is not None and new_url != card.webhook_url:
        if not url_checked:  # the card's URL changed between the peek and the lock
            await _checked_webhook_url(new_url, settings, host_policy)
        card.webhook_url = new_url
        changed = True
    enable = changes.get("webhook_enabled") is True and not card.webhook_enabled
    if "webhook_enabled" in changes and changes["webhook_enabled"] != card.webhook_enabled:
        if changes["webhook_enabled"] and card.webhook_url is None:
            raise _invalid_webhook_url()
        card.webhook_enabled = changes["webhook_enabled"]
        changed = True
    elif changes.get("webhook_enabled") is True and card.webhook_url is None:
        raise _invalid_webhook_url()

    new_secret: str | None = None
    if enable:
        box = secret_box(settings)
        if secret_row is None:
            new_secret = generate_secret()
            secret_row = CardWebhookSecret(
                card_id=card.id, secret_enc=box.seal(new_secret), generation=1
            )
            session.add(secret_row)

    if not changed:
        return card, False, None
    await _flush_card(session, card)
    uow.audit(
        "card.updated",
        "card",
        card.id,
        before=before,
        after={**card_snapshot(card), **_webhook_audit(card, secret_row)},
        scope_path=card.ancestor_ids,
    )
    return card, True, new_secret


async def rotate_webhook_secret(
    uow: UnitOfWork, card: SubscriberCard, box: SecretBox, overlap_s: int
) -> tuple[str, CardWebhookSecret]:
    """Issue the card's next signing secret; the previous one stays valid for ``overlap_s`` seconds.

    Returns the new plaintext (shown once by the caller) and the secret row. The card is locked
    first, then the secret row, so two rotations serialise and never share a generation. Only one
    previous secret is kept. A card whose webhook was never given a secret gets generation 1 with
    no previous one. One ``card.webhook_secret_rotated`` event carries the generation numbers only.
    """
    session = uow.session
    locked = await _locked_card(session, card.id)
    if locked.archived_at is not None:
        raise archived_problem()
    if locked.webhook_url is None:
        raise ProblemError(
            409,
            "webhook-not-configured",
            "Conflict",
            "Set a webhook URL before creating a secret.",
        )
    plain = generate_secret()
    row = await _secret_row(session, locked.id, lock=True)
    now = datetime.now(UTC)
    if row is None:
        old_generation: int | None = None
        row = CardWebhookSecret(card_id=locked.id, secret_enc=box.seal(plain), generation=1)
        session.add(row)
    else:
        old_generation = row.generation
        row.previous_enc = row.secret_enc
        row.previous_valid_until = now + timedelta(seconds=overlap_s)
        row.secret_enc = box.seal(plain)
        row.generation = old_generation + 1
        row.updated_at = now
    await session.flush()
    await session.refresh(row)
    uow.audit(
        "card.webhook_secret_rotated",
        "card",
        locked.id,
        before={"generation": old_generation},
        after={"generation": row.generation},
        scope_path=locked.ancestor_ids,
    )
    return plain, row


async def archive_card(uow: UnitOfWork, card_id: uuid.UUID) -> tuple[SubscriberCard, bool]:
    """Archive a card (never deleted); archiving an archived card is a no-op without an event."""
    session = uow.session
    card = await _locked_card(session, card_id)
    if card.archived_at is not None:
        return card, False
    before = card_snapshot(card)
    card.archived_at = func.now()
    card.archived_by = uow.actor.id
    await _flush_card(session, card)
    # Archiving ends every active pin in the same unit (D-93). The bulk update bypasses the ORM
    # stamping hook, so the audit columns are written here (research Pitfall 4). The card is held
    # FOR UPDATE, so a concurrent subscribe waits for this commit and then finds the card archived.
    ended = await session.execute(
        update(CardSubscription)
        .where(CardSubscription.card_id == card.id, CardSubscription.ended_at.is_(None))
        .values(
            ended_at=func.now(),
            ended_by=uow.actor.id,
            end_reason="card_archived",
            updated_by=uow.actor.id,
            updated_at=func.now(),
        )
        .returning(CardSubscription.id)
        .execution_options(synchronize_session=False)
    )
    ended_ids = sorted(str(sub_id) for sub_id in ended.scalars())
    uow.audit(
        "card.archived",
        "card",
        card.id,
        before=before,
        after={**card_snapshot(card), "ended_subscription_ids": ended_ids},
        scope_path=card.ancestor_ids,
    )
    return card, True


async def list_cards(
    session: AsyncSession,
    folder_id: uuid.UUID,
    *,
    include_archived: bool,
    cursor: uuid.UUID | None,
    limit: int,
) -> tuple[list[SubscriberCard], uuid.UUID | None]:
    """One page of a folder's cards in uuidv7 id order; the cursor is the last id returned."""
    query = select(SubscriberCard).where(SubscriberCard.folder_id == folder_id)
    if not include_archived:
        query = query.where(SubscriberCard.archived_at.is_(None))
    if cursor is not None:
        query = query.where(SubscriberCard.id > cursor)
    rows = list(
        (await session.execute(query.order_by(SubscriberCard.id).limit(limit + 1))).scalars()
    )
    if len(rows) > limit:
        page = rows[:limit]
        return page, page[-1].id
    return rows, None


async def list_members(session: AsyncSession, card_id: uuid.UUID) -> list[CardMember]:
    """The card's linked members ordered by type, then id (SUB-04)."""
    rows = await session.execute(
        select(CardMember)
        .where(CardMember.card_id == card_id)
        .order_by(CardMember.member_type, CardMember.member_id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


def _member_key(card_id: uuid.UUID, member_type: str, member_id: str) -> tuple[Any, ...]:
    return (CardMember.card_id == card_id, CardMember.member_type == member_type) + (
        CardMember.member_id == member_id,
    )


async def add_member(
    uow: UnitOfWork, card: SubscriberCard, member_type: str, member_id: str
) -> tuple[CardMember, bool]:
    """Link a member; returns ``(row, created)``. Linking a linked member is a no-op, no event.

    The card is locked ``FOR UPDATE`` first so an archive cannot slip in between the check and the
    insert; the insert itself is ``ON CONFLICT DO NOTHING`` so two concurrent links of one member
    leave one row and one ``card.member_added`` event. Core inserts bypass the ``before_flush``
    stamping, so the audit columns are written explicitly (SUB-02).
    """
    session = uow.session
    locked = await _locked_card(session, card.id)
    if locked.archived_at is not None:
        raise archived_problem()
    inserted = await session.execute(
        insert(CardMember)
        .values(
            card_id=locked.id,
            member_type=member_type,
            member_id=member_id,
            created_by=uow.actor.id,
            updated_by=uow.actor.id,
        )
        .on_conflict_do_nothing(index_elements=["card_id", "member_type", "member_id"])
        .returning(CardMember.member_id)
    )
    created = inserted.scalar_one_or_none() is not None
    row = (
        await session.execute(
            select(CardMember)
            .where(*_member_key(locked.id, member_type, member_id))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if created:
        uow.audit(
            "card.member_added",
            "card",
            locked.id,
            before=None,
            after={"member_type": member_type, "member_id": member_id},
            scope_path=locked.ancestor_ids,
        )
    return row, created


async def remove_member(
    uow: UnitOfWork, card: SubscriberCard, member_type: str, member_id: str
) -> bool:
    """Unlink a member; returns whether a link was removed. A missing link is a no-op, no event."""
    session = uow.session
    locked = await _locked_card(session, card.id)
    if locked.archived_at is not None:
        raise archived_problem()
    removed = await session.execute(
        delete(CardMember)
        .where(*_member_key(locked.id, member_type, member_id))
        .returning(CardMember.member_id)
    )
    if removed.scalar_one_or_none() is None:
        return False
    uow.audit(
        "card.member_removed",
        "card",
        locked.id,
        before={"member_type": member_type, "member_id": member_id},
        after=None,
        scope_path=locked.ancestor_ids,
    )
    return True
