"""Subscriber card routes: ``/api/v1/contracts`` (SUB-01, SUB-04, D-82, D-97, D-104).

* ``POST /folders/{folder_id}/cards`` creates a card owned by a folder of any kind; gated by
  ``card:create`` at that folder.
* ``POST /cards/{card_id}/subscriptions`` pins a published version to the card (SUB-03); the acting
  principal's own read access to that version decides, never the card (D-86, D-88).
* ``POST .../subscriptions/{id}/move`` and ``.../unsubscribe`` end a pin, never delete it (D-84);
  ``GET /cards/{card_id}/subscriptions`` pages the complete record (the card read embeds at most
  500 rows, A13).
* ``GET /cards/{card_id}`` is gated by the principal-level ``card:read``; the handler then resolves
  the caller's capability on the card. Everyone who cannot read the owning folder gets the same 404
  as for an unknown id, also while identity is failing (D-35, D-104, D-62).

A full identity outage is 503 for every card id, because the principal-level ``require()`` check
asks identity before the card is loaded. A failure of the folder decision after the card is loaded
fails closed to the uniform 404 (see ``card_capability``).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from dc_auth.authorizer import AuthorizerUnavailable, GranteeAuthorizer, Outcome
from dc_auth.deps import UnitOfWorkDep, authenticate, current_decision, parse_bearer, require
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.names import NameRule
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Query, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.api.contracts import _path_folder_id, require_writable_folder
from dc_contracts.card_index import (
    PIN_PREVIEW_LIMIT,
    CardIndexFilters,
    CardIndexScopes,
    list_visible_cards,
)
from dc_contracts.cards import (
    MAX_CONTACTS,
    CardCapability,
    WebhookHostPolicy,
    add_member,
    archive_card,
    archived_problem,
    card_capability,
    create_card,
    get_card,
    has_webhook_secret,
    list_cards,
    list_members,
    remove_member,
    rotate_webhook_secret,
    secret_box,
    update_card,
)
from dc_contracts.models import CardMember, CardSubscription, SubscriberCard
from dc_contracts.notices import list_notices
from dc_contracts.pins import (
    SUBSCRIPTIONS_EMBED_LIMIT,
    card_subscriptions,
    pin_with_semver,
)
from dc_contracts.runtime_settings import effective_notifications
from dc_contracts.search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_Q_LENGTH,
    decode_cursor,
    encode_cursor,
    reject_nul,
)
from dc_contracts.subscriptions import move_subscription, subscribe, unsubscribe

router = PolicyRouter(prefix="/api/v1/contracts")


# Errors carry fixed messages and never interpolate the submitted value (D-97, T-04.1-03).
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}")
_EMAIL_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_MAX_EMAIL = 320
_MAX_URL = 2048
_MAX_TEXT = 500
_MAX_CONTACT_NAME = 100
_MAX_VERSION_REF = 128


def _has_control(value: str, *, allow: str = "") -> bool:
    """True for any C0 control character or DEL, except those listed in ``allow``."""
    return any((ord(c) < 0x20 or ord(c) == 0x7F) and c not in allow for c in value)


def _valid_email(value: str) -> bool:
    if len(value) > _MAX_EMAIL or value.count("@") != 1:
        return False
    local, domain = value.split("@")
    if not _EMAIL_LOCAL.fullmatch(local) or local.startswith(".") or local.endswith("."):
        return False
    if ".." in local:
        return False
    labels = domain.split(".")
    return len(labels) >= 2 and all(_EMAIL_LABEL.fullmatch(label) for label in labels)


def _valid_url(value: str) -> bool:
    if len(value) > _MAX_URL or any(c.isspace() for c in value):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(host)


def _checked_text(value: str, *, allow: str = "") -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    if _has_control(value, allow=allow):
        raise ValueError("must not contain control characters")
    return value


class CardContactIn(BaseModel):
    """One contact of a card: kept exactly as submitted."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(min_length=1, max_length=_MAX_CONTACT_NAME)] = None
    kind: Literal["email", "url", "text"]
    value: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is not None and _has_control(value):
            raise ValueError("must not contain control characters")
        return value

    @field_validator("value")
    @classmethod
    def _value(cls, value: str, info: ValidationInfo) -> str:
        if _has_control(value):
            raise ValueError("must not contain control characters")
        kind = info.data.get("kind")
        if kind == "email" and not _valid_email(value):
            raise ValueError("is not a valid email address")
        if kind == "url" and not _valid_url(value):
            raise ValueError("is not a valid http or https URL")
        if kind == "text" and (not value.strip() or len(value) > _MAX_TEXT):
            raise ValueError(f"must be 1 to {_MAX_TEXT} characters")
        return value


class CardContactOut(BaseModel):
    name: str | None
    kind: Literal["email", "url", "text"]
    value: str


