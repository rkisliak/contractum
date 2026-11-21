"""Card notices (SUB-06 deprecation half, SUB-07 recording): lifecycle events reach pinned cards.

The audience travels inside the operation's own outbox row (`notify_card_ids`); handling that
event (`notices.handle_payload`, which the NATS consumer of Plan 09 calls, and which the tests
reach through `Stack.pump_notices`) records one notice per card and event with one pending
delivery per enabled channel. Handling the same event again changes nothing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]

FORBIDDEN_KEYS = {"document", "raw_document", "contacts", "members"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def today() -> Any:
    return datetime.now(UTC).date()


def action_url(contract: str, ref: str, action: str) -> str:
    return f"/api/v1/contracts/contracts/{contract}/versions/{ref}/{action}"


async def deprecate(stack: Any, contract: str, ref: str, day: Any) -> Any:
    return await stack.client.post(
        action_url(contract, ref, "deprecate"),
        json={"sunset_date": day.isoformat()},
        headers=bearer(stack.admin_token),
    )


def outbox(sql: Sql, stack: Any, action: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT event_id, occurred_at, before, after FROM outbox WHERE action = %s ORDER BY id",
        action,
    )


def count(sql: Sql, stack: Any, table: str) -> int:
    return int(sql(stack.contracts_db_url, f"SELECT count(*) AS n FROM {table}")[0]["n"])  # noqa: S608


def notices_of(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM card_notice WHERE card_id = %s ORDER BY id",
        card_id,
    )


def deliveries_of(sql: Sql, stack: Any, notice_id: Any) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM card_notice_delivery WHERE notice_id = %s ORDER BY channel",
        notice_id,
    )


def keys_of(value: Any) -> set[str]:
    """Every dict key in a JSON value, recursively."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= keys_of(item)
    elif isinstance(value, list):
        for item in value:
            found |= keys_of(item)
    return found


async def _world(stack: Any) -> dict[str, Any]:
    """orders 2.3.0 (Latest), 3.0.0 (Preview); A (email on) and B (off) pin 2.3.0, C pins 3.0.0."""
    chain = await stack.chain()
    contract, versions = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
    card_a = await stack.create_card(chain.team, email_enabled=True)
    card_b = await stack.create_card(chain.team)
    card_c = await stack.create_card(chain.team)
    for card, semver in ((card_a, "2.3.0"), (card_b, "2.3.0"), (card_c, "3.0.0")):
        pinned = await stack.subscribe(card["id"], contract, semver)
        assert pinned.status_code == 201, pinned.text
    return {
        "chain": chain,
        "contract": contract,
        "versions": versions,
        "a": card_a,
        "b": card_b,
        "c": card_c,
    }


async def latest_envelope(stack: Any, action: str) -> dict[str, Any] | None:
    """The newest outbox row of ``action`` as the relay would publish it (None when none)."""
    from dc_contracts.models import OutboxEvent
    from dc_core.events import build_envelope
    from sqlalchemy import select

    async with stack.contracts_app.state.session_factory() as session:
        row = (
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.action == action)
                    .order_by(OutboxEvent.id.desc())
                )
            )
            .scalars()
            .first()
        )
    return None if row is None else build_envelope(row, service="contracts")


# --- tracer: deprecating a pinned version names its cards, handling the event records notices ---


