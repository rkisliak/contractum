"""The all-cards list (``GET /api/v1/contracts/cards``): every card the caller could open by id.

Rows and totals are trimmed to folder readers and linked members, so a caller without access sees
an empty page and the counts never include a hidden card. ``folder_id`` with ``subtree`` (default)
lists a folder and everything below it, so an Organization shows its teams' cards.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def index(stack: Any, token: str, **params: Any) -> dict[str, Any]:
    response = await stack.client.get(CARDS, params=params, headers=bearer(token))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def names(body: dict[str, Any]) -> set[str]:
    return {item["name"] for item in body["items"]}


async def test_the_list_is_trimmed_to_readable_folders_and_counts_exclude_hidden_cards(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        org_a = await stack.chain()
        org_b = await stack.chain()
        second_team_a = await stack.team_in_org(org_a.org, "second")
        tag = uuid.uuid4().hex[:6]
        a1 = await stack.create_card(org_a.team, name=f"a1-{tag}")
        a2 = await stack.create_card(second_team_a, name=f"a2-{tag}")
        await stack.create_card(org_a.project, name=f"ap-{tag}")
        await stack.create_card(org_b.team, name=f"b1-{tag}")

        reader, reader_principal = stack.token(f"reader-{tag}")
        stack.grant_sql(reader_principal, "org_reader", org_a.org)
        mine = await index(stack, reader)
        assert names(mine) == {f"a1-{tag}", f"a2-{tag}", f"ap-{tag}"}
        assert mine["total_cards"] == 3
        assert mine["total_folders"] == 3
        assert mine["next_cursor"] is None
        row = next(item for item in mine["items"] if item["id"] == a1["id"])
        assert row["folder_id"] == org_a.team
        assert row["folder_ancestor_ids"] == [stack.root_id, org_a.org, org_a.project, org_a.team]
        assert row["organization_id"] == org_a.org
        assert row["active_subscription_count"] == 0
        assert row["subscriptions"] == []
        assert mine["pin_preview_limit"] == 5

        # Another organization's folder is an empty page, never a 403 or a count.
        other = await index(stack, reader, folder_id=org_b.org)
        assert other == {
            "items": [],
            "next_cursor": None,
            "total_cards": 0,
            "total_folders": 0,
            "pin_preview_limit": 5,
        }

        nobody, _ = stack.token(f"nobody-{tag}")
        empty = await index(stack, nobody)
        assert empty["items"] == []
        assert empty["total_cards"] == 0

        admin = await index(stack, stack.admin_token, q=tag)
        assert names(admin) == {f"a1-{tag}", f"a2-{tag}", f"ap-{tag}", f"b1-{tag}"}
        assert admin["total_cards"] == 4

        assert a2["folder_id"] == second_team_a
        anonymous = await stack.client.get(CARDS)
        assert anonymous.status_code == 401


async def test_folder_scope_with_and_without_subtree(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        tag = uuid.uuid4().hex[:6]
        await stack.create_card(chain.team, name=f"team-{tag}")
        await stack.create_card(chain.project, name=f"project-{tag}")

        under_org = await index(stack, stack.admin_token, folder_id=chain.org)
        assert names(under_org) == {f"team-{tag}", f"project-{tag}"}
        assert under_org["total_folders"] == 2
        only_org = await index(stack, stack.admin_token, folder_id=chain.org, subtree="false")
        assert only_org["items"] == []
        only_project = await index(
            stack, stack.admin_token, folder_id=chain.project, subtree="false"
        )
        assert names(only_project) == {f"project-{tag}"}


async def test_keyset_paging_and_the_search_filter(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        tag = uuid.uuid4().hex[:6]
        for n in range(5):
            await stack.create_card(
                chain.team, name=f"page-{tag}-{n}", description="match-me" if n == 4 else "d"
            )

        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"folder_id": chain.org, "limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = await index(stack, stack.admin_token, **params)
            assert len(page["items"]) <= 2
            assert page["total_cards"] == 5
            seen.extend(item["name"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == [f"page-{tag}-{n}" for n in range(5)]

        described = await index(stack, stack.admin_token, folder_id=chain.org, q="MATCH-me")
        assert names(described) == {f"page-{tag}-4"}
        wildcard = await index(stack, stack.admin_token, folder_id=chain.org, q="%")
        assert wildcard["items"] == []

        bad = await stack.client.get(
            CARDS, params={"cursor": "not-a-cursor"}, headers=bearer(stack.admin_token)
        )
        assert bad.status_code == 422


async def test_archived_and_active_subscription_filters_and_the_pin_preview(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        org_a = await stack.chain()
        org_b = await stack.chain()
        contract, _ = await stack.publish_versions(org_a.team, ["1.0.0"])
        tag = uuid.uuid4().hex[:6]
        pinned = await stack.create_card(org_b.team, name=f"pinned-{tag}")
        idle = await stack.create_card(org_b.team, name=f"idle-{tag}")
        gone = await stack.create_card(org_b.team, name=f"gone-{tag}")
        subscribed = await stack.subscribe(pinned["id"], contract, "1.0.0")
        assert subscribed.status_code == 201, subscribed.text
        archived = await stack.client.post(
            f"{CARDS}/{gone['id']}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code == 200, archived.text

        default = await index(stack, stack.admin_token, folder_id=org_b.org)
        assert names(default) == {f"pinned-{tag}", f"idle-{tag}"}
        with_archived = await index(
            stack, stack.admin_token, folder_id=org_b.org, include_archived="true"
        )
        assert names(with_archived) == {f"pinned-{tag}", f"idle-{tag}", f"gone-{tag}"}
        active = await index(
            stack, stack.admin_token, folder_id=org_b.org, has_active_subscriptions="true"
        )
        assert names(active) == {f"pinned-{tag}"}
        row = active["items"][0]
        assert row["active_subscription_count"] == 1
        [pin] = row["subscriptions"]
        assert pin["contract_id"] == contract
        assert pin["semver"] == "1.0.0"
        assert pin["contract_name"] is not None
        inactive = await index(
            stack, stack.admin_token, folder_id=org_b.org, has_active_subscriptions="false"
        )
        assert names(inactive) == {f"idle-{tag}"}
        assert idle["id"] in {item["id"] for item in inactive["items"]}

        # A reader of Org B sees the pin, but not the name of a contract in Org A.
        reader, reader_principal = stack.token(f"b-reader-{tag}")
        stack.grant_sql(reader_principal, "org_reader", org_b.org)
        theirs = await index(stack, reader, has_active_subscriptions="true")
        [their_row] = theirs["items"]
        assert their_row["subscriptions"][0]["contract_id"] == contract
        assert their_row["subscriptions"][0]["contract_name"] is None


async def test_a_linked_member_sees_the_card_without_folder_access(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        tag = uuid.uuid4().hex[:6]
        card = await stack.create_card(chain.team, name=f"member-{tag}")
        await stack.create_card(chain.team, name=f"other-{tag}")
        group = await stack.local_group(stack.root_id, f"g-{tag}")
        linked = await stack.link_member(card["id"], "local_group", group)
        assert linked.status_code == 201, linked.text
        member, member_principal = stack.token(f"member-{tag}")
        stack.group_member_sql(group, member_principal)

        body = await index(stack, member)
        assert names(body) == {f"member-{tag}"}
        assert body["total_cards"] == 1


async def test_an_identity_outage_is_503_not_an_unfiltered_page(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        await stack.create_card(chain.team)
        with stack.flaky_authz(fail_grantees=True):
            response = await stack.client.get(CARDS, headers=bearer(stack.admin_token))
        assert response.status_code == 503
        assert response.json()["type"] == "urn:dc:problem:service-unavailable"