CardName = Annotated[str, StringConstraints(min_length=1, max_length=200), NameRule]
"""A card name: the one name rule of ``dc_core.names`` (quick 261120-lbz, D-01); trimmed."""


class SubscriberCardCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: CardName
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    contacts: list[CardContactIn] = Field(default_factory=list, max_length=MAX_CONTACTS)
    email_enabled: bool = False

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return _checked_text(value, allow="\t\n\r")


class SubscriberCardPatchIn(BaseModel):
    """Only the fields present change; an explicit null is refused."""

    model_config = ConfigDict(extra="forbid")

    name: CardName | None = None
    description: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    contacts: list[CardContactIn] | None = Field(default=None, max_length=MAX_CONTACTS)
    email_enabled: bool | None = None
    # The URL is validated by the service (422 ``invalid-webhook-url``, never echoed), not here.
    webhook_url: str | None = None
    webhook_enabled: bool | None = None

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return None if value is None else _checked_text(value, allow="\t\n\r")

    @model_validator(mode="after")
    def _no_nulls(self) -> SubscriberCardPatchIn:
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("fields that are present must not be null")
        return self

    def changes(self) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        for field in self.model_fields_set:
            value = getattr(self, field)
            changes[field] = _contacts_json(value) if field == "contacts" else value
        return changes


class SubscriberCardSummaryOut(BaseModel):
    id: uuid.UUID
    folder_id: uuid.UUID
    name: str
    description: str
    contacts: list[CardContactOut]
    email_enabled: bool
    webhook_enabled: bool
    archived_at: datetime | None
    created_at: datetime


class SubscriberCardListOut(BaseModel):
    items: list[SubscriberCardSummaryOut]
    next_cursor: str | None


class CardIndexPinOut(BaseModel):
    """One active pin shown inline on the all-cards list."""

    contract_id: uuid.UUID
    version_id: uuid.UUID
    semver: str
    # Null when the caller may not list the contract (it lives in a folder they cannot see).
    contract_name: str | None


class SubscriberCardIndexItemOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    folder_id: uuid.UUID
    # Root-first and including ``folder_id``: the UI names the path from the folders it can read.
    folder_ancestor_ids: list[uuid.UUID]
    # Null for a card owned by the ROOT folder.
    organization_id: uuid.UUID | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    active_subscription_count: int
    # The newest active pins, at most ``PIN_PREVIEW_LIMIT``; ``active_subscription_count`` is all.
    subscriptions: list[CardIndexPinOut]


class SubscriberCardIndexOut(BaseModel):
    """A page of every card the caller may read; totals count only those cards."""

    items: list[SubscriberCardIndexItemOut]
    next_cursor: str | None
    total_cards: int
    total_folders: int
    pin_preview_limit: int


class CardMemberIn(BaseModel):
    """Link a group or service account to a card; identity decides whether it may be linked."""

    model_config = ConfigDict(extra="forbid")

    member_type: Literal["local_group", "entra_group", "service_account"]
    member_id: Annotated[str, Field(min_length=1, max_length=200)]


class CardMemberOut(BaseModel):
    member_type: Literal["local_group", "entra_group", "service_account"]
    member_id: str
    added_by: str
    added_at: datetime


class CardSubscribeIn(BaseModel):
    """Pin one version of one contract to a card: a concrete SemVer, never ``latest``."""

    model_config = ConfigDict(extra="forbid")

    contract_id: uuid.UUID
    version: Annotated[str, Field(max_length=_MAX_VERSION_REF)]
    mode: Literal["pinned"] = "pinned"


class CardMoveIn(BaseModel):
    """Move an active pin to another version of the same contract."""

    model_config = ConfigDict(extra="forbid")

    version: Annotated[str, Field(max_length=_MAX_VERSION_REF)]


class CardUnsubscribeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Annotated[str, Field(max_length=_MAX_TEXT)] | None = None

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return None if value is None else _checked_text(value, allow="\t\n\r")


class CardSubscriptionOut(BaseModel):
    id: uuid.UUID
    card_id: uuid.UUID
    contract_id: uuid.UUID
    version_id: uuid.UUID
    semver: str
    mode: Literal["pinned"]
    pinned_by: str
    pinned_at: datetime
    ended_by: str | None
    ended_at: datetime | None
    end_reason: Literal["moved", "unsubscribed", "removed_by_admin", "card_archived"] | None
    end_note: str | None
    replaced_by_id: uuid.UUID | None


class CardSubscriptionListOut(BaseModel):
    items: list[CardSubscriptionOut]
    next_cursor: str | None


class CardNoticeDeliveryOut(BaseModel):
    channel: Literal["email", "webhook"]
    status: Literal["pending", "delivered", "failed", "skipped"]
    attempts: int
    last_attempt_at: datetime | None
    last_status: int | None
    delivered_at: datetime | None
    last_error: str | None


