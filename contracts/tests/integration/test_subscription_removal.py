"""Owner-side removal of a card's pin (SUB-06 removal half, D-94, D-99 as amended).

Anyone holding ``contract:publish`` on the contract ends a card's pin with a mandatory reason. The
request writes one ``card.subscription_removed`` audit event and no notice: the event is the
notice's carrier, and handling it (``Stack.pump_notices`` here, the JetStream consumer live) gives
the card exactly one ``card.subscription.removed`` notice, however often it is delivered.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
REASON = "Retiring 3.0.0, please move to 3.1.0"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def remove_url(contract: str, sub: str) -> str:
    return f"/api/v1/contracts/contracts/{contract}/subscriptions/{sub}/remove"


async def remove(
    stack: Any, contract: str, sub: str, token: str | None = None, **body: Any
) -> httpx.Response:
    return await stack.client.post(
        remove_url(contract, sub),
        json=body if body else {"reason": REASON},
        headers=bearer(token or stack.admin_token),
    )


def stable(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    return {k: body.get(k) for k in ("type", "title", "status", "detail")}


def removal_events(sql: Sql, stack: Any) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT event_id, aggregate_type, aggregate_id, scope_path, before, after, actor "
        "FROM outbox WHERE action = 'card.subscription_removed' ORDER BY id",
    )


def notices_of(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM card_notice WHERE card_id = %s AND event_type = 'card.subscription.removed' "
        "ORDER BY id",
        card_id,
    )


def pin_row(sql: Sql, stack: Any, sub: str) -> dict[str, Any]:
    rows = sql(stack.contracts_db_url, "SELECT * FROM card_subscription WHERE id = %s", sub)
    assert len(rows) == 1
    return rows[0]


async def _world(stack: Any, sql: Sql) -> dict[str, Any]:
    """orders 2.3.0 and 3.0.0 in Org A (a team editor `edith` is on its team); card X in Org B.

    X (email on) pins 3.0.0; card Y in Org A pins 2.3.0. `other` is a second Org A contract that
    X also pins.
    """
    org_a = await stack.chain()
    org_b = await stack.chain()
    contract, versions = await stack.publish_versions(org_a.team, ["2.3.0", "3.0.0"])
    other, _ = await stack.publish_versions(org_a.team, ["1.0.0"])
    card_x = await stack.create_card(org_b.team, email_enabled=True)
    card_y = await stack.create_card(org_a.team)
    pins: dict[str, str] = {}
    for key, card, target, semver in (
        ("x300", card_x, contract, "3.0.0"),
        ("y230", card_y, contract, "2.3.0"),
        ("x_other", card_x, other, "1.0.0"),
    ):
        pinned = await stack.subscribe(card["id"], target, semver)
        assert pinned.status_code == 201, pinned.text
        pins[key] = pinned.json()["id"]
    editor, editor_principal = stack.token("edith")
    stack.grant_sql(editor_principal, "team_editor", org_a.team)
    return {
        "org_a": org_a,
        "org_b": org_b,
        "contract": contract,
        "other": other,
        "versions": versions,
        "x": card_x,
        "y": card_y,
        "pins": pins,
        "editor": editor,
        "editor_principal": editor_principal,
    }


# --- a publisher removes a pin with a reason, the card is told through the removal event --------


async def test_an_editor_removes_a_pin_with_a_reason_and_the_card_is_notified_by_the_event(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        sub = w["pins"]["x300"]

        response = await remove(stack, w["contract"], sub, w["editor"])
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == sub
        assert body["card_id"] == w["x"]["id"]
        assert body["contract_id"] == w["contract"]
        assert body["version_id"] == w["versions"]["3.0.0"]
        assert body["semver"] == "3.0.0"
        assert body["end_reason"] == "removed_by_admin"
        assert body["end_note"] == REASON
        assert body["ended_by"] == w["editor_principal"]
        assert body["ended_at"]
        assert body["replaced_by_id"] is None

        row = pin_row(sql, stack, sub)
        assert row["end_reason"] == "removed_by_admin"
        assert row["end_note"] == REASON
        assert row["ended_by"] == w["editor_principal"]

        events = removal_events(sql, stack)
        assert len(events) == 1
        event = events[0]
        assert event["aggregate_type"] == "card"
        assert event["aggregate_id"] == w["x"]["id"]
        ancestors = sql(
            stack.contracts_db_url,
            "SELECT ancestor_ids FROM contract WHERE id = %s",
            w["contract"],
        )[0]["ancestor_ids"]
        assert [str(i) for i in event["scope_path"]] == [str(i) for i in ancestors]
        after = event["after"]
        assert after["card_id"] == w["x"]["id"]
        assert after["contract_id"] == w["contract"]
        assert after["id"] == sub
        assert after["version_id"] == w["versions"]["3.0.0"]
        assert after["semver"] == "3.0.0"
        assert after["reason"] == REASON
        assert event["before"]["id"] == sub
        assert event["before"]["ended_at"] is None

        # The request itself writes no notice: the event is the carrier.
        assert notices_of(sql, stack, w["x"]["id"]) == []

        assert await stack.pump_notices() == 1
        notices = notices_of(sql, stack, w["x"]["id"])
        assert len(notices) == 1
        notice = notices[0]
        assert notice["event_type"] == "card.subscription.removed"
        assert str(notice["contract_id"]) == w["contract"]
        assert str(notice["version_id"]) == w["versions"]["3.0.0"]
        assert str(notice["source_event_id"]) == str(event["event_id"])
        assert notice["dedupe_key"] == f"event:{event['event_id']}"
        payload = notice["payload"]
        assert payload["reason"] == REASON
        assert payload["removed_by"] == w["editor_principal"]
        assert payload["version"] == {"id": w["versions"]["3.0.0"], "semver": "3.0.0"}
        assert payload["contract"]["id"] == w["contract"]
        deliveries = sql(
            stack.contracts_db_url,
            "SELECT channel, status FROM card_notice_delivery WHERE notice_id = %s "
            "ORDER BY channel",
            notice["id"],
        )
        assert [(d["channel"], d["status"]) for d in deliveries] == [("email", "pending")]

        # Only the removed card is told; the other card keeps its pin and gets nothing.
        assert notices_of(sql, stack, w["y"]["id"]) == []
        assert pin_row(sql, stack, w["pins"]["y230"])["ended_at"] is None

        # Redelivery of the event adds nothing.
        before = len(notices_of(sql, stack, w["x"]["id"]))
        assert await stack.replay_event(event["event_id"]) == 0
        assert await stack.pump_notices() == 0
        assert len(notices_of(sql, stack, w["x"]["id"])) == before
        assert invariant(stack.contracts_db_url) == []


async def test_removing_an_already_ended_pin_is_a_200_no_op_without_event_or_notice(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        sub = w["pins"]["x300"]
        first = await remove(stack, w["contract"], sub)
        assert first.status_code == 200, first.text
        await stack.pump_notices()
        ended = pin_row(sql, stack, sub)

        again = await remove(stack, w["contract"], sub, w["editor"], reason="A different reason")
        assert again.status_code == 200, again.text
        assert again.json()["end_reason"] == "removed_by_admin"
        assert again.json()["end_note"] == REASON
        assert again.json()["ended_by"] == first.json()["ended_by"]
        assert pin_row(sql, stack, sub) == ended
        assert len(removal_events(sql, stack)) == 1
        assert await stack.pump_notices() == 0
        assert len(notices_of(sql, stack, w["x"]["id"])) == 1


async def test_removing_a_pin_the_card_already_ended_itself_changes_nothing(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        sub = w["pins"]["x300"]
        unsubscribed = await stack.client.post(
            f"/api/v1/contracts/cards/{w['x']['id']}/subscriptions/{sub}/unsubscribe",
            json={"note": "done"},
            headers=bearer(stack.admin_token),
        )
        assert unsubscribed.status_code == 200, unsubscribed.text
        response = await remove(stack, w["contract"], sub)
        assert response.status_code == 200, response.text
        assert response.json()["end_reason"] == "unsubscribed"
        assert response.json()["end_note"] == "done"
        assert removal_events(sql, stack) == []


async def test_the_reason_is_required_stripped_and_bounded(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        sub = w["pins"]["x300"]
        url = remove_url(w["contract"], sub)
        headers = bearer(stack.admin_token)
        bad_bodies: list[dict[str, Any]] = [
            {},
            {"reason": ""},
            {"reason": "   "},
            {"reason": "x" * 501},
            {"reason": "ok\u0001bad"},
            {"reason": None},
            {"reason": 5},
            {"reason": "ok", "extra": 1},
        ]
        for body in bad_bodies:
            response = await stack.client.post(url, json=body, headers=headers)
            assert response.status_code == 422, (body, response.text)
            assert response.json()["type"] == "urn:dc:problem:validation-error"
        no_body = await stack.client.post(url, headers=headers)
        assert no_body.status_code == 422, no_body.text
        assert pin_row(sql, stack, sub)["ended_at"] is None
        assert removal_events(sql, stack) == []

        multiline = "line one\nline two"
        ok = await remove(stack, w["contract"], sub, reason=f"  {multiline}  ")
        assert ok.status_code == 200, ok.text
        assert ok.json()["end_note"] == multiline

        boundary_sub = w["pins"]["y230"]
        longest = await remove(stack, w["contract"], boundary_sub, reason="y" * 500)
        assert longest.status_code == 200, longest.text
        assert longest.json()["end_note"] == "y" * 500


async def test_a_team_reader_is_forbidden_and_consumers_get_the_unknown_contract_404(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        sub = w["pins"]["x300"]
        reader, reader_principal = stack.token("rita")
        stack.grant_sql(reader_principal, "team_reader", w["org_a"].team)
        consumer, consumer_principal = stack.token("connor")
        stack.grant_sql(consumer_principal, "consumer", w["org_a"].team)
        manager, manager_principal = stack.token("pm")
        stack.grant_sql(manager_principal, "policy_manager", w["org_a"].org)
        outsider, outsider_principal = stack.token("org-b-user")
        stack.grant_sql(outsider_principal, "team_editor", w["org_b"].team)

        denied = await remove(stack, w["contract"], sub, reader)
        assert denied.status_code == 403, denied.text
        assert denied.json()["type"] == "urn:dc:problem:forbidden"
        for token in (consumer, manager, outsider):
            real = await remove(stack, w["contract"], sub, token)
            missing = await remove(stack, UNKNOWN, sub, token)
            assert real.status_code == 404, real.text
            assert stable(real) == stable(missing)
        anonymous = await stack.client.post(remove_url(w["contract"], sub), json={"reason": "x"})
        assert anonymous.status_code == 401
        assert pin_row(sql, stack, sub)["ended_at"] is None
        assert removal_events(sql, stack) == []


async def test_a_pin_of_another_contract_or_an_unknown_pin_is_the_unknown_404(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        foreign = w["pins"]["x_other"]
        for sub in (foreign, UNKNOWN):
            response = await remove(stack, w["contract"], sub, w["editor"])
            assert response.status_code == 404, response.text
            assert response.json()["type"] == "urn:dc:problem:not-found"
        unknown_contract = await remove(stack, UNKNOWN, w["pins"]["x300"], w["editor"])
        assert unknown_contract.status_code == 404
        assert pin_row(sql, stack, foreign)["ended_at"] is None
        assert removal_events(sql, stack) == []


async def test_an_archived_contract_folder_refuses_the_removal_with_409(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        sub = w["pins"]["x300"]
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{w['org_a'].team}/archive",
            headers=bearer(stack.admin_token),
        )
        assert archived.status_code in (200, 204), archived.text
        response = await remove(stack, w["contract"], sub)
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "urn:dc:problem:folder-archived"
        assert pin_row(sql, stack, sub)["ended_at"] is None
        assert removal_events(sql, stack) == []


async def test_the_cards_history_shows_the_removal_and_the_owner_view_drops_the_pin(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        sub = w["pins"]["x300"]
        assert (await remove(stack, w["contract"], sub, w["editor"])).status_code == 200

        card = await stack.client.get(
            f"/api/v1/contracts/cards/{w['x']['id']}", headers=bearer(stack.admin_token)
        )
        assert card.status_code == 200, card.text
        ended = [s for s in card.json()["subscriptions"] if s["id"] == sub][0]
        assert ended["end_reason"] == "removed_by_admin"
        assert ended["end_note"] == REASON
        assert ended["ended_by"] == w["editor_principal"]
        assert ended["ended_at"]

        owner = await stack.client.get(
            f"/api/v1/contracts/contracts/{w['contract']}/subscribers",
            headers=bearer(stack.admin_token),
        )
        assert owner.status_code == 200, owner.text
        semvers = [v["semver"] for v in owner.json()["versions"]]
        assert semvers == ["2.3.0"]
