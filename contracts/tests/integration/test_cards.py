"""Subscriber cards (SUB-01, SUB-04): create, read, edit, archive and list (D-82, D-97, D-104).

A card belongs to a folder of any kind (root, organization, project or team). A Consumer (or an
admin above) creates it; the creator and the folder's admins change it; everyone who can read the
folder reads it; nobody else learns that it exists (one uniform 404, also while identity is
failing, D-35, D-62).
"""

from __future__ import annotations

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


def folder_cards(folder_id: str) -> str:
    return f"/api/v1/contracts/folders/{folder_id}/cards"


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


def create_card(stack: Any, folder_id: str, **kwargs: Any) -> Any:
    """POST a card and assert 201; returns the body (the shared ``Stack.create_card``)."""
    return stack.create_card(folder_id, **kwargs)


def flaky_authz(stack: Any, *, fail_permissions: frozenset[str]) -> Any:
    """Make the listed permission checks fail (the shared ``Stack.flaky_authz``)."""
    return stack.flaky_authz(fail_permissions=fail_permissions)


# --- tracer: a Consumer creates a card in a team folder and reads it back ----------------------


async def test_a_consumer_creates_a_card_in_a_team_folder_and_reads_it_back(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        token, principal = stack.token("cons")
        stack.grant_sql(principal, "consumer", chain.team)

        response = await stack.client.post(
            folder_cards(chain.team),
            json={
                "name": "orders-ingest",
                "description": "Nightly ingest",
                "contacts": [
                    {"kind": "email", "value": "team@example.com"},
                    {"name": "On-call", "kind": "email", "value": "oncall@example.com"},
                ],
            },
            headers=bearer(token),
        )
        assert response.status_code == 201, response.text
        card = response.json()
        assert response.headers["location"] == f"{CARDS}/{card['id']}"
        assert card["folder_id"] == chain.team
        assert card["name"] == "orders-ingest"
        assert card["contacts"] == [
            {"name": None, "kind": "email", "value": "team@example.com"},
            {"name": "On-call", "kind": "email", "value": "oncall@example.com"},
        ]
        assert card["email_enabled"] is False
        assert card["webhook_enabled"] is False
        assert card["webhook_url"] is None
        assert card["archived_at"] is None

        events = sql(
            stack.contracts_db_url,
            "SELECT action, aggregate_type, aggregate_id, scope_path, after FROM outbox "
            "WHERE action LIKE 'card.%%'",
        )
        assert len(events) == 1
        event = events[0]
        assert event["action"] == "card.created"
        assert event["aggregate_type"] == "card"
        assert event["aggregate_id"] == card["id"]
        assert [str(i) for i in event["scope_path"]] == [
            stack.root_id,
            chain.org,
            chain.project,
            chain.team,
        ]
        assert event["after"]["name"] == "orders-ingest"
        assert len(event["after"]["contacts"]) == 2
        assert "webhook_url" not in event["after"]

        mine = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(token))
        assert mine.status_code == 200, mine.text
        assert mine.json() == card
        admin = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(stack.admin_token))
        assert admin.status_code == 200

        outsider, _ = stack.token("nobody")
        hidden = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(outsider))
        unknown = await stack.client.get(f"{CARDS}/{UNKNOWN}", headers=bearer(outsider))
        assert_problem(hidden, 404, "not-found")
        assert stable(hidden) == stable(unknown)


