"""Card routes when identity answers oddly (SUB-01, SUB-02, SUB-03, D-62, D-79, D-85).

Each case swaps the contracts app's authorizer for a thin wrapper around the real one, so only the
one answer under test changes: an outage of one check is 503 for a caller who already sees the
card, a refused secret right is 403, an authorizer that cannot list members is 503. It also covers
writes to an archived card, a deployment with no settings client, and a member who is also a folder
reader.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, contextmanager
from functools import partial
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from dc_auth.authorizer import AuthorizerUnavailable, Decision, FolderScope, Outcome
from dc_auth.principal import Principal

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"
URL = "https://hooks.example.test/hook"
KEY = Fernet.generate_key().decode()

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_problem(response: httpx.Response, status: int, slug: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["type"] == f"urn:dc:problem:{slug}"


class WithoutMembers:
    """The real authorizer's ``check`` and ``accessible_folders`` only: not a GranteeAuthorizer."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: uuid.UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        decision: Decision = await self._inner.check(principal, permission, folder_id, fresh=fresh)
        return decision

    async def accessible_folders(self, principal: Principal, permission: str) -> FolderScope:
        scope: FolderScope = await self._inner.accessible_folders(principal, permission)
        return scope


class OneCheckAnswered(WithoutMembers):
    """The real authorizer, except that ``check`` of one permission gets a fixed answer (``None``
    raises ``AuthorizerUnavailable``). It lists grantees and checks members like the real one, so it
    is still a ``GranteeAuthorizer`` (a runtime protocol check sees only real methods)."""

    def __init__(self, inner: Any, permission: str, answer: Decision | None) -> None:
        super().__init__(inner)
        self._permission = permission
        self._answer = answer

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: uuid.UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        if permission != self._permission:
            return await super().check(principal, permission, folder_id, fresh=fresh)
        if self._answer is None:
            raise AuthorizerUnavailable("injected failure")
        return self._answer

    async def grantees(self, principal: Principal) -> frozenset[tuple[str, str]]:
        found: frozenset[tuple[str, str]] = await self._inner.grantees(principal)
        return found

    async def check_members(
        self, principal: Principal, folder_id: uuid.UUID, members: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], bool]:
        answer: dict[tuple[str, str], bool] = await self._inner.check_members(
            principal, folder_id, members
        )
        return answer


@contextmanager
def authorizer_as(stack: Any, wrap: Callable[[Any], Any]) -> Iterator[None]:
    state = stack.contracts_app.state
    original = state.authorizer_factory
    state.authorizer_factory = lambda request: wrap(original(request))
    try:
        yield
    finally:
        state.authorizer_factory = original


async def member_of(stack: Any, card_id: str, *, team_reader_at: str | None = None) -> str:
    """A new user, linked to the card through a local group; returns the user's token."""
    token, principal = stack.token(f"mem-{uuid.uuid4().hex[:6]}")
    if team_reader_at is not None:
        stack.grant_sql(principal, "team_reader", team_reader_at)
    group = await stack.local_group(stack.root_id, f"g-{uuid.uuid4().hex[:6]}")
    stack.group_member_sql(group, principal)
    linked = await stack.link_member(card_id, "local_group", group)
    assert linked.status_code == 201, linked.text
    return str(token)


async def pinned_card(stack: Any) -> tuple[str, dict[str, Any], str]:
    """A card pinning 1.0.0 of a contract that also has 1.1.0; returns contract, card, pin id."""
    chain = await stack.chain()
    contract, _ = await stack.publish_versions(chain.team, ["1.0.0", "1.1.0"])
    card = await stack.create_card(chain.team)
    pinned = await stack.subscribe(card["id"], contract, "1.0.0")
    assert pinned.status_code == 201, pinned.text
    return contract, card, str(pinned.json()["id"])


async def test_a_member_who_is_also_a_folder_reader_reads_and_subscribes(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await stack.publish_versions(chain.team, ["1.0.0"])
        card = await stack.create_card(chain.team)
        token = await member_of(stack, card["id"], team_reader_at=chain.team)

        read = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(token))
        assert read.status_code == 200, read.text
        assert read.json()["id"] == card["id"]
        pinned = await stack.subscribe(card["id"], contract, "1.0.0", token=token)
        assert pinned.status_code == 201, pinned.text


