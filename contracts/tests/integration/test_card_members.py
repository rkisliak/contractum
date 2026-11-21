"""Card members (SUB-02, SUB-04): link groups and service accounts, and what members may do (D-85).

A card manager links a local group, an Entra group or a service account. A member reads the card
it acts for even without a role at the card's folder (D-104), but cannot rewire it (D-85). Identity
decides who is linkable (D-27, D-69, D-64). An identity failure never grants access and never
reveals which card ids exist (D-62, D-35).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"
UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_problem(response: httpx.Response, status: int, slug: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    assert body["status"] == status
    return body


def stable(response: httpx.Response) -> dict[str, Any]:
    """The parts of a problem that must not depend on whether the card exists."""
    body = response.json()
    return {k: body.get(k) for k in ("type", "title", "status", "detail")}


def events(sql: Sql, stack: Any, like: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_type, aggregate_id, scope_path, before, after FROM outbox "
        "WHERE action LIKE %s ORDER BY id",
        like,
    )


def member_rows(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT member_type, member_id FROM card_member WHERE card_id = %s "
        "ORDER BY member_type, member_id",
        card_id,
    )


async def _card_with_creator(stack: Any) -> tuple[Any, dict[str, Any], str]:
    """A chain, a card created by a Consumer, and that Consumer's token."""
    chain = await stack.chain()
    token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "consumer", chain.team)
    card = await stack.create_card(chain.team, token=token)
    return chain, card, token


# --- tracer: a manager links a local group and a member of that group reads the card ----------


async def test_a_manager_links_a_local_group_and_a_member_of_it_reads_the_card(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain, card, manager = await _card_with_creator(stack)
        group = await stack.local_group(stack.root_id, "pipeline-people")

        linked = await stack.link_member(card["id"], "local_group", group, token=manager)
        assert linked.status_code == 201, linked.text
        body = linked.json()
        assert body["member_type"] == "local_group"
        assert body["member_id"] == group
        assert body["added_by"] == card["created_by"]
        assert body["added_at"]
        added = events(sql, stack, "card.member_added")
        assert len(added) == 1
        assert added[0]["aggregate_type"] == "card"
        assert added[0]["aggregate_id"] == card["id"]
        assert added[0]["after"] == {"member_type": "local_group", "member_id": group}
        assert [str(i) for i in added[0]["scope_path"]] == [
            stack.root_id,
            chain.org,
            chain.project,
            chain.team,
        ]

        dana, dana_principal = stack.token("dana")
        erin, _ = stack.token("erin")
        stack.group_member_sql(group, dana_principal)

        seen = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(dana))
        assert seen.status_code == 200, seen.text
        assert [(m["member_type"], m["member_id"]) for m in seen.json()["members"]] == [
            ("local_group", group)
        ]
        listing = await stack.client.get(
            f"/api/v1/contracts/folders/{chain.team}/cards", headers=bearer(dana)
        )
        assert_problem(listing, 404, "not-found")
        outsider = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(erin))
        unknown = await stack.client.get(f"{CARDS}/{UNKNOWN}", headers=bearer(erin))
        assert_problem(outsider, 404, "not-found")
        assert stable(outsider) == stable(unknown)


async def test_an_identity_outage_is_503_for_every_card_id_even_for_a_member(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        group = await stack.local_group(stack.root_id, "g-outage")
        assert (await stack.link_member(card["id"], "local_group", group, token=manager)).is_success
        dana, dana_principal = stack.token("dana-out")
        stack.group_member_sql(group, dana_principal)

        stack.identity_link.down = True
        try:
            known = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(dana))
            unknown = await stack.client.get(f"{CARDS}/{UNKNOWN}", headers=bearer(dana))
        finally:
            stack.identity_link.down = False
        assert_problem(known, 503, "service-unavailable")
        assert_problem(unknown, 503, "service-unavailable")
        assert stable(known) == stable(unknown)


