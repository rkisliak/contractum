"""Pinned-version subscriptions (SUB-03, SUB-04): a card records exactly which versions it uses.

A card manager or member pins a published version with the acting principal's own read access
(D-86); the card adds no authority (D-88). History is never deleted or rewritten: the database
refuses it (D-84). Every scenario ends with the pin invariants returning no rows.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

import httpx
import psycopg
import pytest

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"
UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]


def card_grants_access() -> object:
    """The D-83 seam constant, or None while the module does not exist yet."""
    try:
        return getattr(import_module("dc_contracts.subscriptions"), "CARD_GRANTS_ACCESS", None)
    except ModuleNotFoundError:
        return None


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
    body = response.json()
    return {k: body.get(k) for k in ("type", "title", "status", "detail")}


def events(sql: Sql, stack: Any, like: str = "card.%") -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_type, aggregate_id, before, after FROM outbox "
        "WHERE action LIKE %s ORDER BY id",
        like,
    )


def pin_rows(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM card_subscription WHERE card_id = %s ORDER BY id",
        card_id,
    )


async def _world(stack: Any) -> dict[str, Any]:
    """`orders` 2.3.0 (Latest) and 3.0.0 (Preview) in Org A; a card in Org B; dana is a member.

    dana is in a linked local group and holds the consumer role at Org A; fay is in the same
    group and holds nothing at Org A. The card is managed by its creator, ``mgr``.
    """
    org_a = await stack.chain()
    org_b = await stack.chain()
    contract, versions = await stack.publish_versions(org_a.team, ["2.3.0", "3.0.0"])
    mgr, mgr_principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(mgr_principal, "consumer", org_b.team)
    card = await stack.create_card(org_b.team, token=mgr)
    group = await stack.local_group(stack.root_id, f"g-{uuid.uuid4().hex[:8]}")
    linked = await stack.link_member(card["id"], "local_group", group, token=mgr)
    assert linked.status_code == 201, linked.text
    dana, dana_principal = stack.token("dana")
    stack.grant_sql(dana_principal, "consumer", org_a.org)
    stack.group_member_sql(group, dana_principal)
    fay, fay_principal = stack.token("fay")
    stack.group_member_sql(group, fay_principal)
    return {
        "org_a": org_a,
        "org_b": org_b,
        "contract": contract,
        "versions": versions,
        "mgr": mgr,
        "card": card,
        "dana": dana,
        "dana_principal": dana_principal,
        "fay": fay,
    }


# --- tracer: a card member pins published versions and the card shows them ---------------------


async def test_a_member_pins_a_published_version_and_the_card_shows_the_pin(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id, contract = w["card"]["id"], w["contract"]

        first = await stack.subscribe(card_id, contract, "2.3.0", token=w["dana"])
        assert first.status_code == 201, first.text
        pin = first.json()
        assert pin["semver"] == "2.3.0"
        assert pin["mode"] == "pinned"
        assert pin["pinned_by"] == "fake:dana"
        assert pin["pinned_at"]
        assert pin["contract_id"] == contract
        assert pin["version_id"] == w["versions"]["2.3.0"]
        assert pin["ended_at"] is None
        assert first.headers["location"] == f"{CARDS}/{card_id}/subscriptions/{pin['id']}"
        added = events(sql, stack, "card.subscribed")
        assert len(added) == 1
        assert added[0]["aggregate_id"] == card_id
        assert added[0]["before"] is None
        after = added[0]["after"]
        assert after["contract_id"] == contract
        assert after["version_id"] == w["versions"]["2.3.0"]
        assert after["semver"] == "2.3.0"

        again = await stack.subscribe(card_id, contract, "2.3.0", token=w["dana"])
        assert again.status_code == 200, again.text
        assert again.json()["id"] == pin["id"]
        assert len(events(sql, stack, "card.subscribed")) == 1
        assert len(pin_rows(sql, stack, card_id)) == 1

        preview = await stack.subscribe(card_id, contract, "3.0.0", token=w["dana"])
        assert preview.status_code == 201, preview.text
        assert preview.json()["semver"] == "3.0.0"
        assert len(events(sql, stack, "card.subscribed")) == 2

        shown = await stack.client.get(f"{CARDS}/{card_id}", headers=bearer(w["mgr"]))
        assert shown.status_code == 200, shown.text
        body = shown.json()
        assert [s["semver"] for s in body["subscriptions"]] == ["3.0.0", "2.3.0"]
        assert body["subscriptions_truncated"] is False
        assert [s["id"] for s in body["subscriptions"]] == [preview.json()["id"], pin["id"]]
        assert invariant(stack.contracts_db_url) == []


# --- the acting principal's own access decides, never the card (D-86) ---------------------------


async def test_a_member_without_read_access_to_the_contract_gets_the_unknown_contract_404(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id = w["card"]["id"]

        denied = await stack.subscribe(card_id, w["contract"], "2.3.0", token=w["fay"])
        unknown = await stack.subscribe(card_id, UNKNOWN, "2.3.0", token=w["fay"])
        assert_problem(denied, 404, "not-found")
        assert stable(denied) == stable(unknown)
        assert pin_rows(sql, stack, card_id) == []
        assert events(sql, stack, "card.subscribed") == []

        # The manager is a Consumer in Org B only: the card does not widen that either.
        mgr = await stack.subscribe(card_id, w["contract"], "2.3.0", token=w["mgr"])
        assert_problem(mgr, 404, "not-found")
        assert invariant(stack.contracts_db_url) == []


async def test_a_pin_never_grants_read_access_to_the_pinned_version(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        pinned = await stack.subscribe(w["card"]["id"], w["contract"], "2.3.0", token=w["dana"])
        assert pinned.status_code == 201, pinned.text
        assert card_grants_access() is False

        fetch = await stack.client.get(
            f"/api/v1/contracts/contracts/{w['contract']}/versions/2.3.0", headers=bearer(w["fay"])
        )
        assert fetch.status_code == 404, fetch.text
        for route in ("versions", "versions/2.3.0", "versions/2.3.0/document", "document"):
            via_card = await stack.client.get(
                f"{CARDS}/{w['card']['id']}/{route}", headers=bearer(w["fay"])
            )
            assert via_card.status_code in (404, 405), (route, via_card.text)
        assert invariant(stack.contracts_db_url) == []


# --- what may be pinned (D-90) --------------------------------------------------------------------


async def test_deprecated_unpublished_and_non_concrete_versions_are_refused(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id, contract = w["card"]["id"], w["contract"]
        base = f"/api/v1/contracts/contracts/{contract}/versions"
        sunset = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
        deprecated = await stack.client.post(
            f"{base}/2.3.0/deprecate",
            json={"sunset_date": sunset},
            headers=bearer(stack.admin_token),
        )
        assert deprecated.status_code == 200, deprecated.text
        refused = await stack.subscribe(card_id, contract, "2.3.0", token=w["dana"])
        assert_problem(refused, 409, "version-deprecated")

        for ref in ("latest", "2.3", "v2.3.0", "", "9.9.9"):
            response = await stack.subscribe(card_id, contract, ref, token=w["dana"])
            assert_problem(response, 404, "not-found")

        unpublished = await stack.client.post(
            f"{base}/3.0.0/unpublish", headers=bearer(stack.admin_token)
        )
        assert unpublished.status_code == 200, unpublished.text
        hidden = await stack.subscribe(card_id, contract, "3.0.0", token=w["dana"])
        assert_problem(hidden, 404, "not-found")
        assert stable(hidden) == stable(
            await stack.subscribe(card_id, contract, "9.9.9", token=w["dana"])
        )

        # The Global admin creates a card and holds contract:publish, so may be told the reason.
        admin_card = await stack.create_card(w["org_b"].team)
        owner = await stack.subscribe(admin_card["id"], contract, "3.0.0")
        assert_problem(owner, 409, "version-not-published")
        assert events(sql, stack, "card.subscribed") == []
        assert invariant(stack.contracts_db_url) == []


async def test_an_archived_card_refuses_new_pins(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id = w["card"]["id"]
        archived = await stack.client.post(f"{CARDS}/{card_id}/archive", headers=bearer(w["mgr"]))
        assert archived.status_code == 200, archived.text

        refused = await stack.subscribe(card_id, w["contract"], "2.3.0", token=w["dana"])
        assert_problem(refused, 409, "card-archived")
        assert pin_rows(sql, stack, card_id) == []
        assert invariant(stack.contracts_db_url) == []


async def test_outsiders_and_readers_follow_the_card_visibility_rule(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id = w["card"]["id"]
        erin, _ = stack.token("erin")
        rita, rita_principal = stack.token("rita")
        stack.grant_sql(rita_principal, "team_reader", w["org_b"].team)

        outsider = await stack.subscribe(card_id, w["contract"], "2.3.0", token=erin)
        missing = await stack.subscribe(UNKNOWN, w["contract"], "2.3.0", token=erin)
        assert_problem(outsider, 404, "not-found")
        assert stable(outsider) == stable(missing)

        # A folder reader sees the card but is neither its manager nor a member.
        reader = await stack.subscribe(card_id, w["contract"], "2.3.0", token=rita)
        assert_problem(reader, 403, "forbidden")
        assert invariant(stack.contracts_db_url) == []


async def test_an_identity_failure_while_resolving_the_card_follows_the_outage_rule(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id = w["card"]["id"]
        erin, _ = stack.token("erin")
        rita, rita_principal = stack.token("rita")
        stack.grant_sql(rita_principal, "team_reader", w["org_b"].team)

        with stack.flaky_authz(fail_grantees=True):
            outsider = await stack.subscribe(card_id, w["contract"], "2.3.0", token=erin)
            unknown = await stack.subscribe(UNKNOWN, w["contract"], "2.3.0", token=erin)
            reader = await stack.subscribe(card_id, w["contract"], "2.3.0", token=rita)
        assert_problem(outsider, 404, "not-found")
        assert_problem(unknown, 404, "not-found")
        assert stable(outsider) == stable(unknown)
        assert_problem(reader, 503, "service-unavailable")
        assert pin_rows(sql, stack, card_id) == []
        assert events(sql, stack, "card.subscribed") == []
        assert invariant(stack.contracts_db_url) == []


async def test_two_concurrent_identical_subscribes_yield_one_row(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id = w["card"]["id"]
        first, second = await asyncio.gather(
            stack.subscribe(card_id, w["contract"], "2.3.0", token=w["dana"]),
            stack.subscribe(card_id, w["contract"], "2.3.0", token=w["dana"]),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 201], (
            first.text,
            second.text,
        )
        assert first.json()["id"] == second.json()["id"]
        assert len(pin_rows(sql, stack, card_id)) == 1
        assert len(events(sql, stack, "card.subscribed")) == 1
        assert invariant(stack.contracts_db_url) == []


# --- the database protects history (D-84) ---------------------------------------------------------


async def test_the_database_refuses_to_delete_or_rewrite_subscription_history(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id, contract = w["card"]["id"], w["contract"]
        first = await stack.subscribe(card_id, contract, "2.3.0", token=w["dana"])
        assert first.status_code == 201, first.text
        pin = first.json()["id"]
        other = w["versions"]["3.0.0"]
        db = stack.contracts_db_url

        with pytest.raises(psycopg.errors.RaiseException):
            sql(db, "DELETE FROM card_subscription WHERE id = %s", pin)
        with pytest.raises(psycopg.errors.RaiseException):
            sql(db, "UPDATE card_subscription SET version_id = %s WHERE id = %s", other, pin)
        with pytest.raises(psycopg.errors.RaiseException):
            sql(
                db,
                "UPDATE card_subscription SET created_by = 'someone-else' WHERE id = %s",
                pin,
            )
        with pytest.raises(psycopg.errors.RaiseException):
            sql(db, "UPDATE card_subscription SET mode = 'follow-latest' WHERE id = %s", pin)
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(
                db,
                "INSERT INTO card_subscription (card_id, contract_id, version_id, mode, "
                "created_by, updated_by) VALUES (%s, %s, %s, 'follow-latest', 'system:test', "
                "'system:test')",
                card_id,
                contract,
                other,
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(db, "UPDATE contract SET subscription_policy = 'approval' WHERE id = %s", contract)
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(
                db,
                "UPDATE card_subscription SET ended_at = now() WHERE id = %s",
                pin,
            )

        ended = sql(
            db,
            "UPDATE card_subscription SET ended_at = now(), ended_by = 'system:test', "
            "end_reason = 'unsubscribed', updated_by = 'system:test', updated_at = now() "
            "WHERE id = %s RETURNING id",
            pin,
        )
        assert len(ended) == 1
        with pytest.raises(psycopg.errors.RaiseException):
            sql(db, "UPDATE card_subscription SET end_note = 'late' WHERE id = %s", pin)
        with pytest.raises(psycopg.errors.RaiseException):
            sql(db, "DELETE FROM card_subscription WHERE id = %s", pin)
        assert len(pin_rows(sql, stack, card_id)) == 1
        assert invariant(stack.contracts_db_url) == []


def test_the_card_grants_access_seam_is_a_switched_off_constant() -> None:
    assert card_grants_access() is False


# --- SC2: move, unsubscribe, archive and the full record (SUB-03, SUB-04) ---------


async def _move(
    stack: Any, card_id: str, sub_id: str, body: dict[str, Any], token: str
) -> httpx.Response:
    return await stack.client.post(
        f"{CARDS}/{card_id}/subscriptions/{sub_id}/move", json=body, headers=bearer(token)
    )


async def _unsubscribe(
    stack: Any, card_id: str, sub_id: str, token: str, body: dict[str, Any] | None = None
) -> httpx.Response:
    return await stack.client.post(
        f"{CARDS}/{card_id}/subscriptions/{sub_id}/unsubscribe",
        json=body if body is not None else {},
        headers=bearer(token),
    )


async def _listing(stack: Any, card_id: str, token: str, query: str = "") -> httpx.Response:
    return await stack.client.get(f"{CARDS}/{card_id}/subscriptions{query}", headers=bearer(token))


async def test_a_card_moves_a_pin_unsubscribes_and_shows_its_full_record(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id, contract = w["card"]["id"], w["contract"]
        customers, customer_versions = await stack.publish_versions(w["org_a"].team, ["1.0.0"])

        orders = await stack.subscribe(card_id, contract, "2.3.0", token=w["dana"])
        assert orders.status_code == 201, orders.text
        old = orders.json()
        moved = await _move(stack, card_id, old["id"], {"version": "3.0.0"}, w["dana"])
        assert moved.status_code == 200, moved.text
        new = moved.json()
        assert new["id"] != old["id"]
        assert new["semver"] == "3.0.0"
        assert new["ended_at"] is None
        assert new["pinned_by"] == "fake:dana"

        rows = {r["id"]: r for r in pin_rows(sql, stack, card_id)}
        old_row = rows[uuid.UUID(old["id"])]
        assert old_row["ended_at"] is not None
        assert old_row["ended_by"] == "fake:dana"
        assert old_row["end_reason"] == "moved"
        assert str(old_row["replaced_by_id"]) == new["id"]
        assert rows[uuid.UUID(new["id"])]["ended_at"] is None
        moves = events(sql, stack, "card.subscription_moved")
        assert len(moves) == 1
        assert moves[0]["aggregate_id"] == card_id
        assert moves[0]["before"]["id"] == old["id"]
        assert moves[0]["before"]["semver"] == "2.3.0"
        assert moves[0]["after"]["id"] == new["id"]
        assert moves[0]["after"]["semver"] == "3.0.0"

        other = await stack.subscribe(card_id, customers, "1.0.0", token=w["dana"])
        assert other.status_code == 201, other.text
        assert other.json()["version_id"] == customer_versions["1.0.0"]
        gone = await _unsubscribe(
            stack, card_id, other.json()["id"], w["dana"], {"note": "migrated off"}
        )
        assert gone.status_code == 200, gone.text
        assert gone.json()["end_reason"] == "unsubscribed"
        assert gone.json()["end_note"] == "migrated off"
        assert gone.json()["ended_by"] == "fake:dana"
        assert gone.json()["ended_at"] is not None
        unsubscribed = events(sql, stack, "card.unsubscribed")
        assert len(unsubscribed) == 1
        assert unsubscribed[0]["before"]["id"] == other.json()["id"]
        assert unsubscribed[0]["after"]["end_reason"] == "unsubscribed"

        shown = await stack.client.get(f"{CARDS}/{card_id}", headers=bearer(w["mgr"]))
        record = shown.json()["subscriptions"]
        assert [s["id"] for s in record] == [other.json()["id"], new["id"], old["id"]][
            : len(record)
        ]
        assert [(s["semver"], s["end_reason"]) for s in record] == [
            ("1.0.0", "unsubscribed"),
            ("3.0.0", None),
            ("2.3.0", "moved"),
        ]
        assert all(s["pinned_by"] == "fake:dana" and s["pinned_at"] for s in record)
        assert record[0]["ended_by"] == "fake:dana"
        assert record[0]["ended_at"]

        active = await _listing(stack, card_id, w["mgr"], "?state=active")
        assert [i["semver"] for i in active.json()["items"]] == ["3.0.0"]
        ended = await _listing(stack, card_id, w["mgr"], "?state=ended")
        assert [i["semver"] for i in ended.json()["items"]] == ["1.0.0", "2.3.0"]
        first = await _listing(stack, card_id, w["mgr"], "?limit=1")
        assert [i["semver"] for i in first.json()["items"]] == ["1.0.0"]
        assert first.json()["next_cursor"]
        second = await _listing(
            stack, card_id, w["mgr"], f"?limit=1&cursor={first.json()['next_cursor']}"
        )
        assert [i["semver"] for i in second.json()["items"]] == ["3.0.0"]
        third = await _listing(
            stack, card_id, w["mgr"], f"?limit=2&cursor={second.json()['next_cursor']}"
        )
        assert [i["semver"] for i in third.json()["items"]] == ["2.3.0"]
        assert third.json()["next_cursor"] is None
        assert invariant(stack.contracts_db_url) == []


async def test_a_card_without_pins_lists_empty_and_never_null(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id = w["card"]["id"]
        listed = await _listing(stack, card_id, w["mgr"])
        assert listed.status_code == 200, listed.text
        assert listed.json() == {"items": [], "next_cursor": None}
        shown = (await stack.client.get(f"{CARDS}/{card_id}", headers=bearer(w["mgr"]))).json()
        assert shown["subscriptions"] == []
        assert shown["subscriptions_truncated"] is False
        assert shown["contacts"] == []
        assert len(shown["members"]) == 1
        outsider, _ = stack.token("erin")
        hidden = await _listing(stack, card_id, outsider)
        assert_problem(hidden, 404, "not-found")
        assert invariant(stack.contracts_db_url) == []


async def test_move_edge_cases(make_stack: MakeStack, sql: Sql, invariant: Invariant) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id, contract = w["card"]["id"], w["contract"]
        pinned = (await stack.subscribe(card_id, contract, "2.3.0", token=w["dana"])).json()

        same = await _move(stack, card_id, pinned["id"], {"version": "2.3.0"}, w["dana"])
        assert same.status_code == 200, same.text
        assert same.json()["id"] == pinned["id"]
        assert events(sql, stack, "card.subscription_moved") == []

        extra = await _move(
            stack, card_id, pinned["id"], {"version": "3.0.0", "contract_id": contract}, w["dana"]
        )
        assert_problem(extra, 422, "validation-error")
        for ref in ("latest", "2.3", "9.9.9"):
            bad = await _move(stack, card_id, pinned["id"], {"version": ref}, w["dana"])
            assert_problem(bad, 404, "not-found")

        # Moving to a version the card already pins keeps the existing pin and creates no row.
        preview = (await stack.subscribe(card_id, contract, "3.0.0", token=w["dana"])).json()
        merged = await _move(stack, card_id, pinned["id"], {"version": "3.0.0"}, w["dana"])
        assert merged.status_code == 200, merged.text
        assert merged.json()["id"] == preview["id"]
        rows = pin_rows(sql, stack, card_id)
        assert len(rows) == 2
        old = next(r for r in rows if str(r["id"]) == pinned["id"])
        assert old["end_reason"] == "moved"
        assert str(old["replaced_by_id"]) == preview["id"]
        assert len(events(sql, stack, "card.subscription_moved")) == 1

        # An ended pin cannot be moved.
        ended = await _move(stack, card_id, pinned["id"], {"version": "3.0.0"}, w["dana"])
        assert_problem(ended, 409, "subscription-ended")

        # A deprecated target is refused.
        sunset = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
        deprecated = await stack.client.post(
            f"/api/v1/contracts/contracts/{contract}/versions/2.3.0/deprecate",
            json={"sunset_date": sunset},
            headers=bearer(stack.admin_token),
        )
        assert deprecated.status_code == 200, deprecated.text
        refused = await _move(stack, card_id, preview["id"], {"version": "2.3.0"}, w["dana"])
        assert_problem(refused, 409, "version-deprecated")

        # A pin of another card is the unknown-subscription 404.
        second = await stack.create_card(w["org_b"].team, token=w["mgr"])
        foreign = await _move(stack, second["id"], preview["id"], {"version": "3.0.0"}, w["mgr"])
        assert_problem(foreign, 404, "not-found")
        foreign_unsub = await _unsubscribe(stack, second["id"], preview["id"], w["mgr"])
        assert_problem(foreign_unsub, 404, "not-found")
        assert invariant(stack.contracts_db_url) == []


async def test_unsubscribe_edge_cases(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id, contract = w["card"]["id"], w["contract"]
        pinned = (await stack.subscribe(card_id, contract, "2.3.0", token=w["dana"])).json()
        erin, _ = stack.token("erin")

        hidden = await _unsubscribe(stack, card_id, pinned["id"], erin)
        assert_problem(hidden, 404, "not-found")
        assert pin_rows(sql, stack, card_id)[0]["ended_at"] is None

        first = await _unsubscribe(stack, card_id, pinned["id"], w["fay"])
        assert first.status_code == 200, first.text
        assert first.json()["end_note"] is None
        assert first.json()["ended_by"] == "fake:fay"

        again = await _unsubscribe(stack, card_id, pinned["id"], w["dana"], {"note": "again"})
        assert again.status_code == 200, again.text
        assert again.json()["ended_by"] == "fake:fay"
        assert again.json()["end_note"] is None
        assert len(events(sql, stack, "card.unsubscribed")) == 1

        long = await _unsubscribe(stack, card_id, pinned["id"], w["dana"], {"note": "x" * 501})
        assert_problem(long, 422, "validation-error")
        assert invariant(stack.contracts_db_url) == []


async def test_archiving_a_card_ends_every_active_pin_in_the_same_unit(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id, contract = w["card"]["id"], w["contract"]
        first = (await stack.subscribe(card_id, contract, "2.3.0", token=w["dana"])).json()
        second = (await stack.subscribe(card_id, contract, "3.0.0", token=w["dana"])).json()
        before = {str(r["id"]): r for r in pin_rows(sql, stack, card_id)}

        archived = await stack.client.post(f"{CARDS}/{card_id}/archive", headers=bearer(w["mgr"]))
        assert archived.status_code == 200, archived.text
        creator = w["card"]["created_by"]
        rows = pin_rows(sql, stack, card_id)
        assert len(rows) == 2
        for row in rows:
            assert row["end_reason"] == "card_archived"
            assert row["ended_by"] == creator
            assert row["updated_by"] == creator
            assert row["ended_at"] is not None
            assert row["updated_at"] > before[str(row["id"])]["updated_at"]
        events_ = events(sql, stack, "card.archived")
        assert len(events_) == 1
        assert events_[0]["after"]["ended_subscription_ids"] == sorted([first["id"], second["id"]])
        shown = (await stack.client.get(f"{CARDS}/{card_id}", headers=bearer(w["mgr"]))).json()
        assert [s["end_reason"] for s in shown["subscriptions"]] == ["card_archived"] * 2
        assert archived.json()["subscriptions_truncated"] is False
        assert invariant(stack.contracts_db_url) == []


async def test_archiving_a_card_without_pins_lists_no_ended_ids(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        archived = await stack.client.post(
            f"{CARDS}/{w['card']['id']}/archive", headers=bearer(w["mgr"])
        )
        assert archived.status_code == 200, archived.text
        events_ = events(sql, stack, "card.archived")
        assert events_[0]["after"]["ended_subscription_ids"] == []


async def test_a_pin_survives_the_loss_of_the_pinners_access_but_new_pins_are_refused(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id, contract = w["card"]["id"], w["contract"]
        pinned = await stack.subscribe(card_id, contract, "2.3.0", token=w["dana"])
        assert pinned.status_code == 201, pinned.text

        sql(
            stack.identity_db_url,
            "DELETE FROM role_assignments WHERE principal_id = %s",
            w["dana_principal"],
        )
        shown = await stack.client.get(f"{CARDS}/{card_id}", headers=bearer(w["mgr"]))
        assert [(s["semver"], s["ended_at"]) for s in shown.json()["subscriptions"]] == [
            ("2.3.0", None)
        ]
        refused = await stack.subscribe(card_id, contract, "3.0.0", token=w["dana"])
        assert_problem(refused, 404, "not-found")
        assert invariant(stack.contracts_db_url) == []


async def test_unsubscribe_accepts_a_request_without_a_body(
    make_stack: MakeStack, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        pinned = await stack.subscribe(w["card"]["id"], w["contract"], "2.3.0", token=w["dana"])
        response = await stack.client.post(
            f"{CARDS}/{w['card']['id']}/subscriptions/{pinned.json()['id']}/unsubscribe",
            headers=bearer(w["dana"]),
        )
        assert response.status_code == 200, response.text
        assert response.json()["end_reason"] == "unsubscribed"
        assert response.json()["end_note"] is None
        assert invariant(stack.contracts_db_url) == []
