"""Card and pin writes re-check under the row lock what a concurrent write may have changed.

The routes check a card before they lock it, so a concurrent archive, unsubscribe or URL edit can
land in between. These tests call the service functions the routes call and make that concurrent
write happen in another connection at the exact moment (right before the lock), then check that
the locked re-read refuses the write with the same answer the route would have given.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet
from dc_auth.authorizer import Decision, Outcome
from dc_auth.principal import Principal, principal_to_actor
from dc_contracts import cards, subscriptions
from dc_contracts.cards import (
    WebhookHostPolicy,
    add_member,
    get_card,
    remove_member,
    rotate_webhook_secret,
    update_card,
)
from dc_contracts.models import OutboxEvent, SubscriberCard
from dc_contracts.subscriptions import move_subscription, subscribe, unsubscribe
from dc_contracts.webhooks import SecretBox
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

URL = "https://hooks.example.test/hook"
OTHER_URL = "https://hooks.example.test/elsewhere"
ACTOR = Principal(
    id="fake:racer",
    type="user",
    display="racer",
    issuer="https://issuer.test",
    subject="racer",
    via="jwt",
    scopes=frozenset({"api"}),
)


class AllowAll:
    async def check(self, principal: Any, permission: str, folder_id: Any = None, **_: Any) -> Any:
        return Decision(outcome=Outcome.ALLOW)


@asynccontextmanager
async def unit(stack: Any) -> AsyncIterator[UnitOfWork]:
    async with UnitOfWork(
        stack.contracts_app.state.session_factory,
        actor=principal_to_actor(ACTOR),
        outbox_model=OutboxEvent,
        service="contracts",
    ) as uow:
        yield uow


async def loaded_card(stack: Any, card_id: str) -> SubscriberCard:
    async with stack.contracts_app.state.session_factory() as session:
        return await get_card(session, uuid.UUID(card_id))


async def pinned_world(stack: Any) -> dict[str, Any]:
    """A card pinning 1.0.0 of a contract that also has 1.1.0, as the route loaded it."""
    chain = await stack.chain()
    contract, _ = await stack.publish_versions(chain.team, ["1.0.0", "1.1.0"])
    created = await stack.create_card(chain.team)
    pinned = await stack.subscribe(created["id"], contract, "1.0.0")
    assert pinned.status_code == 201, pinned.text
    return {
        "contract": uuid.UUID(contract),
        "card": await loaded_card(stack, created["id"]),
        "pin": uuid.UUID(pinned.json()["id"]),
    }


def archive_behind(sql: Sql, stack: Any, card_id: uuid.UUID) -> None:
    """What a concurrent archive commits: the card archived and its active pins ended."""
    db = stack.contracts_db_url
    sql(
        db,
        "UPDATE subscriber_card SET archived_at = now(), archived_by = 'fake:other' WHERE id = %s",
        card_id,
    )
    sql(
        db,
        "UPDATE card_subscription SET ended_at = now(), ended_by = 'fake:other', "
        "end_reason = 'card_archived' WHERE card_id = %s AND ended_at IS NULL",
        card_id,
    )


def unsubscribe_behind(sql: Sql, stack: Any, card_id: uuid.UUID) -> None:
    sql(
        stack.contracts_db_url,
        "UPDATE card_subscription SET ended_at = now(), ended_by = 'fake:other', "
        "end_reason = 'unsubscribed' WHERE card_id = %s AND ended_at IS NULL",
        card_id,
    )


def just_before(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, act: Any) -> None:
    """Run ``act`` (another connection's commit) right before ``module.name`` takes its lock."""
    original = getattr(module, name)

    async def racing(session: Any, card_id: uuid.UUID) -> Any:
        act(card_id)
        return await original(session, card_id)

    monkeypatch.setattr(module, name, racing)


def assert_slug(caught: pytest.ExceptionInfo[ProblemError], status: int, slug: str) -> None:
    assert (caught.value.status, caught.value.slug) == (status, slug)


async def test_every_card_write_refuses_a_card_archived_after_the_route_looked(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await pinned_world(stack)
        card = w["card"]
        archive_behind(sql, stack, card.id)
        box = SecretBox(SecretStr(Fernet.generate_key().decode()))
        writes = [
            lambda uow: subscribe(uow, AllowAll(), ACTOR, card, w["contract"], "1.1.0"),
            lambda uow: unsubscribe(uow, card, w["pin"], None),
            lambda uow: rotate_webhook_secret(uow, card, box, 60),
            lambda uow: add_member(uow, card, "local_group", str(uuid.uuid4())),
            lambda uow: remove_member(uow, card, "local_group", str(uuid.uuid4())),
        ]
        for write in writes:
            async with unit(stack) as uow:
                with pytest.raises(ProblemError) as caught:
                    await write(uow)
            assert_slug(caught, 409, "card-archived")
        assert sql(stack.contracts_db_url, "SELECT * FROM card_webhook_secret") == []
        assert sql(stack.contracts_db_url, "SELECT * FROM card_member") == []


@pytest.mark.parametrize(
    ("behind", "status", "slug"),
    [
        (archive_behind, 409, "card-archived"),
        (unsubscribe_behind, 409, "subscription-ended"),
    ],
)
async def test_a_move_racing_an_archive_or_an_unsubscribe_is_refused_under_the_lock(
    make_stack: MakeStack,
    sql: Sql,
    monkeypatch: pytest.MonkeyPatch,
    behind: Any,
    status: int,
    slug: str,
) -> None:
    async with make_stack() as stack:
        w = await pinned_world(stack)
        just_before(
            monkeypatch,
            subscriptions,
            "_share_locked_card",
            lambda card_id: behind(sql, stack, card_id),
        )
        async with unit(stack) as uow:
            with pytest.raises(ProblemError) as caught:
                await move_subscription(uow, AllowAll(), ACTOR, w["card"], w["pin"], "1.1.0")
        assert_slug(caught, status, slug)
        active = sql(
            stack.contracts_db_url,
            "SELECT id FROM card_subscription WHERE card_id = %s AND ended_at IS NULL",
            w["card"].id,
        )
        assert active == []


async def test_a_card_that_is_gone_is_not_found_under_the_lock(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        w = await pinned_world(stack)
        gone = SimpleNamespace(id=uuid.uuid4())
        async with unit(stack) as uow:
            with pytest.raises(ProblemError) as pin_caught:
                await subscribe(uow, AllowAll(), ACTOR, gone, w["contract"], "1.1.0")  # type: ignore[arg-type]
        assert_slug(pin_caught, 404, "not-found")
        async with unit(stack) as uow:
            with pytest.raises(ProblemError) as edit_caught:
                await update_card(
                    uow, gone.id, {"webhook_url": URL}, settings=stack.contracts_app.state.settings
                )
        assert_slug(edit_caught, 404, "not-found")


async def test_a_url_that_changed_between_the_peek_and_the_lock_is_checked_again(
    make_stack: MakeStack, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.create_card(chain.team)
        db = stack.contracts_db_url
        sql(db, "UPDATE subscriber_card SET webhook_url = %s WHERE id = %s", URL, created["id"])

        def edited_elsewhere(card_id: uuid.UUID) -> None:
            sql(db, "UPDATE subscriber_card SET webhook_url = %s WHERE id = %s", OTHER_URL, card_id)

        just_before(monkeypatch, cards, "_locked_card", edited_elsewhere)
        # The peek saw the URL unchanged, so only the locked re-read can catch the edit; the
        # list no longer allows the host, so storing the old URL again is refused.
        policy = WebhookHostPolicy(hosts=("other.example.test",), known=True)
        async with unit(stack) as uow:
            with pytest.raises(ProblemError) as caught:
                await update_card(
                    uow,
                    uuid.UUID(created["id"]),
                    {"webhook_url": URL},
                    settings=stack.contracts_app.state.settings,
                    host_policy=policy,
                )
        assert_slug(caught, 422, "webhook-host-not-allowed")
        (row,) = sql(db, "SELECT webhook_url FROM subscriber_card WHERE id = %s", created["id"])
        assert row["webhook_url"] == OTHER_URL


async def test_a_database_constraint_other_than_the_name_index_is_raised_unmapped(
    make_stack: MakeStack,
) -> None:
    # The request model caps names at 200 characters; the database check is the backstop and is
    # never turned into the 409 that only the per-folder name index means.
    async with make_stack() as stack:
        chain = await stack.chain()
        created = await stack.create_card(chain.team)
        async with unit(stack) as uow:
            with pytest.raises(IntegrityError) as caught:
                await update_card(
                    uow,
                    uuid.UUID(created["id"]),
                    {"name": "n" * 201},
                    settings=stack.contracts_app.state.settings,
                )
        assert "name_length" in str(caught.value.orig.diag.constraint_name)  # type: ignore[union-attr]