async def test_a_failing_membership_lookup_is_the_uniform_404_unless_the_caller_reads_the_folder(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        group = await stack.local_group(stack.root_id, "g-flaky")
        assert (await stack.link_member(card["id"], "local_group", group, token=manager)).is_success
        dana, dana_principal = stack.token("dana-flaky")
        erin, _ = stack.token("erin-flaky")
        stack.group_member_sql(group, dana_principal)

        with stack.flaky_authz(fail_grantees=True):
            member = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(dana))
            outsider = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(erin))
            unknown = await stack.client.get(f"{CARDS}/{UNKNOWN}", headers=bearer(erin))
            creator = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(manager))
        for response in (member, outsider, unknown):
            assert_problem(response, 404, "not-found")
        assert stable(member) == stable(outsider) == stable(unknown)
        # a folder reader's read never consults the membership lookup
        assert creator.status_code == 200, creator.text


async def test_a_failing_folder_decision_fails_closed_before_the_membership_branch(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        group = await stack.local_group(stack.root_id, "g-folder-flaky")
        assert (await stack.link_member(card["id"], "local_group", group, token=manager)).is_success
        dana, dana_principal = stack.token("dana-ff")
        erin, _ = stack.token("erin-ff")
        stack.group_member_sql(group, dana_principal)

        # The member's answer comes from its folder:read scope (WR-02), which fails here too.
        with stack.flaky_authz(fail_permissions=frozenset({"folder:read"})):
            member = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(dana))
            outsider = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(erin))
            unknown = await stack.client.get(f"{CARDS}/{UNKNOWN}", headers=bearer(erin))
        for response in (member, outsider, unknown):
            assert_problem(response, 404, "not-found")
        assert stable(member) == stable(outsider) == stable(unknown)


def denial_rows(sql: Sql, stack: Any, principal_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.identity_db_url,
        "SELECT aggregate_id, detail FROM outbox WHERE aggregate_type = 'authz' "
        "AND action = 'check.denied' AND actor->>'id' = %s ORDER BY id",
        principal_id,
    )


async def test_a_members_reads_record_no_denial_and_an_outsiders_refusal_still_does(
    make_stack: MakeStack, sql: Sql
) -> None:
    """WR-02: a linked member without a folder role is answered without the folder:read check."""
    async with make_stack() as stack:
        chain, card, manager = await _card_with_creator(stack)
        group = await stack.local_group(stack.root_id, "g-no-denial")
        assert (await stack.link_member(card["id"], "local_group", group, token=manager)).is_success
        dana, dana_principal = stack.token("dana-nd")
        erin, erin_principal = stack.token("erin-nd")
        stack.group_member_sql(group, dana_principal)

        seen = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(dana))
        assert seen.status_code == 200, seen.text
        notices = await stack.client.get(f"{CARDS}/{card['id']}/notices", headers=bearer(dana))
        assert notices.status_code == 200, notices.text
        assert denial_rows(sql, stack, dana_principal) == []
        # A member is still no manager: the write is refused, and that refusal is recorded.
        patched = await stack.client.patch(
            f"{CARDS}/{card['id']}", json={"description": "x"}, headers=bearer(dana)
        )
        assert_problem(patched, 403, "forbidden")
        refused = denial_rows(sql, stack, dana_principal)
        assert len(refused) == 1, refused
        assert refused[0]["detail"]["permission"] == "folder:read"
        assert refused[0]["detail"]["route"] == "/api/v1/contracts/cards/{card_id}"

        outsider = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(erin))
        assert_problem(outsider, 404, "not-found")
        rows = denial_rows(sql, stack, erin_principal)
        assert len(rows) == 1, rows
        assert str(rows[0]["aggregate_id"]) == chain.team
        assert rows[0]["detail"]["permission"] == "folder:read"
        assert rows[0]["detail"]["outcome"] == "not_found"


# --- service accounts, out-of-scope members, unlink ---------------------------------------------


def member_url(card_id: str, member_type: str, member_id: str) -> str:
    return f"{CARDS}/{card_id}/members/{member_type}/{member_id}"


