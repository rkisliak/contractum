"""Owner view of a contract's consumers (SUB-05, D-102, D-103).

Readers of a contract page through the cards that actively pin its versions, with each card's name,
description and contacts, and nothing else about the card: never its members, channels, webhook URL
or its pins on other contracts, whatever organization the card lives in (SC3, T-04.1-16).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

UNKNOWN = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
WEBHOOK = "https://hooks.example.test/secret-token-path"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]

CARD_KEYS = {
    "subscription_id",
    "card_id",
    "name",
    "description",
    "contacts",
    "pinned_at",
    "pinned_by",
}
VERSION_KEYS = {"version_id", "semver", "deprecated", "sunset_date", "cards"}
PAGE_KEYS = {"contract_id", "versions", "next_cursor"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def subscribers_url(contract: str) -> str:
    return f"/api/v1/contracts/contracts/{contract}/subscribers"


async def get_page(
    stack: Any, contract: str, token: str | None = None, **params: Any
) -> httpx.Response:
    return await stack.client.get(
        subscribers_url(contract),
        params=params,
        headers=bearer(token or stack.admin_token),
    )


def stable(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    return {k: body.get(k) for k in ("type", "title", "status", "detail")}


def flat(page: dict[str, Any]) -> list[tuple[str, str]]:
    """(semver, card id) for every card entry of one page, in page order."""
    return [(v["semver"], c["card_id"]) for v in page["versions"] for c in v["cards"]]


async def _world(stack: Any, sql: Sql) -> dict[str, Any]:
    """orders 2.3.0 and 3.0.0 in Org A; card X (Org B) pins both, card Y (Org A) pins 2.3.0.

    X has a linked local group, a webhook URL, two contacts and a pin on another contract. Pins
    are made in the order X@2.3.0, Y@2.3.0, X@3.0.0.
    """
    org_a = await stack.chain()
    org_b = await stack.chain()
    contract, versions = await stack.publish_versions(org_a.team, ["2.3.0", "3.0.0"])
    other, _ = await stack.publish_versions(org_a.team, ["1.0.0"])
    mgr, mgr_principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(mgr_principal, "consumer", org_b.team)
    card_x = await stack.create_card(
        org_b.team,
        token=mgr,
        name="Billing consumers",
        description="Nightly billing job",
        contacts=[
            {"name": "Ada", "kind": "email", "value": "ada@example.test"},
            {"kind": "url", "value": "https://wiki.example.test/billing"},
        ],
    )
    group = await stack.local_group(stack.root_id, f"g-{uuid.uuid4().hex[:8]}")
    linked = await stack.link_member(card_x["id"], "local_group", group, token=mgr)
    assert linked.status_code == 201, linked.text
    sql(
        stack.contracts_db_url,
        "UPDATE subscriber_card SET email_enabled = true, webhook_enabled = true, "
        "webhook_url = %s WHERE id = %s",
        WEBHOOK,
        card_x["id"],
    )
    card_y = await stack.create_card(org_a.team, name="Orders reporting", description="Reports")
    pins: dict[str, str] = {}
    for key, card, target, semver in (
        ("x230", card_x, contract, "2.3.0"),
        ("y230", card_y, contract, "2.3.0"),
        ("x300", card_x, contract, "3.0.0"),
        ("x_other", card_x, other, "1.0.0"),
    ):
        pinned = await stack.subscribe(card["id"], target, semver)
        assert pinned.status_code == 201, pinned.text
        pins[key] = pinned.json()["id"]
    return {
        "org_a": org_a,
        "org_b": org_b,
        "contract": contract,
        "other": other,
        "versions": versions,
        "x": card_x,
        "y": card_y,
        "group": group,
        "pins": pins,
    }


async def _reader(stack: Any, team: str) -> str:
    token, principal = stack.token(f"reader-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "team_reader", team)
    return token


# --- tracer: the owner pages through who consumes each version, members never shown ------------


async def test_the_owner_lists_consumers_per_version_with_contacts_and_nothing_else(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        reader = await _reader(stack, w["org_a"].team)

        response = await get_page(stack, w["contract"], reader)
        assert response.status_code == 200, response.text
        page = response.json()
        assert set(page) == PAGE_KEYS
        assert page["contract_id"] == w["contract"]
        assert page["next_cursor"] is None
        assert [v["semver"] for v in page["versions"]] == ["3.0.0", "2.3.0"]
        for entry in page["versions"]:
            assert set(entry) == VERSION_KEYS
            assert entry["deprecated"] is False
            assert entry["sunset_date"] is None
            for card in entry["cards"]:
                assert set(card) == CARD_KEYS
        newest, oldest = page["versions"]
        assert newest["version_id"] == w["versions"]["3.0.0"]
        assert oldest["version_id"] == w["versions"]["2.3.0"]
        assert [c["card_id"] for c in newest["cards"]] == [w["x"]["id"]]
        assert [c["card_id"] for c in oldest["cards"]] == [w["x"]["id"], w["y"]["id"]]

        x_entry = oldest["cards"][0]
        assert x_entry["name"] == "Billing consumers"
        assert x_entry["description"] == "Nightly billing job"
        assert x_entry["contacts"] == [
            {"name": "Ada", "kind": "email", "value": "ada@example.test"},
            {"name": None, "kind": "url", "value": "https://wiki.example.test/billing"},
        ]
        assert x_entry["pinned_by"]
        assert x_entry["pinned_at"]
        # The pin's own id, so an owner can remove it without reading the card.
        assert x_entry["subscription_id"] == w["pins"]["x230"]
        assert newest["cards"][0]["subscription_id"] == w["pins"]["x300"]
        assert oldest["cards"][1]["subscription_id"] == w["pins"]["y230"]

        # SC3: a card of another organization shows only its pins on this contract.
        body = json.dumps(page)
        assert w["group"] not in body
        assert WEBHOOK not in body
        assert "hooks.example.test" not in body
        assert w["other"] not in body
        assert w["pins"]["x_other"] not in body
        for forbidden in ("members", "webhook", "channels", "email_enabled", "subscriptions"):
            assert forbidden not in body
        assert invariant(stack.contracts_db_url) == []


async def test_a_card_pinned_to_two_versions_appears_once_under_each_never_merged(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        page = (await get_page(stack, w["contract"])).json()
        occurrences = [(s, c) for s, c in flat(page) if c == w["x"]["id"]]
        assert occurrences == [("3.0.0", w["x"]["id"]), ("2.3.0", w["x"]["id"])]


async def test_paging_with_limit_one_visits_every_active_pin_exactly_once(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        seen: list[tuple[str, str]] = []
        cursors: list[str | None] = []
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            response = await get_page(stack, w["contract"], **params)
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(flat(page)) == 1
            assert len(page["versions"]) == 1
            seen.extend(flat(page))
            cursors.append(page["next_cursor"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert cursors[:-1] and all(c for c in cursors[:-1])
        assert cursors[-1] is None
        assert len(cursors) == 3
        assert seen == [
            ("2.3.0", w["x"]["id"]),
            ("2.3.0", w["y"]["id"]),
            ("3.0.0", w["x"]["id"]),
        ]
        assert len(set(seen)) == len(seen)


async def test_a_page_larger_than_the_data_has_no_cursor_and_the_limit_is_bounded(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        exact = await get_page(stack, w["contract"], limit=3)
        assert exact.status_code == 200
        assert exact.json()["next_cursor"] is None
        assert len(flat(exact.json())) == 3
        for bad in (0, 101, -1):
            response = await get_page(stack, w["contract"], limit=bad)
            assert response.status_code == 422, response.text
            assert response.json()["type"] == "urn:dc:problem:validation-error"
        too_long = await get_page(stack, w["contract"], limit="x")
        assert too_long.status_code == 422


async def test_a_malformed_cursor_is_a_422(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        for bad in ("not-a-cursor", "!!!", "AAAA"):
            response = await get_page(stack, w["contract"], cursor=bad)
            assert response.status_code == 422, response.text
            assert response.json()["type"] == "urn:dc:problem:validation-error"


async def test_consumers_policy_managers_and_outsiders_get_the_unknown_contract_404(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        consumer_team, consumer_team_p = stack.token("consumer-team")
        stack.grant_sql(consumer_team_p, "consumer", w["org_a"].team)
        consumer_org, consumer_org_p = stack.token("consumer-org")
        stack.grant_sql(consumer_org_p, "consumer", w["org_a"].org)
        manager, manager_p = stack.token("pm")
        stack.grant_sql(manager_p, "policy_manager", w["org_a"].org)
        outsider, outsider_p = stack.token("org-b-user")
        stack.grant_sql(outsider_p, "team_reader", w["org_b"].team)
        for token in (consumer_team, consumer_org, manager, outsider):
            real = await get_page(stack, w["contract"], token)
            missing = await get_page(stack, UNKNOWN, token)
            assert real.status_code == 404, real.text
            assert real.json()["type"] == "urn:dc:problem:not-found"
            assert stable(real) == stable(missing)
        anonymous = await stack.client.get(subscribers_url(w["contract"]))
        assert anonymous.status_code == 401


async def test_every_contract_reader_role_opens_the_owner_view(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        holders = {
            "team_reader": w["org_a"].team,
            "team_editor": w["org_a"].team,
            "org_reader": w["org_a"].org,
            "project_editor": w["org_a"].project,
        }
        for role, folder in holders.items():
            token, principal = stack.token(f"{role}-user")
            stack.grant_sql(principal, role, folder)
            response = await get_page(stack, w["contract"], token)
            assert response.status_code == 200, (role, response.text)
            assert len(flat(response.json())) == 3
        missing = await get_page(stack, UNKNOWN)
        assert missing.status_code == 404


async def test_a_contract_with_no_pins_is_an_empty_page_and_ended_pins_never_appear(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        org = await stack.chain()
        contract, _ = await stack.publish_versions(org.team, ["1.0.0"])
        empty = await get_page(stack, contract)
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"contract_id": contract, "versions": [], "next_cursor": None}

        w = await _world(stack, sql)
        unsubscribed = await stack.client.post(
            f"/api/v1/contracts/cards/{w['x']['id']}/subscriptions/{w['pins']['x300']}/unsubscribe",
            headers=bearer(stack.admin_token),
        )
        assert unsubscribed.status_code == 200, unsubscribed.text
        page = (await get_page(stack, w["contract"])).json()
        assert [v["semver"] for v in page["versions"]] == ["2.3.0"]
        assert flat(page) == [("2.3.0", w["x"]["id"]), ("2.3.0", w["y"]["id"])]


async def test_a_deprecated_version_shows_its_flag_and_sunset_date(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack, sql)
        deprecated = await stack.client.post(
            f"/api/v1/contracts/contracts/{w['contract']}/versions/2.3.0/deprecate",
            json={"sunset_date": "2999-01-31"},
            headers=bearer(stack.admin_token),
        )
        assert deprecated.status_code == 200, deprecated.text
        page = (await get_page(stack, w["contract"])).json()
        by_semver = {v["semver"]: v for v in page["versions"]}
        assert by_semver["2.3.0"]["deprecated"] is True
        assert by_semver["2.3.0"]["sunset_date"] == "2999-01-31"
        assert by_semver["3.0.0"]["deprecated"] is False


async def test_a_soft_deleted_contract_is_the_unknown_contract_404(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        org = await stack.chain()
        contract, _ = await stack.publish_versions(org.team, ["1.0.0"])
        deleted = await stack.client.delete(
            f"/api/v1/contracts/contracts/{contract}", headers=bearer(stack.admin_token)
        )
        assert deleted.status_code in (200, 204), deleted.text
        response = await get_page(stack, contract)
        assert response.status_code == 404