async def test_a_member_write_whose_denial_cannot_be_recorded_is_503(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        card = await stack.create_card(chain.team)
        token = await member_of(stack, card["id"])
        with authorizer_as(stack, lambda inner: OneCheckAnswered(inner, "folder:read", None)):
            refused = await stack.client.patch(
                f"{CARDS}/{card['id']}", json={"description": "x"}, headers=bearer(token)
            )
        assert_problem(refused, 503, "service-unavailable")


@pytest.mark.parametrize(
    ("answer", "status", "slug"),
    [
        (None, 503, "service-unavailable"),
        (Decision(outcome=Outcome.FORBIDDEN), 403, "forbidden"),
    ],
)
async def test_the_first_secret_needs_the_secret_right_and_an_answer_from_identity(
    make_stack: MakeStack, sql: Sql, answer: Decision | None, status: int, slug: str
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        chain = await stack.chain()
        card = await stack.create_card(chain.team)
        sql(
            stack.contracts_db_url,
            "UPDATE subscriber_card SET webhook_url = %s WHERE id = %s",
            URL,
            card["id"],
        )
        wrap = partial(OneCheckAnswered, permission="card:manage-secret", answer=answer)
        with authorizer_as(stack, wrap):
            refused = await stack.client.patch(
                f"{CARDS}/{card['id']}",
                json={"webhook_enabled": True},
                headers=bearer(stack.admin_token),
            )
        assert_problem(refused, status, slug)
        assert sql(stack.contracts_db_url, "SELECT * FROM card_webhook_secret") == []


async def test_an_authorizer_that_cannot_check_members_makes_linking_503(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        card = await stack.create_card(chain.team)
        group = await stack.local_group(stack.root_id, "linkers")
        with authorizer_as(stack, WithoutMembers):
            refused = await stack.link_member(card["id"], "local_group", group)
        assert_problem(refused, 503, "service-unavailable")
        assert sql(stack.contracts_db_url, "SELECT * FROM card_member") == []


async def test_pinning_or_moving_while_the_read_check_is_down_is_503(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        contract, card, pin = await pinned_card(stack)
        with stack.flaky_authz(fail_permissions=frozenset({"contract:read-published"})):
            pinned = await stack.subscribe(card["id"], contract, "1.1.0")
            moved = await stack.client.post(
                f"{CARDS}/{card['id']}/subscriptions/{pin}/move",
                json={"version": "1.1.0"},
                headers=bearer(stack.admin_token),
            )
        assert_problem(pinned, 503, "service-unavailable")
        assert_problem(moved, 503, "service-unavailable")


async def test_moving_or_ending_a_pin_of_an_archived_card_is_card_archived(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        _contract, card, pin = await pinned_card(stack)
        base = f"{CARDS}/{card['id']}"
        archived = await stack.client.post(f"{base}/archive", headers=bearer(stack.admin_token))
        assert archived.status_code == 200, archived.text

        moved = await stack.client.post(
            f"{base}/subscriptions/{pin}/move",
            json={"version": "1.1.0"},
            headers=bearer(stack.admin_token),
        )
        ended = await stack.client.post(
            f"{base}/subscriptions/{pin}/unsubscribe", headers=bearer(stack.admin_token)
        )
        assert_problem(moved, 409, "card-archived")
        assert_problem(ended, 409, "card-archived")


async def test_without_a_settings_client_no_webhook_host_is_allowed(
    make_stack: MakeStack, sql: Sql
) -> None:
    # The allowed-hosts list is mandatory: with no stored list to read, the empty env list
    # allows nothing, so a URL edit is refused before any DNS lookup.
    async with make_stack() as stack:
        chain = await stack.chain()
        card = await stack.create_card(chain.team)
        state = stack.contracts_app.state
        client, state.settings_client = state.settings_client, None
        try:
            refused = await stack.client.patch(
                f"{CARDS}/{card['id']}",
                json={"webhook_url": URL},
                headers=bearer(stack.admin_token),
            )
        finally:
            state.settings_client = client
        assert_problem(refused, 422, "webhook-host-not-allowed")
        (row,) = sql(
            stack.contracts_db_url,
            "SELECT webhook_url FROM subscriber_card WHERE id = %s",
            card["id"],
        )
        assert row["webhook_url"] is None


async def test_rotating_a_url_only_card_issues_generation_one_with_no_previous_secret(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        chain = await stack.chain()
        card = await stack.create_card(chain.team)
        sql(
            stack.contracts_db_url,
            "UPDATE subscriber_card SET webhook_url = %s WHERE id = %s",
            URL,
            card["id"],
        )
        rotated = await stack.client.post(
            f"{CARDS}/{card['id']}/webhook-secret", headers=bearer(stack.admin_token)
        )
        assert rotated.status_code == 200, rotated.text
        body = rotated.json()
        assert body["generation"] == 1
        assert body["previous_valid_until"] is None
        (row,) = sql(stack.contracts_db_url, "SELECT * FROM card_webhook_secret")
        assert row["previous_enc"] is None
        assert Fernet(KEY).decrypt(row["secret_enc"].encode()).decode() == body["webhook_secret"]
        (event,) = sql(
            stack.contracts_db_url,
            "SELECT before, after FROM outbox WHERE action = 'card.webhook_secret_rotated'",
        )
        assert (event["before"], event["after"]) == ({"generation": None}, {"generation": 1})
