"""SUB-07 / D-96: the sunset-approaching scheduler records one notice per pin per lead time.

Tests drive ``app.state.sunset_scheduler.scan_once(today=...)`` by hand; ``make_stack`` keeps the
background loop off. Dates are UTC calendar dates, passed in so no test depends on the clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"
SUNSET_EVENT = "contract.version.sunset_approaching"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def today_plus(days: int) -> date:
    return (datetime.now(UTC) + timedelta(days=days)).date()


def scheduler(stack: Any) -> Any:
    return stack.contracts_app.state.sunset_scheduler


async def world(stack: Any, *, email_enabled: bool = True) -> dict[str, Any]:
    """Contract 2.3.0 (Latest) and 3.0.0; an email card pins 2.3.0."""
    chain = await stack.chain()
    contract, versions = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
    token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "consumer", chain.team)
    card = await stack.create_card(
        chain.team,
        token=token,
        name="orders consumers",
        contacts=[{"kind": "email", "value": "a@example.test"}],
        email_enabled=email_enabled,
    )
    pinned = await stack.subscribe(card["id"], contract, "2.3.0")
    assert pinned.status_code == 201, pinned.text
    return {
        "chain": chain,
        "contract": contract,
        "versions": versions,
        "card": card,
        "manager": token,
        "pin": pinned.json()["id"],
    }


async def deprecate(stack: Any, contract: str, sunset: date, ref: str = "2.3.0") -> httpx.Response:
    response = await stack.client.post(
        f"/api/v1/contracts/contracts/{contract}/versions/{ref}/deprecate",
        json={"sunset_date": sunset.isoformat()},
        headers=bearer(stack.admin_token),
    )
    assert response.status_code == 200, response.text
    return response


def count(sql: Sql, stack: Any, table: str) -> int:
    return int(sql(stack.contracts_db_url, f"SELECT count(*) AS n FROM {table}")[0]["n"])  # noqa: S608


def sunset_notices(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM card_notice WHERE card_id = %s AND event_type = %s ORDER BY id",
        card_id,
        SUNSET_EVENT,
    )


# --- tracer: a card pinned to a version 30 days from sunset gets one notice ---------------------


async def test_a_pinned_version_thirty_days_from_sunset_notices_the_card_once(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        sunset = today_plus(30)
        await deprecate(stack, w["contract"], sunset)

        assert await scheduler(stack).scan_once() == 1

        (notice,) = sunset_notices(sql, stack, w["card"]["id"])
        assert notice["payload"]["lead_days"] == 30
        assert notice["payload"]["days_left"] == 30
        assert notice["payload"]["sunset_date"] == sunset.isoformat()
        assert notice["payload"]["version"]["semver"] == "2.3.0"
        assert notice["version_id"] is not None
        assert notice["dedupe_key"] == f"sunset:{w['pin']}:30:{sunset.isoformat()}"
        assert str(notice["version_id"]) == w["versions"]["2.3.0"]

        deliveries = sql(
            stack.contracts_db_url,
            "SELECT channel, status FROM card_notice_delivery WHERE notice_id = %s",
            notice["id"],
        )
        assert deliveries == [{"channel": "email", "status": "pending"}]

        notices, delivery_rows = (
            count(sql, stack, "card_notice"),
            count(sql, stack, "card_notice_delivery"),
        )
        assert await scheduler(stack).scan_once() == 0
        assert count(sql, stack, "card_notice") == notices
        assert count(sql, stack, "card_notice_delivery") == delivery_rows


async def test_the_test_stack_starts_no_scheduler_loop(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        assert scheduler(stack) is not None
        assert stack.contracts_app.state.settings.sunset_scheduler_enabled is False


# --- the lead rule over a pin's whole window, re-dating and the cases that record nothing --------


async def test_a_pin_held_through_the_window_is_warned_at_thirty_and_at_seven_days(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        sunset = today_plus(30)
        await deprecate(stack, w["contract"], sunset)

        assert await scheduler(stack).scan_once(today=sunset - timedelta(days=30)) == 1
        assert await scheduler(stack).scan_once(today=sunset - timedelta(days=7)) == 1
        assert await scheduler(stack).scan_once(today=sunset - timedelta(days=3)) == 0
        assert await scheduler(stack).scan_once(today=sunset - timedelta(days=7)) == 0

        notices = sunset_notices(sql, stack, w["card"]["id"])
        assert [n["payload"]["lead_days"] for n in notices] == [30, 7]
        assert [n["payload"]["days_left"] for n in notices] == [30, 7]


async def test_a_pin_created_three_days_before_the_sunset_gets_exactly_one_notice(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        sunset = today_plus(10)
        # A deprecated version cannot be newly pinned, so the second pin comes first.
        late = await stack.create_card(
            w["chain"].team, token=w["manager"], name="late consumers", email_enabled=True
        )
        pinned = await stack.subscribe(late["id"], w["contract"], "2.3.0", token=w["manager"])
        assert pinned.status_code == 201, pinned.text
        await deprecate(stack, w["contract"], sunset)

        today = sunset - timedelta(days=3)
        assert await scheduler(stack).scan_once(today=today) == 2  # both cards, lead 7
        assert await scheduler(stack).scan_once(today=today) == 0
        (notice,) = sunset_notices(sql, stack, late["id"])
        assert notice["payload"]["lead_days"] == 7
        assert notice["payload"]["days_left"] == 3


async def test_a_re_dated_sunset_warns_the_pin_again(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        await deprecate(stack, w["contract"], today_plus(30))
        assert await scheduler(stack).scan_once() == 1
        assert await scheduler(stack).scan_once() == 0

        later = today_plus(20)
        await deprecate(stack, w["contract"], later)
        assert await scheduler(stack).scan_once() == 1  # d = 20 -> lead 30, a new date in the key

        keys = [n["dedupe_key"] for n in sunset_notices(sql, stack, w["card"]["id"])]
        assert len(keys) == 2
        assert keys[1] == f"sunset:{w['pin']}:30:{later.isoformat()}"


async def test_a_sunset_further_away_than_the_largest_lead_records_nothing(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        await deprecate(stack, w["contract"], today_plus(60))
        assert await scheduler(stack).scan_once() == 0
        assert sunset_notices(sql, stack, w["card"]["id"]) == []


async def test_an_ended_pin_records_nothing(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        await deprecate(stack, w["contract"], today_plus(30))
        ended = await stack.client.post(
            f"{CARDS}/{w['card']['id']}/subscriptions/{w['pin']}/unsubscribe",
            json={},
            headers=bearer(w["manager"]),
        )
        assert ended.status_code == 200, ended.text
        assert await scheduler(stack).scan_once() == 0
        assert sunset_notices(sql, stack, w["card"]["id"]) == []


async def test_an_archived_card_records_nothing(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        await deprecate(stack, w["contract"], today_plus(30))
        archived = await stack.client.post(
            f"{CARDS}/{w['card']['id']}/archive", headers=bearer(w["manager"])
        )
        assert archived.status_code in (200, 204), archived.text
        assert await scheduler(stack).scan_once() == 0
        assert sunset_notices(sql, stack, w["card"]["id"]) == []


async def test_promote_clears_the_deprecation_so_nothing_is_recorded(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        pinned = await stack.subscribe(w["card"]["id"], w["contract"], "3.0.0")
        assert pinned.status_code == 201, pinned.text
        await deprecate(stack, w["contract"], today_plus(30), ref="3.0.0")
        promoted = await stack.client.post(
            f"/api/v1/contracts/contracts/{w['contract']}/versions/3.0.0/promote",
            headers=bearer(stack.admin_token),
        )
        assert promoted.status_code == 200, promoted.text
        assert await scheduler(stack).scan_once() == 0
        assert sunset_notices(sql, stack, w["card"]["id"]) == []


async def test_a_card_with_both_channels_off_still_records_the_notice_without_deliveries(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await world(stack, email_enabled=False)
        await deprecate(stack, w["contract"], today_plus(30))
        assert await scheduler(stack).scan_once() == 1
        assert count(sql, stack, "card_notice_delivery") == 0


# --- replicas ----------------------------------------------------------------------------------


async def test_concurrent_scans_add_no_duplicate_notices_or_delivery_rows(
    make_stack: MakeStack, sql: Sql
) -> None:
    import asyncio

    from dc_contracts.scheduler import SunsetScheduler

    async with make_stack() as stack:
        w = await world(stack)
        for index in range(3):
            card = await stack.create_card(
                w["chain"].team, token=w["manager"], name=f"extra {index}", email_enabled=True
            )
            pinned = await stack.subscribe(card["id"], w["contract"], "2.3.0", token=w["manager"])
            assert pinned.status_code == 201, pinned.text
        await deprecate(stack, w["contract"], today_plus(30))

        state = stack.contracts_app.state
        replicas = [SunsetScheduler(state.session_factory, state.settings) for _ in range(4)]
        inserted = await asyncio.gather(*(replica.scan_once() for replica in replicas))

        assert sum(inserted) == 4  # four active pins, one notice each, however many replicas ran
        assert count(sql, stack, "card_notice") == 4
        assert count(sql, stack, "card_notice_delivery") == 4
        # The delivery rows belong to exactly the notices that exist (none for a lost conflict).
        orphans = sql(
            stack.contracts_db_url,
            "SELECT count(*) AS n FROM card_notice_delivery d "
            "LEFT JOIN card_notice n ON n.id = d.notice_id WHERE n.id IS NULL",
        )
        assert orphans[0]["n"] == 0

        assert sum(await asyncio.gather(*(r.scan_once() for r in replicas))) == 0
        assert count(sql, stack, "card_notice") == 4
        assert count(sql, stack, "card_notice_delivery") == 4