async def test_cards_live_in_folders_of_every_kind_and_need_card_create(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        token, principal = stack.token("cons-p")
        stack.grant_sql(principal, "consumer", chain.project)
        created = await stack.client.post(
            folder_cards(chain.project),
            json={"name": "p-card", "description": "d"},
            headers=bearer(token),
        )
        assert created.status_code == 201, created.text
        assert created.json()["folder_id"] == chain.project

        org_admin, org_principal = stack.token("orgadm")
        stack.grant_sql(org_principal, "org_admin", chain.org)
        in_org = await stack.client.post(
            folder_cards(chain.org),
            json={"name": "o-card", "description": "d"},
            headers=bearer(org_admin),
        )
        assert in_org.status_code == 201, in_org.text
        in_root = await stack.client.post(
            folder_cards(stack.root_id),
            json={"name": "r-card", "description": "d"},
            headers=bearer(stack.admin_token),
        )
        assert in_root.status_code == 201, in_root.text
        assert in_root.json()["folder_id"] == stack.root_id
        stored = {
            str(r["folder_id"]): r["organization_id"]
            for r in sql(
                stack.contracts_db_url,
                "SELECT folder_id, organization_id FROM subscriber_card WHERE name IN "
                "('o-card', 'r-card', 'p-card')",
            )
        }
        assert str(stored[chain.org]) == chain.org
        assert str(stored[chain.project]) == chain.org
        assert stored[stack.root_id] is None

        editor, editor_principal = stack.token("editor")
        stack.grant_sql(editor_principal, "team_editor", chain.team)
        forbidden = await stack.client.post(
            folder_cards(chain.team),
            json={"name": "e-card", "description": "d"},
            headers=bearer(editor),
        )
        assert_problem(forbidden, 403, "forbidden")

        stranger, _ = stack.token("stranger")
        hidden = await stack.client.post(
            folder_cards(chain.team),
            json={"name": "s-card", "description": "d"},
            headers=bearer(stranger),
        )
        assert_problem(hidden, 404, "not-found")

        anonymous = await stack.client.post(
            folder_cards(chain.team), json={"name": "a-card", "description": "d"}
        )
        assert anonymous.status_code == 401


async def test_an_identity_outage_is_503_for_every_card_id_and_never_reveals_existence(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        card = await create_card(stack, chain.team)
        outsider, _ = stack.token("outsider")

        stack.identity_link.down = True
        try:
            known = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(outsider))
            unknown = await stack.client.get(f"{CARDS}/{UNKNOWN}", headers=bearer(outsider))
        finally:
            stack.identity_link.down = False
        assert_problem(known, 503, "service-unavailable")
        assert_problem(unknown, 503, "service-unavailable")
        assert stable(known) == stable(unknown)


async def test_a_failing_folder_decision_after_the_card_is_loaded_fails_closed_to_the_uniform_404(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        token, principal = stack.token("creator")
        stack.grant_sql(principal, "consumer", chain.team)
        card = await create_card(stack, chain.team, token=token)
        outsider, _ = stack.token("outsider2")

        with flaky_authz(stack, fail_permissions=frozenset({"folder:read"})):
            existing = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(outsider))
            unknown = await stack.client.get(f"{CARDS}/{UNKNOWN}", headers=bearer(outsider))
            creator = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(token))
        for response in (existing, unknown, creator):
            assert_problem(response, 404, "not-found")
        assert stable(existing) == stable(unknown) == stable(creator)

        # the failure injection is gone: the creator reads the card again
        again = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(token))
        assert again.status_code == 200


# --- contacts, names and descriptions are validated and never echoed ---------------------------


def _post(stack: Any, folder: str, body: dict[str, Any], token: str | None = None) -> Any:
    return stack.client.post(
        folder_cards(folder), json=body, headers=bearer(token or stack.admin_token)
    )


async def test_valid_contacts_are_stored_and_returned_exactly_as_submitted(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contacts = [
            {"name": None, "kind": "email", "value": "a.b+c@sub.example.com"},
            {"name": "Mixed", "kind": "email", "value": "Mixed.Case@Example.COM"},
            {"name": None, "kind": "email", "value": "second@example.com"},
            {"name": None, "kind": "url", "value": "https://Teams.Microsoft.com/l/channel/x?Q=1"},
            {"name": "Chat", "kind": "text", "value": "  Slack #orders  "},
        ]
        card = await create_card(
            stack, chain.team, contacts=contacts, description="line1\nline2\tend"
        )
        assert card["contacts"] == contacts
        assert card["description"] == "line1\nline2\tend"
        fetched = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(stack.admin_token))
        assert fetched.json()["contacts"] == contacts