class CardNoticeOut(BaseModel):
    id: uuid.UUID
    event_type: str
    contract_id: uuid.UUID
    version_id: uuid.UUID | None
    occurred_at: datetime
    created_at: datetime
    payload: dict[str, Any]
    deliveries: list[CardNoticeDeliveryOut]


class CardNoticeListOut(BaseModel):
    items: list[CardNoticeOut]
    next_cursor: str | None


class SubscriberCardOut(BaseModel):
    id: uuid.UUID
    folder_id: uuid.UUID
    name: str
    description: str
    contacts: list[CardContactOut]
    email_enabled: bool
    webhook_enabled: bool
    # The full URL for managers; scheme and host only for every other reader (T-04.1-56).
    webhook_url: str | None
    # Whether a signing secret exists. The secret itself is returned once, by the call that
    # creates or rotates it, and never again (D-98, D-107).
    webhook_secret_configured: bool
    archived_at: datetime | None
    archived_by: str | None
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    members: list[CardMemberOut]
    subscriptions: list[CardSubscriptionOut]
    subscriptions_truncated: bool


class SubscriberCardPatchOut(SubscriberCardOut):
    """The PATCH answer: ``webhook_secret`` is set only by the call that created the secret."""

    webhook_secret: str | None = None


class CardWebhookSecretOut(BaseModel):
    """The rotated secret, shown once, and when the previous one stops being accepted."""

    webhook_secret: str
    generation: int
    previous_valid_until: datetime | None


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def _public_webhook_url(url: str | None) -> str | None:
    """Scheme and host only: provider URLs can carry tokens in the path or query."""
    if url is None:
        return None
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}"


def _member_out(member: CardMember) -> CardMemberOut:
    return CardMemberOut(
        member_type=member.member_type,
        member_id=member.member_id,
        added_by=member.created_by,
        added_at=member.created_at,
    )


def _unavailable(exc: AuthorizerUnavailable) -> ProblemError:
    return ProblemError(
        503,
        "service-unavailable",
        "Service Unavailable",
        "Authorization is temporarily unavailable.",
    )


def _subscription_out(sub: CardSubscription, semver: str) -> CardSubscriptionOut:
    return CardSubscriptionOut(
        id=sub.id,
        card_id=sub.card_id,
        contract_id=sub.contract_id,
        version_id=sub.version_id,
        semver=semver,
        mode="pinned",
        pinned_by=sub.created_by,
        pinned_at=sub.created_at,
        ended_by=sub.ended_by,
        ended_at=sub.ended_at,
        end_reason=sub.end_reason,
        end_note=sub.end_note,
        replaced_by_id=sub.replaced_by_id,
    )


async def _card_out(
    session: AsyncSession, card: SubscriberCard, *, show_url: bool
) -> SubscriberCardOut:
    members = [_member_out(member) for member in await list_members(session, card.id)]
    # One statement, one snapshot, newest first; one extra row says whether the record is cut.
    pins = await card_subscriptions(
        session, card.id, state="all", cursor=None, limit=SUBSCRIPTIONS_EMBED_LIMIT + 1
    )
    truncated = len(pins) > SUBSCRIPTIONS_EMBED_LIMIT
    return SubscriberCardOut(
        id=card.id,
        folder_id=card.folder_id,
        name=card.name,
        description=card.description,
        contacts=_contacts_out(card),
        email_enabled=card.email_enabled,
        webhook_enabled=card.webhook_enabled,
        webhook_url=card.webhook_url if show_url else _public_webhook_url(card.webhook_url),
        webhook_secret_configured=await has_webhook_secret(session, card.id),
        archived_at=card.archived_at,
        archived_by=card.archived_by,
        created_by=card.created_by,
        created_at=card.created_at,
        updated_by=card.updated_by,
        updated_at=card.updated_at,
        members=members,
        subscriptions=[
            _subscription_out(sub, semver) for sub, semver in pins[:SUBSCRIPTIONS_EMBED_LIMIT]
        ],
        subscriptions_truncated=truncated,
    )


def _contacts_out(card: SubscriberCard) -> list[CardContactOut]:
    return [
        CardContactOut(name=contact.get("name"), kind=contact["kind"], value=contact["value"])
        for contact in card.contacts
    ]


def _contacts_json(contacts: list[CardContactIn]) -> list[dict[str, Any]]:
    return [contact.model_dump() for contact in contacts]