async def test_deprecating_a_pinned_version_names_the_cards_and_the_event_records_the_notices(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        day = today() + timedelta(days=60)

        response = await deprecate(stack, w["contract"], "2.3.0", day)
        assert response.status_code == 200, response.text
        assert response.json()["deprecated"] is True
        assert "deprecation" in response.headers
        assert "sunset" in response.headers
        events = outbox(sql, stack, "contract.version_deprecated")
        assert len(events) == 1
        after = events[0]["after"]
        assert after["notify_card_ids"] == sorted([w["a"]["id"], w["b"]["id"]])
        # every key the event carried before this plan is still there
        for key in ("id", "contract_id", "semver", "deprecated_at", "sunset_date"):
            assert key in after
        # Nothing is materialised inside the request itself.
        assert count(sql, stack, "card_notice") == 0

        assert await stack.pump_notices() == 2

        notice_a = notices_of(sql, stack, w["a"]["id"])
        assert len(notice_a) == 1
        notice = notice_a[0]
        assert notice["event_type"] == "contract.version.deprecated"
        assert str(notice["version_id"]) == w["versions"]["2.3.0"]
        assert str(notice["contract_id"]) == w["contract"]
        assert notice["occurred_at"] == events[0]["occurred_at"]
        assert str(notice["source_event_id"]) == str(events[0]["event_id"])
        assert notice["dedupe_key"] == f"event:{events[0]['event_id']}"
        payload = notice["payload"]
        assert payload["contract"]["id"] == w["contract"]
        assert payload["contract"]["odcs_id"]
        assert payload["version"] == {"id": w["versions"]["2.3.0"], "semver": "2.3.0"}
        assert payload["sunset_date"] == day.isoformat()
        assert payload["deprecated_at"]
        assert not keys_of(payload) & FORBIDDEN_KEYS

        delivery_a = deliveries_of(sql, stack, notice["id"])
        assert [(d["channel"], d["status"], d["attempts"]) for d in delivery_a] == [
            ("email", "pending", 0)
        ]

        notice_b = notices_of(sql, stack, w["b"]["id"])
        assert len(notice_b) == 1
        assert deliveries_of(sql, stack, notice_b[0]["id"]) == []
        assert notices_of(sql, stack, w["c"]["id"]) == []

        # Handling the same event again (redelivery) creates neither a notice nor a delivery.
        notices_before = count(sql, stack, "card_notice")
        deliveries_before = count(sql, stack, "card_notice_delivery")
        assert await stack.replay_event(events[0]["event_id"]) == 0
        assert count(sql, stack, "card_notice") == notices_before
        assert count(sql, stack, "card_notice_delivery") == deliveries_before
        assert await stack.pump_notices() == 0
        assert invariant(stack.contracts_db_url) == []


async def test_a_card_with_both_channels_gets_one_pending_delivery_per_channel(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        sql(
            stack.contracts_db_url,
            "UPDATE subscriber_card SET email_enabled = true, webhook_enabled = true, "
            "webhook_url = 'https://hooks.example.test/x' WHERE id = %s",
            w["b"]["id"],
        )
        assert (await deprecate(stack, w["contract"], "2.3.0", today())).status_code == 200
        assert await stack.pump_notices() == 2
        notice = notices_of(sql, stack, w["b"]["id"])[0]
        rows = deliveries_of(sql, stack, notice["id"])
        assert [(d["channel"], d["status"]) for d in rows] == [
            ("email", "pending"),
            ("webhook", "pending"),
        ]


async def test_the_notice_list_is_readable_by_readers_and_members_and_paged(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        card_id = w["a"]["id"]
        empty = await stack.client.get(
            f"{CARDS}/{card_id}/notices", headers=bearer(stack.admin_token)
        )
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"items": [], "next_cursor": None}

        assert (await deprecate(stack, w["contract"], "2.3.0", today())).status_code == 200
        later = await deprecate(stack, w["contract"], "2.3.0", today() + timedelta(days=5))
        assert later.status_code == 200, later.text
        assert await stack.pump_notices() == 4

        reader, reader_principal = stack.token("reader")
        stack.grant_sql(reader_principal, "team_reader", w["chain"].team)
        listed = await stack.client.get(f"{CARDS}/{card_id}/notices", headers=bearer(reader))
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert len(items) == 2
        assert items[0]["id"] > items[1]["id"]
        first = items[0]
        assert first["event_type"] == "contract.version.deprecated"
        assert first["contract_id"] == w["contract"]
        assert first["version_id"] == w["versions"]["2.3.0"]
        assert first["occurred_at"]
        assert first["created_at"]
        assert first["payload"]["version"]["semver"] == "2.3.0"
        assert [d["channel"] for d in first["deliveries"]] == ["email"]
        delivery = first["deliveries"][0]
        assert delivery["status"] == "pending"
        assert delivery["attempts"] == 0
        assert delivery["last_attempt_at"] is None
        assert delivery["delivered_at"] is None
        assert delivery["last_error"] is None

        paged = await stack.client.get(
            f"{CARDS}/{card_id}/notices", params={"limit": 1}, headers=bearer(reader)
        )
        assert paged.status_code == 200
        assert len(paged.json()["items"]) == 1
        assert paged.json()["items"][0]["id"] == items[0]["id"]
        cursor = paged.json()["next_cursor"]
        assert cursor
        rest = await stack.client.get(
            f"{CARDS}/{card_id}/notices",
            params={"limit": 1, "cursor": cursor},
            headers=bearer(reader),
        )
        assert [i["id"] for i in rest.json()["items"]] == [items[1]["id"]]
        assert rest.json()["next_cursor"] is None

        # a linked member with no role at the card's folder reads the notices too
        group = await stack.local_group(stack.root_id, f"g-{uuid.uuid4().hex[:8]}")
        assert (await stack.link_member(card_id, "local_group", group)).status_code == 201
        member, member_principal = stack.token("member")
        stack.group_member_sql(group, member_principal)
        as_member = await stack.client.get(f"{CARDS}/{card_id}/notices", headers=bearer(member))
        assert as_member.status_code == 200, as_member.text
        assert len(as_member.json()["items"]) == 2

        # everyone else gets the unknown-card 404, and the same body as for an unknown id
        outsider, _ = stack.token("outsider")
        hidden = await stack.client.get(f"{CARDS}/{card_id}/notices", headers=bearer(outsider))
        unknown = await stack.client.get(
            f"{CARDS}/{uuid.uuid4()}/notices", headers=bearer(outsider)
        )
        assert hidden.status_code == 404
        stable = ("type", "title", "status", "detail")
        assert {k: hidden.json()[k] for k in stable} == {k: unknown.json()[k] for k in stable}
        anonymous = await stack.client.get(f"{CARDS}/{card_id}/notices")
        assert anonymous.status_code == 401
        bad_cursor = await stack.client.get(
            f"{CARDS}/{card_id}/notices", params={"cursor": "nope"}, headers=bearer(reader)
        )
        assert bad_cursor.status_code == 422
        # the record never leaks across cards
        other = await stack.client.get(
            f"{CARDS}/{w['c']['id']}/notices", headers=bearer(stack.admin_token)
        )
        assert other.json() == {"items": [], "next_cursor": None}


async def test_a_deprecation_with_no_pinned_card_writes_an_empty_audience_and_no_notice(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await stack.publish_versions(chain.team, ["1.0.0", "1.1.0"])
        response = await deprecate(stack, contract, "1.0.0", today())
        assert response.status_code == 200, response.text
        events = outbox(sql, stack, "contract.version_deprecated")
        assert events[0]["after"]["notify_card_ids"] == []
        assert await stack.pump_notices() == 0
        assert count(sql, stack, "card_notice") == 0


async def test_the_notice_payload_is_json_with_identifiers_only(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        assert (await deprecate(stack, w["contract"], "2.3.0", today())).status_code == 200
        await stack.pump_notices()
        body = (
            await stack.client.get(
                f"{CARDS}/{w['a']['id']}/notices", headers=bearer(stack.admin_token)
            )
        ).text
        assert not keys_of(json.loads(body)) & FORBIDDEN_KEYS


# --- publishing and Latest changes reach every card pinned to the contract (D-101) ---------------


async def _lifecycle_world(stack: Any) -> dict[str, Any]:
    """orders 2.3.0 (Latest), 3.0.0 (Preview); A pins both, E pins 3.0.0, D another contract."""
    chain = await stack.chain()
    odcs_id = f"urn:dc:notices:{uuid.uuid4().hex}"
    contract, versions = await stack.publish_versions(
        chain.team, ["2.3.0", "3.0.0"], odcs_id=odcs_id
    )
    other, _ = await stack.publish_versions(chain.team, ["1.0.0"])
    card_a = await stack.create_card(chain.team, email_enabled=True)
    card_d = await stack.create_card(chain.team)
    card_e = await stack.create_card(chain.team)
    for card, ref_contract, semver in (
        (card_a, contract, "2.3.0"),
        (card_a, contract, "3.0.0"),
        (card_e, contract, "3.0.0"),
        (card_d, other, "1.0.0"),
    ):
        pinned = await stack.subscribe(card["id"], ref_contract, semver)
        assert pinned.status_code == 201, pinned.text
    return {
        "chain": chain,
        "odcs_id": odcs_id,
        "contract": contract,
        "versions": versions,
        "a": card_a,
        "d": card_d,
        "e": card_e,
    }


def odcs_doc(odcs_id: str, *, version: str) -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        "status: draft\nname: Orders\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


async def publish_next(stack: Any, w: dict[str, Any], base: str, semver: str) -> Any:
    """Draft from ``base``, set the declared version to ``semver`` and publish; returns the 201."""
    path = f"/api/v1/contracts/contracts/{w['contract']}"
    created = await stack.client.post(
        f"{path}/drafts",
        json={"from_version": base, "bump": "none"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        f"{path}/drafts/{draft['id']}",
        content=odcs_doc(w["odcs_id"], version=semver),
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": draft["etag"],
        },
    )
    assert saved.status_code == 200, saved.text
    return await stack.publish(w["contract"], draft["id"], saved.headers["etag"])


async def promote(stack: Any, contract: str, ref: str) -> Any:
    return await stack.client.post(
        action_url(contract, ref, "promote"), headers=bearer(stack.admin_token)
    )


async def test_publishing_a_version_notifies_every_card_pinned_to_the_contract_once(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _lifecycle_world(stack)
        assert await stack.pump_notices() == 0
        published = await publish_next(stack, w, "3.0.0", "3.1.0")
        assert published.status_code == 201, published.text

        events = outbox(sql, stack, "contract.version_published")
        newest = events[-1]["after"]
        assert newest["notify_card_ids"] == sorted([w["a"]["id"], w["e"]["id"]])
        assert newest["semver"] == "3.1.0"
        assert newest["status"] == "preview"

        assert await stack.pump_notices() == 2
        mine = notices_of(sql, stack, w["a"]["id"])
        assert len(mine) == 1
        assert mine[0]["event_type"] == "contract.version.published"
        assert str(mine[0]["version_id"]) == published.json()["version_id"]
        assert mine[0]["payload"]["version"]["semver"] == "3.1.0"
        assert mine[0]["payload"]["status"] == "preview"
        assert not keys_of(mine[0]["payload"]) & FORBIDDEN_KEYS
        assert [d["channel"] for d in deliveries_of(sql, stack, mine[0]["id"])] == ["email"]
        assert len(notices_of(sql, stack, w["e"]["id"])) == 1
        assert notices_of(sql, stack, w["d"]["id"]) == []

        before = (count(sql, stack, "card_notice"), count(sql, stack, "card_notice_delivery"))
        assert await stack.replay_event(events[-1]["event_id"]) == 0
        assert (
            count(sql, stack, "card_notice"),
            count(sql, stack, "card_notice_delivery"),
        ) == before


async def test_promoting_a_version_notifies_pinned_cards_and_a_no_op_notifies_nobody(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _lifecycle_world(stack)
        assert await stack.pump_notices() == 0
        promoted = await promote(stack, w["contract"], "3.0.0")
        assert promoted.status_code == 200, promoted.text
        events = outbox(sql, stack, "contract.version_promoted")
        assert len(events) == 1
        assert events[0]["after"]["notify_card_ids"] == sorted([w["a"]["id"], w["e"]["id"]])

        assert await stack.pump_notices() == 2
        mine = notices_of(sql, stack, w["a"]["id"])
        assert len(mine) == 1
        assert mine[0]["event_type"] == "contract.latest.changed"
        assert str(mine[0]["version_id"]) == w["versions"]["3.0.0"]
        assert mine[0]["payload"]["version"] == {"id": w["versions"]["3.0.0"], "semver": "3.0.0"}
        assert mine[0]["payload"]["previous_latest"] == {
            "id": w["versions"]["2.3.0"],
            "semver": "2.3.0",
        }

        again = await promote(stack, w["contract"], "3.0.0")
        assert again.status_code == 200, again.text
        assert len(outbox(sql, stack, "contract.version_promoted")) == 1
        assert await stack.pump_notices() == 0
        assert len(notices_of(sql, stack, w["a"]["id"])) == 1

        notices_before = count(sql, stack, "card_notice")
        deliveries_before = count(sql, stack, "card_notice_delivery")
        assert await stack.replay_event(events[0]["event_id"]) == 0
        assert count(sql, stack, "card_notice") == notices_before
        assert count(sql, stack, "card_notice_delivery") == deliveries_before


async def test_a_card_pinned_to_two_versions_is_listed_once_and_gets_one_notice_per_event(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _lifecycle_world(stack)
        assert (await deprecate(stack, w["contract"], "3.0.0", today())).status_code == 200
        # card A pins 2.3.0 and 3.0.0, but only 3.0.0 is deprecated: A once, E once
        deprecated = outbox(sql, stack, "contract.version_deprecated")[0]["after"]
        assert deprecated["notify_card_ids"] == sorted([w["a"]["id"], w["e"]["id"]])
        published = await publish_next(stack, w, "3.0.0", "3.1.0")
        assert published.status_code == 201, published.text
        listed = outbox(sql, stack, "contract.version_published")[-1]["after"]["notify_card_ids"]
        assert listed.count(w["a"]["id"]) == 1
        assert await stack.pump_notices() == 4
        assert len(notices_of(sql, stack, w["a"]["id"])) == 2


async def test_the_first_publish_of_a_new_contract_notifies_nobody_and_answers_as_before(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await stack.publish_versions(chain.team, ["1.0.0"])
        events = outbox(sql, stack, "contract.version_published")
        assert events[-1]["after"]["notify_card_ids"] == []
        assert events[-1]["after"]["status"] == "latest"
        assert await stack.pump_notices() == 0
        assert count(sql, stack, "card_notice") == 0
        fetched = await stack.client.get(
            f"/api/v1/contracts/contracts/{contract}/versions/1.0.0",
            headers=bearer(stack.admin_token),
        )
        assert fetched.status_code == 200


async def test_an_archived_card_is_not_in_the_audience(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        w = await _lifecycle_world(stack)
        archived = await stack.client.post(
            f"{CARDS}/{w['e']['id']}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code == 200, archived.text
        assert (await deprecate(stack, w["contract"], "3.0.0", today())).status_code == 200
        after = outbox(sql, stack, "contract.version_deprecated")[0]["after"]
        assert after["notify_card_ids"] == [w["a"]["id"]]
        assert await stack.pump_notices() == 1
        assert notices_of(sql, stack, w["e"]["id"]) == []


async def test_a_failed_commit_leaves_no_event_and_no_notice_and_a_retry_records_it(
    make_stack: MakeStack, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dc_core.uow import UnitOfWork

    async with make_stack() as stack:
        w = await _world(stack)
        await stack.pump_notices()
        events_before = count(sql, stack, "outbox")

        async def failing_commit(self: UnitOfWork) -> None:
            await self.session.rollback()
            raise RuntimeError("injected commit failure")

        with monkeypatch.context() as patch:
            patch.setattr(UnitOfWork, "commit", failing_commit)
            with pytest.raises(RuntimeError, match="injected commit failure"):
                await deprecate(stack, w["contract"], "2.3.0", today())
        assert count(sql, stack, "outbox") == events_before
        assert outbox(sql, stack, "contract.version_deprecated") == []
        flagged = sql(
            stack.contracts_db_url,
            "SELECT deprecated_at FROM contract_version WHERE contract_id = %s AND semver = %s",
            w["contract"],
            "2.3.0",
        )
        assert flagged[0]["deprecated_at"] is None
        assert await stack.pump_notices() == 0
        assert count(sql, stack, "card_notice") == 0

        retried = await deprecate(stack, w["contract"], "2.3.0", today())
        assert retried.status_code == 200, retried.text
        assert await stack.pump_notices() == 2


async def test_notice_recording_never_changes_or_fails_a_lifecycle_operation(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _lifecycle_world(stack)
        # Break the notice tables: nothing in a lifecycle request may touch them.
        sql(stack.contracts_db_url, "ALTER TABLE card_notice RENAME TO card_notice_away")
        sql(
            stack.contracts_db_url,
            "ALTER TABLE card_notice_delivery RENAME TO card_notice_delivery_away",
        )
        try:
            assert (await publish_next(stack, w, "3.0.0", "3.1.0")).status_code == 201
            assert (await promote(stack, w["contract"], "3.0.0")).status_code == 200
            assert (await deprecate(stack, w["contract"], "2.3.0", today())).status_code == 200
        finally:
            sql(stack.contracts_db_url, "ALTER TABLE card_notice_away RENAME TO card_notice")
            sql(
                stack.contracts_db_url,
                "ALTER TABLE card_notice_delivery_away RENAME TO card_notice_delivery",
            )
        # the three events were written, so the notices can still be recorded afterwards
        assert await stack.pump_notices() > 0


async def test_handle_payload_refuses_foreign_or_misrouted_envelopes_and_ignores_the_rest(
    make_stack: MakeStack, sql: Sql
) -> None:
    from dc_contracts.notices import InvalidNoticeEvent, handle_payload
    from dc_core.events import encode_envelope, subject_for

    async with make_stack() as stack:
        w = await _world(stack)
        assert (await deprecate(stack, w["contract"], "2.3.0", today())).status_code == 200
        factory = stack.contracts_app.state.session_factory
        deprecated = await latest_envelope(stack, "contract.version_deprecated")
        assert deprecated is not None
        subject = subject_for("contracts", "contract", "contract.version_deprecated")

        spoof = {**deprecated, "service": "identity"}
        with pytest.raises(InvalidNoticeEvent):
            await handle_payload(
                factory,
                encode_envelope(spoof),
                subject=subject_for("identity", "contract", "contract.version_deprecated"),
            )
        misrouted = {**deprecated, "action": "contract.version_published"}
        with pytest.raises(InvalidNoticeEvent):
            await handle_payload(factory, encode_envelope(misrouted), subject=subject)
        with pytest.raises(InvalidNoticeEvent):
            await handle_payload(factory, b"not json", subject=subject)
        with pytest.raises(InvalidNoticeEvent):
            await handle_payload(factory, encode_envelope(deprecated), subject="audit.x.y.z")
        assert count(sql, stack, "card_notice") == 0

        created = await latest_envelope(stack, "contract.created")
        assert created is not None
        assert (
            await handle_payload(
                factory,
                encode_envelope(created),
                subject=subject_for("contracts", "contract", "contract.created"),
            )
            == 0
        )

        # a pre-phase lifecycle event has no audience: nothing is recorded
        legacy = {
            **deprecated,
            "after": {k: v for k, v in deprecated["after"].items() if k != "notify_card_ids"},
        }
        assert await handle_payload(factory, encode_envelope(legacy), subject=subject) == 0
        assert count(sql, stack, "card_notice") == 0
        assert count(sql, stack, "card_notice_delivery") == 0

        # the real event still records
        assert await handle_payload(factory, encode_envelope(deprecated), subject=subject) == 2


async def test_a_lifecycle_envelope_with_a_missing_or_malformed_id_is_invalid_not_retried(
    make_stack: MakeStack, sql: Sql
) -> None:
    # IN-01: the consumer acknowledges an InvalidNoticeEvent once; any other error is retried.
    from dc_contracts.notices import InvalidNoticeEvent, handle_payload
    from dc_core.events import encode_envelope, subject_for

    async with make_stack() as stack:
        w = await _world(stack)
        assert (await deprecate(stack, w["contract"], "2.3.0", today())).status_code == 200
        factory = stack.contracts_app.state.session_factory
        deprecated = await latest_envelope(stack, "contract.version_deprecated")
        assert deprecated is not None
        cards = deprecated["after"]["notify_card_ids"]
        assert cards

        def variant(action: str, after: dict[str, Any]) -> tuple[bytes, str]:
            envelope = {
                **deprecated,
                "action": action,
                "after": {"notify_card_ids": cards, **after},
            }
            return encode_envelope(envelope), subject_for("contracts", "contract", action)

        bad_ids: list[dict[str, Any]] = [{}, {"id": "not-a-uuid"}, {"id": None}]
        cases: list[tuple[bytes, str]] = []
        for after in bad_ids:
            cases.append(variant("contract.version_deprecated", after))
            cases.append(variant("contract.version_published", after))
        for version in ({}, {"id": "not-a-uuid"}):
            cases.append(variant("contract.version_promoted", {"version": version}))
        cases.append(variant("contract.version_promoted", {}))

        for payload, subject in cases:
            with pytest.raises(InvalidNoticeEvent):
                await handle_payload(factory, payload, subject=subject)
        assert count(sql, stack, "card_notice") == 0


async def test_an_event_naming_rows_that_do_not_exist_is_invalid_or_ignored(
    make_stack: MakeStack, sql: Sql
) -> None:
    from dc_contracts.notices import InvalidNoticeEvent, handle_payload
    from dc_core.events import encode_envelope, subject_for

    async with make_stack() as stack:
        w = await _world(stack)
        assert (await deprecate(stack, w["contract"], "2.3.0", today())).status_code == 200
        factory = stack.contracts_app.state.session_factory
        deprecated = await latest_envelope(stack, "contract.version_deprecated")
        assert deprecated is not None
        pinned_version = deprecated["after"]["id"]
        published_subject = subject_for("contracts", "contract", "contract.version_published")

        def publish_event(new_version: str, pinned: str) -> bytes:
            pins = [{"card_id": w["a"]["id"], "version_id": pinned}]
            return encode_envelope(
                {
                    **deprecated,
                    "action": "contract.version_published",
                    "after": {"id": new_version, "semver": "4.0.0", "notify_pins": pins},
                }
            )

        # A deprecation of a contract that does not exist (deleted rows are never hard-deleted,
        # so this is a forged or corrupted event): no retry can make it valid.
        ghost = encode_envelope({**deprecated, "aggregate_id": str(uuid.uuid4())})
        with pytest.raises(InvalidNoticeEvent, match="contract that does not exist"):
            await handle_payload(
                factory,
                ghost,
                subject=subject_for("contracts", "contract", "contract.version_deprecated"),
            )

        # Pins on a version of another contract: nothing of this contract to compare against.
        foreign = publish_event(str(uuid.uuid4()), str(uuid.uuid4()))
        assert await handle_payload(factory, foreign, subject=published_subject) == 0

        # A real pin, but the published version itself is not there to compare with.
        missing_new = publish_event(str(uuid.uuid4()), pinned_version)
        with pytest.raises(InvalidNoticeEvent, match="version that does not exist"):
            await handle_payload(factory, missing_new, subject=published_subject)

        assert count(sql, stack, "card_notice") == 0