async def test_a_linked_service_account_reads_the_card_but_cannot_rewire_it(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        key, account = await stack.service_account_key(stack.root_id, "pipeline-bot")
        group = await stack.local_group(stack.root_id, "keep-me")
        assert (await stack.link_member(card["id"], "local_group", group, token=manager)).is_success
        linked = await stack.link_member(card["id"], "service_account", account, token=manager)
        assert linked.status_code == 201, linked.text
        assert linked.json()["member_id"] == account

        read = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(key))
        assert read.status_code == 200, read.text
        patch = await stack.client.patch(
            f"{CARDS}/{card['id']}",
            json={
                "name": "renamed",
                "description": "x",
                "contacts": [{"kind": "text", "value": "me"}],
                "email_enabled": True,
            },
            headers=bearer(key),
        )
        assert_problem(patch, 403, "forbidden")
        archive = await stack.client.post(f"{CARDS}/{card['id']}/archive", headers=bearer(key))
        assert_problem(archive, 403, "forbidden")
        link = await stack.link_member(card["id"], "local_group", group, token=key)
        assert_problem(link, 403, "forbidden")
        other = await stack.local_group(stack.root_id, "sneaky")
        sneaky = await stack.link_member(card["id"], "local_group", other, token=key)
        assert_problem(sneaky, 403, "forbidden")
        unlink = await stack.client.delete(
            member_url(card["id"], "service_account", account), headers=bearer(key)
        )
        assert_problem(unlink, 403, "forbidden")

        # nothing changed: the card is as created and both links remain
        after = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(manager))
        body = after.json()
        assert body["name"] == card["name"]
        assert body["description"] == card["description"]
        assert body["contacts"] == card["contacts"]
        assert body["email_enabled"] is False
        assert body["archived_at"] is None
        assert len(member_rows(sql, stack, card["id"])) == 2
        assert [e["action"] for e in events(sql, stack, "card.%")] == [
            "card.created",
            "card.member_added",
            "card.member_added",
        ]