@router.post(
    "/folders/{folder_id}/cards",
    status_code=201,
    response_model=SubscriberCardOut,
    dependencies=[require("card:create", target=_path_folder_id)],
    responses={
        403: {"description": "The caller can read the folder but may not create cards in it"},
        404: {"description": "Folder not found, or not visible to the caller"},
        409: {"description": "Folder is archived, or the card name is taken in the folder"},
        422: {"description": "Invalid body"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def create_card_route(
    folder_id: uuid.UUID,
    body: SubscriberCardCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
) -> SubscriberCardOut:
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)
    card = await create_card(
        uow,
        facts,
        name=body.name,
        description=body.description,
        contacts=_contacts_json(body.contacts),
        email_enabled=body.email_enabled,
    )
    # Commit before answering: the client only sees 201 once the row and its event are durable.
    await uow.commit()
    response.headers["Location"] = f"/api/v1/contracts/cards/{card.id}"
    return await _card_out(uow.session, card, show_url=True)


async def _readable_card_with_capability(
    request: Request, principal: Principal, session: AsyncSession, card_id: uuid.UUID
) -> tuple[SubscriberCard, CardCapability]:
    """The card and the caller's capability, once the caller is known to read it."""
    card = await get_card(session, card_id)
    try:
        capability = await card_capability(
            request.app.state.authorizer_factory(request), principal, card, session, need="read"
        )
    except AuthorizerUnavailable as exc:  # pragma: no cover - a folder reader's lookup is skipped
        raise _unavailable(exc) from exc
    if not capability.read:
        raise _not_found()
    return card, capability


async def _readable_card(
    request: Request, principal: Principal, session: AsyncSession, card_id: uuid.UUID
) -> SubscriberCard:
    """The card, once the caller is known to read it; everyone else gets the uniform 404."""
    card, _ = await _readable_card_with_capability(request, principal, session, card_id)
    return card


@router.get(
    "/cards/{card_id}",
    response_model=SubscriberCardOut,
    dependencies=[require("card:read")],
    responses={
        404: {"description": "Card not found, or not visible to the caller (one response)"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def get_card_route(
    card_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> SubscriberCardOut:
    card, capability = await _readable_card_with_capability(request, principal, session, card_id)
    return await _card_out(session, card, show_url=capability.manage)


async def _managed_card(
    request: Request, principal: Principal, uow: UnitOfWorkDep, card_id: uuid.UUID
) -> SubscriberCard:
    """The card, once the caller is known to manage it (creator or folder admin, D-82).

    Not readable -> the uniform 404; readable but not a manager -> 403 (D-08); then the archived
    folder rule. The decision is taken fresh: a write must not rest on a cached grant. A linked
    member can read the card, so a member who is not a manager gets 403, not 404 (D-85): the
    capability is resolved with ``need="read"`` for that reason, and membership never adds
    ``manage``.
    """
    card = await get_card(uow.session, card_id)
    authorizer = request.app.state.authorizer_factory(request)
    capability = await card_capability(
        authorizer,
        principal,
        card,
        uow.session,
        fresh=True,
        need="read",
    )
    if not capability.read:
        raise _not_found()
    if not capability.manage or capability.facts is None:
        if capability.facts is None:
            # A linked member answered by membership alone (WR-02): the write is refused, so
            # make the folder check now and let identity record the denial (D-172). The member
            # already sees the card, so an unrecordable denial may be 503 (no existence oracle).
            try:
                await authorizer.check(principal, "folder:read", card.folder_id, fresh=True)
            except AuthorizerUnavailable as exc:
                raise _unavailable(exc) from exc
        raise ProblemError(
            403,
            "forbidden",
            "Forbidden",
            "Only the card's creator or the folder's admins may change it.",
        )
    require_writable_folder(capability.facts)
    return card


@router.patch(
    "/cards/{card_id}",
    response_model=SubscriberCardPatchOut,
    dependencies=[require("card:write")],
    responses={
        403: {
            "description": (
                "The caller can read the card but is neither its creator nor an admin (forbidden), "
                "or the call would issue the first webhook secret and the caller did not sign in "
                "interactively (interactive-session-required)"
            )
        },
        404: {"description": "Card not found, or not visible to the caller (one response)"},
        409: {"description": "Card is archived, folder is archived, or the name is taken"},
        422: {
            "description": (
                "Invalid body, the webhook URL is not acceptable, or its host is not on the "
                "Global admin's list of allowed webhook hosts, which is mandatory: an empty list "
                "allows no host (webhook-host-not-allowed)"
            )
        },
        503: {
            "description": (
                "Authorization is temporarily unavailable, webhook delivery is not configured "
                "on this deployment, or the allowed webhook hosts cannot be read or are not "
                "seeded yet (settings-unavailable)"
            )
        },
    },
)
async def update_card_route(
    card_id: uuid.UUID,
    body: SubscriberCardPatchIn,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> SubscriberCardPatchOut:
    """Edit a card; enabling the webhook channel returns the new signing secret once."""
    card = await _managed_card(request, principal, uow, card_id)
    changes = body.changes()
    if changes.get("webhook_enabled") is True and not await has_webhook_secret(
        uow.session, card.id
    ):
        # Issuing the first secret is credential management: identity's PDP refuses a PAT or a
        # key here (interactive-session-required, D-79). The card is already known to the caller.
        await _require_interactive_secret_right(request, principal)
    host_policy: WebhookHostPolicy | None = None
    if changes.get("webhook_url") is not None:
        # The Global admin's allowed-hosts list, read with the caller's own bearer (D-22, D-25).
        # A URL edit asks identity again instead of trusting the 30 s cache: an admin who has
        # just added the host in Settings must not be refused for another half minute (the last
        # good list still answers when identity cannot).
        settings_client = request.app.state.settings_client
        if settings_client is not None:
            settings_client.invalidate()
        effective = await effective_notifications(
            request.app.state.settings_client, parse_bearer(request), request.app.state.settings
        )
        seed = getattr(request.app.state, "webhook_host_seed", None)
        host_policy = WebhookHostPolicy(
            effective.allowed_hosts,
            # Until the one-time seed of the list is confirmed, a URL cannot be judged (503).
            effective.allowed_hosts_known and (seed is None or seed.done),
        )
    card, changed, new_secret = await update_card(
        uow, card.id, changes, settings=request.app.state.settings, host_policy=host_policy
    )
    if changed:
        await uow.commit()
    out = await _card_out(uow.session, card, show_url=True)
    return SubscriberCardPatchOut(**out.model_dump(), webhook_secret=new_secret)


async def _require_interactive_secret_right(request: Request, principal: Principal) -> None:
    authorizer = request.app.state.authorizer_factory(request)
    try:
        decision = await authorizer.check(principal, "card:manage-secret")
    except AuthorizerUnavailable as exc:
        raise _unavailable(exc) from exc
    if decision.outcome is not Outcome.ALLOW:
        raise ProblemError(
            403, "forbidden", "Forbidden", "You are not permitted to manage webhook secrets."
        )


@router.post(
    "/cards/{card_id}/webhook-secret",
    response_model=CardWebhookSecretOut,
    dependencies=[require("card:manage-secret")],
    responses={
        401: {"description": "Not authenticated"},
        403: {
            "description": (
                "The caller did not sign in interactively (interactive-session-required, a PAT or "
                "a key is refused before the card is looked up), or can read the card but is "
                "neither its creator nor an admin (forbidden)"
            )
        },
        404: {"description": "Card not found, or not visible to the caller (one response)"},
        409: {
            "description": (
                "The card is archived (card-archived), the folder is archived, or the card has no "
                "webhook URL (webhook-not-configured)"
            )
        },
        503: {
            "description": (
                "Authorization is temporarily unavailable, or webhook delivery is not configured "
                "on this deployment"
            )
        },
    },
)
async def rotate_webhook_secret_route(
    card_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> CardWebhookSecretOut:
    """Rotate the signing secret; the previous one stays valid for the overlap window."""
    card = await _managed_card(request, principal, uow, card_id)
    if card.archived_at is not None:
        raise archived_problem()
    settings = request.app.state.settings
    box = secret_box(settings)
    secret, row = await rotate_webhook_secret(uow, card, box, settings.webhook_secret_overlap_s)
    await uow.commit()
    return CardWebhookSecretOut(
        webhook_secret=secret,
        generation=row.generation,
        previous_valid_until=row.previous_valid_until,
    )


@router.post(
    "/cards/{card_id}/archive",
    response_model=SubscriberCardOut,
    dependencies=[require("card:write")],
    responses={
        403: {
            "description": "The caller can read the card but is neither its creator nor an admin"
        },
        404: {"description": "Card not found, or not visible to the caller (one response)"},
        409: {"description": "The folder is archived"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def archive_card_route(
    card_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> SubscriberCardOut:
    card = await _managed_card(request, principal, uow, card_id)
    card, changed = await archive_card(uow, card.id)
    if changed:
        await uow.commit()
    return await _card_out(uow.session, card, show_url=True)


@router.post(
    "/cards/{card_id}/members",
    status_code=201,
    response_model=CardMemberOut,
    dependencies=[require("card:write")],
    responses={
        200: {"description": "The member was already linked (no change, no event)"},
        403: {
            "description": "The caller can read the card but is neither its creator nor an admin"
        },
        404: {"description": "Card not found, or not visible to the caller (one response)"},
        409: {"description": "The card is archived (card-archived) or the folder is archived"},
        422: {
            "description": (
                "Invalid body, or the member cannot be linked to the card (invalid-member)"
            )
        },
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def add_card_member_route(
    card_id: uuid.UUID,
    body: CardMemberIn,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> CardMemberOut:
    """Link a local group, a directory group or a service account; identity decides eligibility."""
    card = await _managed_card(request, principal, uow, card_id)
    if card.archived_at is not None:
        raise archived_problem()
    authorizer = request.app.state.authorizer_factory(request)
    key = (body.member_type, body.member_id)
    # Only a manager reaches this point, so the caller already sees the card: an identity failure
    # here is a 503 and reveals nothing (D-35, D-62).
    try:
        if not isinstance(authorizer, GranteeAuthorizer):
            raise AuthorizerUnavailable("authorizer cannot check members")
        answer = await authorizer.check_members(principal, card.folder_id, [key])
    except AuthorizerUnavailable as exc:
        raise _unavailable(exc) from exc
    if answer.get(key) is not True:
        raise ProblemError(
            422,
            "invalid-member",
            "Unprocessable Content",
            "This member cannot be linked to the card.",
        )
    member, created = await add_member(uow, card, body.member_type, body.member_id)
    if created:
        await uow.commit()
    else:
        response.status_code = 200
    return _member_out(member)


@router.delete(
    "/cards/{card_id}/members/{member_type}/{member_id}",
    status_code=204,
    response_class=Response,
    dependencies=[require("card:write")],
    responses={
        403: {
            "description": "The caller can read the card but is neither its creator nor an admin"
        },
        404: {"description": "Card not found, or not visible to the caller (one response)"},
        409: {"description": "The card is archived (card-archived) or the folder is archived"},
        422: {"description": "Unknown member type"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def remove_card_member_route(
    card_id: uuid.UUID,
    member_type: Literal["local_group", "entra_group", "service_account"],
    member_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> Response:
    """Unlink a member; unlinking one that is not linked is a 204 no-op without an event."""
    card = await _managed_card(request, principal, uow, card_id)
    if card.archived_at is not None:
        raise archived_problem()
    if await remove_member(uow, card, member_type, member_id):
        await uow.commit()
    return Response(status_code=204)


async def _subscribing_card(
    request: Request, principal: Principal, uow: UnitOfWorkDep, card_id: uuid.UUID
) -> SubscriberCard:
    """The card, once the caller is known to act for it: a manager or a linked member (D-85).

    Not readable -> the uniform 404; readable but neither manager nor member -> 403. The
    membership lookup of a caller the folder decision did not allow fails closed to that same 404
    (``card_capability``); only a caller who already sees the card (a folder reader whose
    subscribe needs the lookup) gets 503. The archived-folder rule applies to managers only: a
    member cannot be told about the folder (Open Question 5).
    """
    card = await get_card(uow.session, card_id)
    try:
        capability = await card_capability(
            request.app.state.authorizer_factory(request),
            principal,
            card,
            uow.session,
            fresh=True,
            need="subscribe",
        )
    except AuthorizerUnavailable as exc:
        raise _unavailable(exc) from exc
    if not capability.read:
        raise _not_found()
    if not capability.subscribe:
        raise ProblemError(
            403,
            "forbidden",
            "Forbidden",
            "Only the card's managers and linked members may change its subscriptions.",
        )
    if capability.manage and capability.facts is not None:
        require_writable_folder(capability.facts)
    return card


@router.post(
    "/cards/{card_id}/subscriptions",
    status_code=201,
    response_model=CardSubscriptionOut,
    dependencies=[require("card:write")],
    responses={
        200: {"description": "The card already pins this version (no change, no event)"},
        403: {
            "description": (
                "The caller can read the card but is neither a manager nor a linked member, "
                "or the contract does not accept subscriptions"
            )
        },
        404: {
            "description": (
                "Card not found or not visible to the caller (one response), or the caller "
                "cannot read the contract or the version (the unknown-contract 404)"
            )
        },
        409: {
            "description": (
                "The card is archived (card-archived), the folder is archived, the version is "
                "deprecated (version-deprecated), or unpublished and visible to the caller "
                "(version-not-published)"
            )
        },
        422: {"description": "Invalid body"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def subscribe_card_route(
    card_id: uuid.UUID,
    body: CardSubscribeIn,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> CardSubscriptionOut:
    """Pin a published version to the card; the acting principal's own read access decides."""
    card = await _subscribing_card(request, principal, uow, card_id)
    if card.archived_at is not None:
        raise archived_problem()
    try:
        pin, created = await subscribe(
            uow,
            request.app.state.authorizer_factory(request),
            principal,
            card,
            body.contract_id,
            body.version,
        )
    except AuthorizerUnavailable as exc:
        raise _unavailable(exc) from exc
    if created:
        await uow.commit()
        response.headers["Location"] = f"/api/v1/contracts/cards/{card.id}/subscriptions/{pin.id}"
    else:
        response.status_code = 200
    stored, semver = await pin_with_semver(uow.session, pin.id)
    return _subscription_out(stored, semver)


@router.post(
    "/cards/{card_id}/subscriptions/{subscription_id}/move",
    response_model=CardSubscriptionOut,
    dependencies=[require("card:write")],
    responses={
        403: {
            "description": (
                "The caller can read the card but is neither a manager nor a linked member"
            )
        },
        404: {
            "description": (
                "Card, subscription or version not found or not visible to the caller "
                "(the unknown-resource 404)"
            )
        },
        409: {
            "description": (
                "The subscription has ended (subscription-ended), the card or folder is archived, "
                "the version is deprecated (version-deprecated) or unpublished "
                "(version-not-published)"
            )
        },
        422: {"description": "Invalid body"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def move_subscription_route(
    card_id: uuid.UUID,
    subscription_id: uuid.UUID,
    body: CardMoveIn,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> CardSubscriptionOut:
    """End the pin as ``moved`` and pin another version of the contract, atomically."""
    card = await _subscribing_card(request, principal, uow, card_id)
    if card.archived_at is not None:
        raise archived_problem()
    try:
        pin, changed = await move_subscription(
            uow,
            request.app.state.authorizer_factory(request),
            principal,
            card,
            subscription_id,
            body.version,
        )
    except AuthorizerUnavailable as exc:
        raise _unavailable(exc) from exc
    if changed:
        await uow.commit()
    stored, semver = await pin_with_semver(uow.session, pin.id)
    return _subscription_out(stored, semver)


@router.post(
    "/cards/{card_id}/subscriptions/{subscription_id}/unsubscribe",
    response_model=CardSubscriptionOut,
    dependencies=[require("card:write")],
    responses={
        200: {"description": "The pin was ended, or had already ended (no change, no event)"},
        403: {
            "description": (
                "The caller can read the card but is neither a manager nor a linked member"
            )
        },
        404: {"description": "Card or subscription not found or not visible to the caller"},
        409: {"description": "The card is archived (card-archived) or the folder is archived"},
        422: {"description": "Invalid body"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def unsubscribe_route(
    card_id: uuid.UUID,
    subscription_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
    body: CardUnsubscribeIn | None = None,
) -> CardSubscriptionOut:
    """End the pin as ``unsubscribed``; the row and its history stay."""
    card = await _subscribing_card(request, principal, uow, card_id)
    if card.archived_at is not None:
        raise archived_problem()
    pin, changed = await unsubscribe(
        uow, card, subscription_id, body.note if body is not None else None
    )
    if changed:
        await uow.commit()
    stored, semver = await pin_with_semver(uow.session, pin.id)
    return _subscription_out(stored, semver)


@router.get(
    "/cards/{card_id}/subscriptions",
    response_model=CardSubscriptionListOut,
    dependencies=[require("card:read")],
    responses={
        404: {"description": "Card not found, or not visible to the caller (one response)"},
        422: {"description": "Invalid state, limit or cursor"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def list_card_subscriptions_route(
    card_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    state: Annotated[Literal["active", "ended", "all"], Query()] = "all",
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> CardSubscriptionListOut:
    """The card's complete pin record, newest first, with a keyset cursor and no total."""
    card = await _readable_card(request, principal, session, card_id)
    after = decode_cursor(cursor) if cursor is not None else None
    rows = await card_subscriptions(session, card.id, state=state, cursor=after, limit=limit + 1)
    page = rows[:limit]
    return CardSubscriptionListOut(
        items=[_subscription_out(sub, semver) for sub, semver in page],
        next_cursor=encode_cursor(page[-1][0].id) if len(rows) > limit else None,
    )


@router.get(
    "/cards/{card_id}/notices",
    response_model=CardNoticeListOut,
    dependencies=[require("card:read")],
    responses={
        404: {"description": "Card not found, or not visible to the caller (one response)"},
        422: {"description": "Invalid limit or cursor"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def list_card_notices_route(
    card_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> CardNoticeListOut:
    """The card's recorded notices, newest first, keyset-paged with no total.

    There is no ``since`` filter, read position or acknowledgement: the list is the record.
    """
    card = await _readable_card(request, principal, session, card_id)
    after = decode_cursor(cursor) if cursor is not None else None
    page, next_id = await list_notices(session, card.id, cursor=after, limit=limit)
    return CardNoticeListOut(
        items=[
            CardNoticeOut(
                id=notice.id,
                event_type=notice.event_type,
                contract_id=notice.contract_id,
                version_id=notice.version_id,
                occurred_at=notice.occurred_at,
                created_at=notice.created_at,
                payload=notice.payload,
                deliveries=[
                    CardNoticeDeliveryOut(
                        channel=delivery.channel,
                        status=delivery.status,
                        attempts=delivery.attempts,
                        last_attempt_at=delivery.last_attempt_at,
                        last_status=delivery.last_status,
                        delivered_at=delivery.delivered_at,
                        last_error=delivery.last_error,
                    )
                    for delivery in deliveries
                ],
            )
            for notice, deliveries in page
        ],
        next_cursor=encode_cursor(next_id) if next_id is not None else None,
    )


@router.get(
    "/folders/{folder_id}/cards",
    response_model=SubscriberCardListOut,
    dependencies=[require("folder:read", target=_path_folder_id)],
    responses={
        404: {"description": "Folder not found, or not visible to the caller"},
        422: {"description": "Invalid limit or cursor"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def list_cards_route(
    folder_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(read_session)],
    include_archived: Annotated[bool, Query()] = False,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> SubscriberCardListOut:
    """The folder's cards in id order (uuidv7, so creation order) with a keyset cursor, no total."""
    after = decode_cursor(cursor) if cursor is not None else None
    cards, next_id = await list_cards(
        session, folder_id, include_archived=include_archived, cursor=after, limit=limit
    )
    return SubscriberCardListOut(
        items=[
            SubscriberCardSummaryOut(
                id=card.id,
                folder_id=card.folder_id,
                name=card.name,
                description=card.description,
                contacts=_contacts_out(card),
                email_enabled=card.email_enabled,
                webhook_enabled=card.webhook_enabled,
                archived_at=card.archived_at,
                created_at=card.created_at,
            )
            for card in cards
        ],
        next_cursor=encode_cursor(next_id) if next_id is not None else None,
    )


@router.get(
    "/cards",
    response_model=SubscriberCardIndexOut,
    dependencies=[require("card:read")],
    responses={
        422: {"description": "Invalid limit, cursor or filter value"},
        503: {"description": "Authorization is temporarily unavailable"},
    },
)
async def list_all_cards_route(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    folder_id: Annotated[uuid.UUID | None, Query()] = None,
    subtree: Annotated[bool, Query()] = True,
    q: Annotated[str | None, Query(max_length=MAX_Q_LENGTH)] = None,
    include_archived: Annotated[bool, Query()] = False,
    has_active_subscriptions: Annotated[bool | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> SubscriberCardIndexOut:
    """Every subscriber card the caller may read, across folders, in id order (keyset cursor).

    ``card:read`` admits every authenticated caller; the rows are trimmed to the cards the caller
    could open by id: folder readers (``accessible_folders(folder:read)``) and linked members. A
    caller who sees nothing gets an empty page, never a 403. ``folder_id`` narrows to that folder
    and, with ``subtree`` (default), everything below it; an unreadable folder is an empty page.
    The totals count the filtered visible cards only, so they never reveal a hidden card.
    """
    reject_nul(q=q)
    after = decode_cursor(cursor) if cursor is not None else None
    authorizer = request.app.state.authorizer_factory(request)
    try:
        folder_scope = await authorizer.accessible_folders(principal, "folder:read")
        grantees = (
            await authorizer.grantees(principal)
            if isinstance(authorizer, GranteeAuthorizer)
            else frozenset()
        )
        name_scopes = tuple(
            [
                await authorizer.accessible_folders(principal, permission)
                for permission in (
                    "contract:read-metadata",
                    "contract:read",
                    "contract:read-published",
                )
            ]
        )
    except AuthorizerUnavailable as exc:
        raise _unavailable(exc) from exc
    page = await list_visible_cards(
        session,
        CardIndexScopes(folder_read=folder_scope, grantees=grantees, contract_names=name_scopes),
        CardIndexFilters(
            folder_id=folder_id,
            subtree=subtree,
            q=q or None,
            include_archived=include_archived,
            has_active_subscriptions=has_active_subscriptions,
            cursor=after,
            limit=limit,
        ),
    )
    return SubscriberCardIndexOut(
        items=[
            SubscriberCardIndexItemOut(
                id=row.card.id,
                name=row.card.name,
                description=row.card.description,
                folder_id=row.card.folder_id,
                folder_ancestor_ids=list(row.card.ancestor_ids),
                organization_id=row.card.organization_id,
                archived_at=row.card.archived_at,
                created_at=row.card.created_at,
                updated_at=row.card.updated_at,
                active_subscription_count=row.active_subscription_count,
                subscriptions=[
                    CardIndexPinOut(
                        contract_id=pin.contract_id,
                        version_id=pin.version_id,
                        semver=pin.semver,
                        contract_name=pin.contract_name,
                    )
                    for pin in row.pins
                ],
            )
            for row in page.rows
        ],
        next_cursor=encode_cursor(page.next_cursor) if page.next_cursor is not None else None,
        total_cards=page.total_cards,
        total_folders=page.total_folders,
        pin_preview_limit=PIN_PREVIEW_LIMIT,
    )