async def test_contacts_may_be_omitted_or_empty(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        omitted = await _post(stack, chain.team, {"name": "no-contacts", "description": "d"})
        assert omitted.status_code == 201, omitted.text
        assert omitted.json()["contacts"] == []
        empty = await _post(
            stack, chain.team, {"name": "empty-contacts", "description": "d", "contacts": []}
        )
        assert empty.status_code == 201, empty.text
        assert empty.json()["contacts"] == []


MARKER = "zzmarkerzz"


async def test_invalid_contacts_names_and_descriptions_are_422_and_never_echo_the_value(
    make_stack: MakeStack,
) -> None:
    long_email = "a" * 64 + "@" + "b" * 257 + ".com"
    cases: list[tuple[str, dict[str, Any], str | None]] = [
        ("email without at", {"contacts": [{"kind": "email", "value": MARKER}]}, MARKER),
        ("email without dot", {"contacts": [{"kind": "email", "value": f"a@{MARKER}"}]}, MARKER),
        (
            "email with space",
            {"contacts": [{"kind": "email", "value": f"a b@{MARKER}.example.com"}]},
            MARKER,
        ),
        ("email over 320", {"contacts": [{"kind": "email", "value": long_email}]}, "bbbbbbbbbb"),
        ("url ftp", {"contacts": [{"kind": "url", "value": f"ftp://{MARKER}"}]}, MARKER),
        (
            "url javascript",
            {"contacts": [{"kind": "url", "value": f"javascript:{MARKER}"}]},
            MARKER,
        ),
        ("url without host", {"contacts": [{"kind": "url", "value": "https://"}]}, None),
        (
            "text with NUL",
            {"contacts": [{"kind": "text", "value": f"{MARKER}\x00"}]},
            MARKER,
        ),
        (
            "text with ESC",
            {"contacts": [{"kind": "text", "value": f"{MARKER}\x1b[0m"}]},
            MARKER,
        ),
        ("text over 500", {"contacts": [{"kind": "text", "value": "t" * 501}]}, "tttttttttt"),
        ("unknown kind", {"contacts": [{"kind": MARKER, "value": "x"}]}, MARKER),
        ("extra contact field", {"contacts": [{"kind": "text", "value": "x", "extra": 1}]}, None),
        ("21 contacts", {"contacts": [{"kind": "text", "value": "x"}] * 21}, None),
        ("name over 200", {"name": "x" * 201}, "xxxxxxxxxx"),
        ("name whitespace only", {"name": "   "}, None),
        ("name with control char", {"name": f"{MARKER}\x07"}, MARKER),
        ("description empty", {"description": ""}, None),
        ("description whitespace only", {"description": " \n\t "}, None),
        ("description with BEL", {"description": f"{MARKER}\x07"}, MARKER),
        ("description over 2000", {"description": "d" * 2001}, "dddddddddd"),
        ("unknown top-level field", {"mystery": 1}, None),
    ]
    async with make_stack() as stack:
        chain = await stack.chain()
        for label, override, marker in cases:
            body = {"name": f"case-{uuid.uuid4().hex[:8]}", "description": "d", **override}
            response = await _post(stack, chain.team, body)
            assert response.status_code == 422, (label, response.text)
            problem = assert_problem(response, 422, "validation-error")
            assert problem["errors"], label
            if marker is not None:
                assert marker not in response.text, label
        listed = await stack.client.get(folder_cards(chain.team), headers=bearer(stack.admin_token))
        assert listed.json() == {"items": [], "next_cursor": None}


# --- names are unique per folder among active cards ---------------------------------------------


async def test_card_names_are_unique_per_folder_case_insensitively_and_reusable_after_archive(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other_team = await stack.team_in_org(chain.org, "other")
        first = await create_card(stack, chain.team, name="Orders")
        taken = await _post(stack, chain.team, {"name": "oRDERS", "description": "d"})
        assert_problem(taken, 409, "card-name-taken")
        elsewhere = await _post(stack, other_team, {"name": "Orders", "description": "d"})
        assert elsewhere.status_code == 201, elsewhere.text

        second = await create_card(stack, chain.team, name="Payments")
        clash = await stack.client.patch(
            f"{CARDS}/{second['id']}",
            json={"name": "ORDERS"},
            headers=bearer(stack.admin_token),
        )
        assert_problem(clash, 409, "card-name-taken")

        archived = await stack.client.post(
            f"{CARDS}/{first['id']}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code == 200, archived.text
        reused = await _post(stack, chain.team, {"name": "Orders", "description": "d"})
        assert reused.status_code == 201, reused.text


async def test_two_concurrent_creates_of_one_name_yield_one_201_and_one_409(
    make_stack: MakeStack, sql: Sql
) -> None:
    import asyncio

    async with make_stack() as stack:
        chain = await stack.chain()
        responses = await asyncio.gather(
            *[_post(stack, chain.team, {"name": "Race", "description": "d"}) for _ in range(2)]
        )
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201, 409], [r.text for r in responses]
        loser = next(r for r in responses if r.status_code == 409)
        assert_problem(loser, 409, "card-name-taken")
        rows = sql(
            stack.contracts_db_url,
            "SELECT count(*) AS n FROM subscriber_card WHERE lower(name) = 'race' "
            "AND archived_at IS NULL",
        )
        assert rows[0]["n"] == 1


# --- edit and archive: the creator and the folder's admins -------------------------------------


def _card_events(sql: Sql, stack: Any) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_id, before, after FROM outbox WHERE action LIKE 'card.%%' "
        "ORDER BY id",
    )


async def test_the_creator_and_the_folders_admins_edit_a_card_with_one_audited_write(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        creator, creator_principal = stack.token("creator")
        stack.grant_sql(creator_principal, "consumer", chain.team)
        card = await create_card(stack, chain.team, token=creator, description="old")
        url = f"{CARDS}/{card['id']}"

        edited = await stack.client.patch(url, json={"description": "new"}, headers=bearer(creator))
        assert edited.status_code == 200, edited.text
        assert edited.json()["description"] == "new"
        events = _card_events(sql, stack)
        assert [e["action"] for e in events] == ["card.created", "card.updated"]
        assert events[1]["aggregate_id"] == card["id"]
        assert events[1]["before"]["description"] == "old"
        assert events[1]["after"]["description"] == "new"

        team_admin, team_admin_principal = stack.token("tadmin")
        stack.grant_sql(team_admin_principal, "team_admin", chain.team)
        by_team_admin = await stack.client.patch(
            url, json={"email_enabled": True}, headers=bearer(team_admin)
        )
        assert by_team_admin.status_code == 200, by_team_admin.text
        assert by_team_admin.json()["email_enabled"] is True

        org_admin, org_admin_principal = stack.token("oadmin")
        stack.grant_sql(org_admin_principal, "org_admin", chain.org)
        by_org_admin = await stack.client.patch(
            url,
            json={"name": "renamed", "contacts": [{"kind": "text", "value": "ops"}]},
            headers=bearer(org_admin),
        )
        assert by_org_admin.status_code == 200, by_org_admin.text
        assert by_org_admin.json()["name"] == "renamed"
        assert by_org_admin.json()["contacts"] == [{"name": None, "kind": "text", "value": "ops"}]
        assert len(_card_events(sql, stack)) == 4


async def test_a_folder_reader_who_is_neither_creator_nor_admin_is_forbidden_and_an_outsider_404(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        card = await create_card(stack, chain.team)
        url = f"{CARDS}/{card['id']}"
        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", chain.team)

        patched = await stack.client.patch(url, json={"description": "x"}, headers=bearer(reader))
        assert_problem(patched, 403, "forbidden")
        archived = await stack.client.post(f"{url}/archive", headers=bearer(reader))
        assert_problem(archived, 403, "forbidden")
        assert (await stack.client.get(url, headers=bearer(reader))).status_code == 200

        outsider, _ = stack.token("outsider3")
        hidden = await stack.client.patch(url, json={"description": "x"}, headers=bearer(outsider))
        unknown = await stack.client.patch(
            f"{CARDS}/{UNKNOWN}", json={"description": "x"}, headers=bearer(outsider)
        )
        assert_problem(hidden, 404, "not-found")
        assert stable(hidden) == stable(unknown)
        hidden_archive = await stack.client.post(f"{url}/archive", headers=bearer(outsider))
        assert_problem(hidden_archive, 404, "not-found")
        # no token: 401 on every card route, whatever the body
        assert (await stack.client.patch(url, json={"description": "x"})).status_code == 401
        assert (await stack.client.post(f"{url}/archive")).status_code == 401
        assert (await stack.client.get(url)).status_code == 401
        assert (await stack.client.get(folder_cards(chain.team))).status_code == 401
        assert [e["action"] for e in _card_events(sql, stack)] == ["card.created"]


async def test_an_unchanged_patch_and_a_second_archive_are_200_no_ops_without_events(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        creator, principal = stack.token("creator2")
        stack.grant_sql(principal, "consumer", chain.team)
        card = await create_card(stack, chain.team, token=creator, name="Same", description="d")
        url = f"{CARDS}/{card['id']}"

        for body in ({}, {"name": "Same"}, {"description": "d", "email_enabled": False}):
            response = await stack.client.patch(url, json=body, headers=bearer(creator))
            assert response.status_code == 200, response.text
        assert [e["action"] for e in _card_events(sql, stack)] == ["card.created"]

        first = await stack.client.post(f"{url}/archive", headers=bearer(creator))
        assert first.status_code == 200, first.text
        assert first.json()["archived_at"] is not None
        assert first.json()["archived_by"] == principal
        second = await stack.client.post(f"{url}/archive", headers=bearer(creator))
        assert second.status_code == 200, second.text
        assert [e["action"] for e in _card_events(sql, stack)] == ["card.created", "card.archived"]

        refused = await stack.client.patch(url, json={"description": "x"}, headers=bearer(creator))
        assert_problem(refused, 409, "card-archived")


async def test_an_archived_folder_refuses_manager_writes(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        creator, principal = stack.token("creator3")
        stack.grant_sql(principal, "consumer", chain.team)
        card = await create_card(stack, chain.team, token=creator)
        frozen = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.team}/archive", headers=bearer(stack.admin_token)
        )
        assert frozen.status_code == 200, frozen.text
        refused = await stack.client.patch(
            f"{CARDS}/{card['id']}", json={"description": "x"}, headers=bearer(creator)
        )
        assert_problem(refused, 409, "folder-archived")
        refused_archive = await stack.client.post(
            f"{CARDS}/{card['id']}/archive", headers=bearer(creator)
        )
        assert_problem(refused_archive, 409, "folder-archived")


async def test_a_creator_who_lost_the_folder_grant_can_no_longer_edit_the_card(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        creator, principal = stack.token("creator4")
        assignment = stack.grant_sql(principal, "consumer", chain.team)
        card = await create_card(stack, chain.team, token=creator)
        sql(stack.identity_db_url, "DELETE FROM role_assignments WHERE id = %s", assignment)
        refused = await stack.client.patch(
            f"{CARDS}/{card['id']}", json={"description": "x"}, headers=bearer(creator)
        )
        assert_problem(refused, 404, "not-found")
        gone = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(creator))
        assert_problem(gone, 404, "not-found")


# --- list ------------------------------------------------------------------------------------


async def test_a_folder_reader_lists_cards_in_id_order_with_a_keyset_cursor(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        empty = await stack.client.get(folder_cards(chain.team), headers=bearer(stack.admin_token))
        assert empty.status_code == 200
        assert empty.json() == {"items": [], "next_cursor": None}

        first = await create_card(stack, chain.team, name="one")
        second = await create_card(stack, chain.team, name="two")
        third = await create_card(stack, chain.team, name="three")
        await stack.client.post(f"{CARDS}/{third['id']}/archive", headers=bearer(stack.admin_token))

        reader, reader_principal = stack.token("lister")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        page_one = await stack.client.get(
            folder_cards(chain.team), params={"limit": 1}, headers=bearer(reader)
        )
        assert page_one.status_code == 200, page_one.text
        body = page_one.json()
        assert [item["id"] for item in body["items"]] == [first["id"]]
        assert body["next_cursor"] is not None
        assert set(body["items"][0]) >= {"id", "name", "contacts", "email_enabled", "archived_at"}
        page_two = await stack.client.get(
            folder_cards(chain.team),
            params={"limit": 1, "cursor": body["next_cursor"]},
            headers=bearer(reader),
        )
        assert [item["id"] for item in page_two.json()["items"]] == [second["id"]]
        assert page_two.json()["next_cursor"] is None

        with_archived = await stack.client.get(
            folder_cards(chain.team), params={"include_archived": "true"}, headers=bearer(reader)
        )
        assert [item["id"] for item in with_archived.json()["items"]] == [
            first["id"],
            second["id"],
            third["id"],
        ]

        bad_cursor = await stack.client.get(
            folder_cards(chain.team), params={"cursor": "not-a-cursor"}, headers=bearer(reader)
        )
        assert bad_cursor.status_code == 422

        outsider, _ = stack.token("outsider4")
        hidden = await stack.client.get(folder_cards(chain.team), headers=bearer(outsider))
        assert_problem(hidden, 404, "not-found")


# --- cards above team level (quick 261119-wo3) --------------------------------------------------


async def test_an_organization_card_is_read_by_its_organization_and_hidden_from_another(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        other = await stack.chain()
        cons, cons_principal = stack.token("org-cons")
        stack.grant_sql(cons_principal, "consumer", chain.org)
        card = await create_card(stack, chain.org, token=cons, name="org-level")
        assert card["folder_id"] == chain.org

        reader, reader_principal = stack.token("org-reader")
        stack.grant_sql(reader_principal, "org_reader", chain.org)
        seen = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(reader))
        assert seen.status_code == 200, seen.text
        listed = await stack.client.get(CARDS, headers=bearer(reader))
        assert listed.status_code == 200, listed.text
        item = next(i for i in listed.json()["items"] if i["id"] == card["id"])
        assert item["organization_id"] == chain.org

        foreign, foreign_principal = stack.token("other-org-reader")
        stack.grant_sql(foreign_principal, "org_reader", other.org)
        hidden = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(foreign))
        assert_problem(hidden, 404, "not-found")
        not_listed = await stack.client.get(CARDS, headers=bearer(foreign))
        assert card["id"] not in {i["id"] for i in not_listed.json()["items"]}

        # The creator edits and archives it; a reader cannot.
        edited = await stack.client.patch(
            f"{CARDS}/{card['id']}", json={"description": "changed"}, headers=bearer(cons)
        )
        assert edited.status_code == 200, edited.text
        refused = await stack.client.patch(
            f"{CARDS}/{card['id']}", json={"description": "nope"}, headers=bearer(reader)
        )
        assert refused.status_code == 403, refused.text
        archived = await stack.client.post(f"{CARDS}/{card['id']}/archive", headers=bearer(cons))
        assert archived.status_code == 200, archived.text


async def test_a_root_card_has_no_organization_and_is_managed_by_a_global_admin(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        card = await create_card(stack, stack.root_id, name="root-level")
        assert card["folder_id"] == stack.root_id

        listed = await stack.client.get(CARDS, headers=bearer(stack.admin_token))
        item = next(i for i in listed.json()["items"] if i["id"] == card["id"])
        assert item["organization_id"] is None
        assert item["folder_ancestor_ids"] == [stack.root_id]

        # A reader of one organization does not read a card owned by the ROOT folder.
        reader, reader_principal = stack.token("org-reader-r")
        stack.grant_sql(reader_principal, "org_reader", chain.org)
        hidden = await stack.client.get(f"{CARDS}/{card['id']}", headers=bearer(reader))
        assert_problem(hidden, 404, "not-found")

        edited = await stack.client.patch(
            f"{CARDS}/{card['id']}",
            json={"description": "changed"},
            headers=bearer(stack.admin_token),
        )
        assert edited.status_code == 200, edited.text
        archived = await stack.client.post(
            f"{CARDS}/{card['id']}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code == 200, archived.text


async def test_an_organization_card_subscribes_to_a_contract_published_in_that_organization(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await stack.publish_versions(chain.team, ["1.0.0"])
        card = await create_card(stack, chain.org, name="org-subscriber")
        pinned = await stack.subscribe(card["id"], contract, "1.0.0")
        assert pinned.status_code == 201, pinned.text

        root_card = await create_card(stack, stack.root_id, name="root-subscriber")
        root_pin = await stack.subscribe(root_card["id"], contract, "1.0.0")
        assert root_pin.status_code == 201, root_pin.text