async def test_members_outside_the_card_s_organization_or_malformed_are_invalid_member(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        other_chain = await stack.chain()
        foreign_group = await stack.local_group(other_chain.org, "foreign-group")
        _, foreign_account = await stack.service_account_key(other_chain.org, "foreign-bot")
        unknown_group = str(uuid.uuid4())

        refused = [
            await stack.link_member(card["id"], "local_group", foreign_group, token=manager),
            await stack.link_member(card["id"], "service_account", foreign_account, token=manager),
            await stack.link_member(card["id"], "service_account", "sa:garbage", token=manager),
            await stack.link_member(card["id"], "local_group", unknown_group, token=manager),
            await stack.link_member(card["id"], "entra_group", "not-a-guid", token=manager),
        ]
        for response in refused:
            assert_problem(response, 422, "invalid-member")
        assert len({str(stable(r)) for r in refused}) == 1  # no hint which refusal it was

        wrong_type = await stack.link_member(card["id"], "user", "fake:dana", token=manager)
        assert_problem(wrong_type, 422, "validation-error")
        too_long = await stack.link_member(card["id"], "local_group", "a" * 201, token=manager)
        assert_problem(too_long, 422, "validation-error")
        assert member_rows(sql, stack, card["id"]) == []
        assert events(sql, stack, "card.member_%") == []


async def test_an_identity_failure_while_linking_is_503_and_writes_nothing(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        group = await stack.local_group(stack.root_id, "g-503")

        stack.identity_link.down = True
        try:
            down = await stack.link_member(card["id"], "local_group", group, token=manager)
        finally:
            stack.identity_link.down = False
        assert_problem(down, 503, "service-unavailable")

        with stack.flaky_authz(fail_check_members=True):
            flaky = await stack.link_member(card["id"], "local_group", group, token=manager)
        assert_problem(flaky, 503, "service-unavailable")
        assert member_rows(sql, stack, card["id"]) == []
        assert events(sql, stack, "card.member_%") == []

        ok = await stack.link_member(card["id"], "local_group", group, token=manager)
        assert ok.status_code == 201, ok.text


async def test_a_failing_folder_decision_makes_member_routes_the_uniform_404(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        group = await stack.local_group(stack.root_id, "g-uniform")
        erin, _ = stack.token("erin-uniform")

        with stack.flaky_authz(fail_permissions=frozenset({"folder:read"})):
            existing = await stack.link_member(card["id"], "local_group", group, token=erin)
            unknown = await stack.link_member(UNKNOWN, "local_group", group, token=erin)
            creator = await stack.link_member(card["id"], "local_group", group, token=manager)
        for response in (existing, unknown, creator):
            assert_problem(response, 404, "not-found")
        assert stable(existing) == stable(unknown) == stable(creator)


async def test_unlinking_is_audited_once_and_takes_access_away_at_once(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        group = await stack.local_group(stack.root_id, "g-unlink")
        dana, dana_principal = stack.token("dana-unlink")
        erin, _ = stack.token("erin-unlink")
        stack.group_member_sql(group, dana_principal)
        assert (await stack.link_member(card["id"], "local_group", group, token=manager)).is_success
        url = member_url(card["id"], "local_group", group)

        by_member = await stack.client.delete(url, headers=bearer(dana))
        assert_problem(by_member, 403, "forbidden")
        by_outsider = await stack.client.delete(url, headers=bearer(erin))
        assert_problem(by_outsider, 404, "not-found")
        assert len(member_rows(sql, stack, card["id"])) == 1

        removed = await stack.client.delete(url, headers=bearer(manager))
        assert removed.status_code == 204, removed.text
        assert removed.content == b""
        gone = events(sql, stack, "card.member_removed")
        assert len(gone) == 1
        assert gone[0]["before"] == {"member_type": "local_group", "member_id": group}
        assert gone[0]["after"] is None
        assert gone[0]["aggregate_id"] == card["id"]
        assert member_rows(sql, stack, card["id"]) == []
        lost = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(dana))
        assert_problem(lost, 404, "not-found")

        again = await stack.client.delete(url, headers=bearer(manager))
        assert again.status_code == 204
        assert len(events(sql, stack, "card.member_removed")) == 1
        bad_type = await stack.client.delete(
            member_url(card["id"], "user", "x"), headers=bearer(manager)
        )
        assert_problem(bad_type, 422, "validation-error")
        anonymous = await stack.client.delete(url)
        assert anonymous.status_code == 401


async def test_two_concurrent_links_of_one_member_yield_one_row_and_one_event(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        _, account = await stack.service_account_key(stack.root_id, "racer")

        first, second = await asyncio.gather(
            stack.link_member(card["id"], "service_account", account, token=manager),
            stack.link_member(card["id"], "service_account", account, token=manager),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 201]
        assert first.json()["member_id"] == second.json()["member_id"] == account
        assert len(member_rows(sql, stack, card["id"])) == 1
        assert len(events(sql, stack, "card.member_added")) == 1


async def test_an_archived_card_refuses_member_changes(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        group = await stack.local_group(stack.root_id, "g-archived")
        assert (await stack.link_member(card["id"], "local_group", group, token=manager)).is_success
        archived = await stack.client.post(f"{CARDS}/{card['id']}/archive", headers=bearer(manager))
        assert archived.status_code == 200, archived.text

        other = await stack.local_group(stack.root_id, "g-archived-2")
        link = await stack.link_member(card["id"], "local_group", other, token=manager)
        assert_problem(link, 409, "card-archived")
        unlink = await stack.client.delete(
            member_url(card["id"], "local_group", group), headers=bearer(manager)
        )
        assert_problem(unlink, 409, "card-archived")
        assert len(member_rows(sql, stack, card["id"])) == 1


async def test_the_card_lists_its_members_ordered_by_type_then_id(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        _, card, manager = await _card_with_creator(stack)
        _, account = await stack.service_account_key(stack.root_id, "listed-bot")
        groups = [await stack.local_group(stack.root_id, f"listed-{i}") for i in range(3)]
        for member_type, member_id in [
            ("service_account", account),
            ("local_group", groups[2]),
            ("local_group", groups[0]),
            ("local_group", groups[1]),
        ]:
            response = await stack.link_member(card["id"], member_type, member_id, token=manager)
            assert response.status_code == 201, response.text

        fetched = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(manager))
        members = fetched.json()["members"]
        assert [(m["member_type"], m["member_id"]) for m in members] == [
            *[("local_group", g) for g in sorted(groups)],
            ("service_account", account),
        ]
        assert all(m["added_by"] == card["created_by"] and m["added_at"] for m in members)
