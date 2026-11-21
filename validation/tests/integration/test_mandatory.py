"""Mandatory rule sets over HTTP: the flag, the computed warn-then-enforce mode, enforce-now,
clear-then-set (D-142) and "no lower level can weaken it" (plan 05-09 task 1; RULE-05, RULE-08).

Time is never waited for: a test moves a version's ``enforce_at`` with SQL to stand for "the warn
period has passed" (``enforce_at`` is outside the immutability trigger's column list).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def rule_body(message: str) -> str:
    return json.dumps(
        {
            "target": "$.schema[*]",
            "condition": UNIQUE_NAMES,
            "severity": "error",
            "message": message,
            "fixtures": {
                "pass": [{"document": table("id", "name")}],
                "fail": [{"document": table("id", "id")}],
            },
        }
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def setup(stack: Any) -> tuple[Any, str, str, str]:
    """A chain with a Policy Manager at org A and a team editor at team T (below A)."""
    chain = await stack.chain()
    pam, pam_id = stack.token("pam")
    stack.grant_sql(pam_id, "policy_manager", chain.org)
    ted, ted_id = stack.token("ted")
    stack.grant_sql(ted_id, "team_editor", chain.team)
    return chain, pam, ted, pam_id


async def publish_next(stack: Any, rule_set_id: str, token: str, number: int) -> None:
    current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=token)
    put = await stack.put_rule(
        rule_set_id,
        "names-unique",
        rule_body(f"Message {number}"),
        current.headers["ETag"],
        token=token,
    )
    assert put.status_code == 200, put.text
    published = await stack.publish(rule_set_id, put.headers["ETag"], token=token)
    assert published.status_code == 201, published.text


async def new_set(
    stack: Any, folder: str, token: str, versions: int, name: str = "Org naming"
) -> str:
    created = await stack.create_rule_set(folder, name, token=token)
    assert created.status_code == 201, created.text
    rule_set_id: str = created.json()["id"]
    for number in range(1, versions + 1):
        await publish_next(stack, rule_set_id, token, number)
    return rule_set_id


async def mandate(stack: Any, rule_set_id: str, token: str, value: bool = True) -> Any:
    return await stack.client.put(
        f"{BASE}/rule-sets/{rule_set_id}/mandatory", json={"mandatory": value}, headers=auth(token)
    )


async def enforce_now(stack: Any, rule_set_id: str, version_no: int, token: str) -> Any:
    return await stack.client.post(
        f"{BASE}/rule-sets/{rule_set_id}/versions/{version_no}/enforce-now", headers=auth(token)
    )


async def reschedule(
    stack: Any, rule_set_id: str, version_no: int, token: str, when: datetime | str
) -> Any:
    value = when if isinstance(when, str) else when.isoformat()
    return await stack.client.put(
        f"{BASE}/rule-sets/{rule_set_id}/versions/{version_no}/enforce-at",
        json={"enforce_at": value},
        headers=auth(token),
    )


async def view(stack: Any, folder: str, token: str, rule_set_id: str) -> dict[str, Any]:
    response = await stack.get(f"{BASE}/folders/{folder}/available-rule-sets", token=token)
    assert response.status_code == 200, response.text
    found = [i for i in response.json()["items"] if i["id"] == rule_set_id]
    assert len(found) == 1
    entry: dict[str, Any] = found[0]
    return entry


def modes(entry: dict[str, Any]) -> list[tuple[int, str]]:
    return [(r["version_no"], r["mode"]) for r in entry["resolved"]]


def enforce_at(stack: Any, rule_set_id: str) -> dict[int, datetime | None]:
    rows = stack.validation_sql(
        "SELECT version_no, enforce_at FROM rule_set_version WHERE rule_set_id = %s", rule_set_id
    )
    return {row["version_no"]: row["enforce_at"] for row in rows}


def events(stack: Any, action: str) -> list[dict[str, Any]]:
    return stack.validation_sql(
        "SELECT before, after FROM outbox WHERE action = %s ORDER BY id", action
    )


async def test_set_warns_then_enforce_now_and_repeating_either_changes_nothing(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)

        set_ = await mandate(stack, rule_set_id, pam)
        assert set_.status_code == 200, set_.text
        assert set_.json()["mandatory"] is True
        assert set_.json()["mandatory_set_by"] == "fake:pam"
        stamps = enforce_at(stack, rule_set_id)
        delta = stack.validation_sql(
            "SELECT abs(extract(epoch FROM (%s::timestamptz - now())) - 14 * 86400) AS off",
            stamps[1],
        )[0]["off"]
        assert delta < 5, delta  # the database clock plus 14 days
        rows = events(stack, "ruleset.mandatory_set")
        assert len(rows) == 1
        assert rows[0]["after"]["mandatory"] is True
        assert rows[0]["after"]["version_no"] == 1
        assert rows[0]["after"]["restored_version_no"] is None
        assert rows[0]["after"]["enforce_at"] is not None
        assert "condition" not in json.dumps(rows[0]) and "Message" not in json.dumps(rows[0])

        entry = await view(stack, chain.team, ted, rule_set_id)
        assert entry["mandatory"] is True
        assert modes(entry) == [(1, "warn")]
        assert entry["resolved"][0]["enforce_at"] is not None

        again = await mandate(stack, rule_set_id, pam)
        assert again.status_code == 200, again.text
        assert enforce_at(stack, rule_set_id) == stamps  # no extension, no restart
        assert len(events(stack, "ruleset.mandatory_set")) == 1

        early = await enforce_now(stack, rule_set_id, 1, pam)
        assert early.status_code == 200, early.text
        assert early.json()["enforce_at"] is not None
        assert len(events(stack, "ruleset.enforced_early")) == 1
        assert enforce_at(stack, rule_set_id)[1] < stamps[1]  # type: ignore[operator]
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [(1, "enforce")]

        stamp = enforce_at(stack, rule_set_id)
        repeat = await enforce_now(stack, rule_set_id, 1, pam)
        assert repeat.status_code == 200, repeat.text
        assert enforce_at(stack, rule_set_id) == stamp
        assert len(events(stack, "ruleset.enforced_early")) == 1
        early_event = events(stack, "ruleset.enforced_early")[0]
        assert early_event["after"]["previous_enforce_at"] is not None


async def test_every_new_version_warns_while_the_enforced_one_keeps_blocking(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        assert (await enforce_now(stack, rule_set_id, 1, pam)).status_code == 200

        await publish_next(stack, rule_set_id, pam, 2)
        v2 = enforce_at(stack, rule_set_id)[2]
        assert v2 is not None
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [
            (1, "enforce"),
            (2, "warn"),
        ]
        await publish_next(stack, rule_set_id, pam, 3)
        assert enforce_at(stack, rule_set_id)[3] is not None
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [
            (1, "enforce"),
            (2, "warn"),
            (3, "warn"),
        ]

        # v3 is enforced early while v2 still warns: an older warn version is superseded.
        assert (await enforce_now(stack, rule_set_id, 3, pam)).status_code == 200
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [(3, "enforce")]

        # Time passes for v2 (it would be enforced) but v3 is newer and enforced: v3 alone.
        stack.validation_sql(
            "UPDATE rule_set_version SET enforce_at = now() - interval '1 minute' "
            "WHERE rule_set_id = %s AND version_no = 2 RETURNING 1",
            rule_set_id,
        )
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [(3, "enforce")]


async def test_the_warn_window_moves_to_the_next_version_as_time_passes(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 3)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        # Only the newest version carries a date after a first set; give v1 and v2 theirs.
        stack.validation_sql(
            "UPDATE rule_set_version SET enforce_at = now() - interval '1 day' "
            "WHERE rule_set_id = %s AND version_no = 1 RETURNING 1",
            rule_set_id,
        )
        stack.validation_sql(
            "UPDATE rule_set_version SET enforce_at = now() + interval '1 hour' "
            "WHERE rule_set_id = %s AND version_no = 2 RETURNING 1",
            rule_set_id,
        )
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [
            (1, "enforce"),
            (2, "warn"),
            (3, "warn"),
        ]
        stack.validation_sql(
            "UPDATE rule_set_version SET enforce_at = now() - interval '1 second' "
            "WHERE rule_set_id = %s AND version_no = 2 RETURNING 1",
            rule_set_id,
        )
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [
            (2, "enforce"),
            (3, "warn"),
        ]


async def test_clear_records_the_enforced_version_and_set_restores_it_at_once(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        assert (await enforce_now(stack, rule_set_id, 1, pam)).status_code == 200
        await publish_next(stack, rule_set_id, pam, 2)
        await publish_next(stack, rule_set_id, pam, 3)

        cleared = await mandate(stack, rule_set_id, pam, False)
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["mandatory"] is False
        assert all(value is None for value in enforce_at(stack, rule_set_id).values())
        row = stack.validation_sql(
            "SELECT cleared_enforced_version_no FROM rule_set WHERE id = %s", rule_set_id
        )[0]
        assert row["cleared_enforced_version_no"] == 1
        cleared_events = events(stack, "ruleset.mandatory_cleared")
        assert len(cleared_events) == 1
        assert cleared_events[0]["after"]["enforced_version_no"] == 1
        assert cleared_events[0]["after"]["warn_version_nos"] == [2, 3]
        entry = await view(stack, chain.team, ted, rule_set_id)
        assert entry["mandatory"] is False and entry["resolved"] == []
        # A second clear changes nothing.
        assert (await mandate(stack, rule_set_id, pam, False)).status_code == 200
        assert len(events(stack, "ruleset.mandatory_cleared")) == 1

        reset = await mandate(stack, rule_set_id, pam)
        assert reset.status_code == 200, reset.text
        stamps = enforce_at(stack, rule_set_id)
        # v1 is enforced immediately; v2 and v3 start a new grace period.
        now = stack.validation_sql("SELECT now() AS now")[0]["now"]
        assert stamps[1] is not None and stamps[1] <= now
        assert stamps[2] is not None and stamps[2] > now
        assert stamps[3] is not None and stamps[3] > now
        row = stack.validation_sql(
            "SELECT cleared_enforced_version_no FROM rule_set WHERE id = %s", rule_set_id
        )[0]
        assert row["cleared_enforced_version_no"] is None
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [
            (1, "enforce"),
            (2, "warn"),
            (3, "warn"),
        ]
        set_events = events(stack, "ruleset.mandatory_set")
        assert len(set_events) == 2
        assert set_events[1]["after"]["restored_version_no"] == 1


async def test_clearing_when_nothing_was_enforced_makes_the_next_set_a_first_time_set(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 2)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        assert (await mandate(stack, rule_set_id, pam, False)).status_code == 200
        row = stack.validation_sql(
            "SELECT cleared_enforced_version_no FROM rule_set WHERE id = %s", rule_set_id
        )[0]
        assert row["cleared_enforced_version_no"] is None
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        stamps = enforce_at(stack, rule_set_id)
        assert stamps[1] is None and stamps[2] is not None
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [(2, "warn")]


async def test_an_unpublished_set_cannot_be_mandatory_and_enforce_now_needs_a_mandatory_set(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _, _ = await setup(stack)
        empty = (await stack.create_rule_set(chain.org, "Empty", token=pam)).json()["id"]
        refused = await mandate(stack, empty, pam)
        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:rule-set-not-published"
        assert events(stack, "ruleset.mandatory_set") == []

        rule_set_id = await new_set(stack, chain.org, pam, 1)
        not_mandatory = await enforce_now(stack, rule_set_id, 1, pam)
        assert not_mandatory.status_code == 409, not_mandatory.text
        assert not_mandatory.json()["type"] == "urn:dc:problem:rule-set-not-mandatory"
        assert (await enforce_now(stack, rule_set_id, 9, pam)).status_code == 404
        assert events(stack, "ruleset.enforced_early") == []
        bad = await stack.client.put(
            f"{BASE}/rule-sets/{rule_set_id}/mandatory",
            json={"mandatory": "yes", "extra": 1},
            headers=auth(pam),
        )
        assert bad.status_code == 422


async def test_only_a_manager_at_the_rule_set_folder_or_above_can_change_enforcement(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200

        low, low_id = stack.token("lowpm")
        stack.grant_sql(low_id, "policy_manager", chain.team)
        for token in (low, ted):
            for response in (
                await mandate(stack, rule_set_id, token, False),
                await mandate(stack, rule_set_id, token, True),
                await enforce_now(stack, rule_set_id, 1, token),
            ):
                assert response.status_code in (403, 404), response.text
        sibling = await stack.chain()
        outsider, outsider_id = stack.token("other")
        stack.grant_sql(outsider_id, "policy_manager", sibling.org)
        assert (await mandate(stack, rule_set_id, outsider, False)).status_code == 404

        # Nothing changed: still mandatory with its original date, no clear event.
        entry = await view(stack, chain.team, ted, rule_set_id)
        assert entry["mandatory"] is True and modes(entry) == [(1, "warn")]
        assert events(stack, "ruleset.mandatory_cleared") == []
        assert events(stack, "ruleset.enforced_early") == []


async def test_a_block_inheritance_flag_does_not_stop_a_mandatory_set(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        blocked = await stack.identity.put(
            f"/api/v1/identity/folders/{chain.team}/block-inheritance",
            json={"enabled": True},
            headers=auth(stack.admin_token),
        )
        assert blocked.status_code == 200, blocked.text
        # The team editor keeps its own grant at T and still sees the org set as mandatory.
        entry = await view(stack, chain.team, ted, rule_set_id)
        assert entry["mandatory"] is True and modes(entry) == [(1, "warn")]


async def test_a_set_held_at_the_folder_itself_applies_and_an_empty_chain_resolves_nothing(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        own = await new_set(stack, chain.team, pam, 1, name="Team naming")
        plain = await new_set(stack, chain.org, pam, 1, name="Plain")
        assert (await mandate(stack, own, pam)).status_code == 200
        team_entry = await view(stack, chain.team, ted, own)
        assert modes(team_entry) == [(1, "warn")]
        # The plain set is available but not mandatory; nothing at the project mandates anything.
        assert (await view(stack, chain.team, ted, plain))["resolved"] == []
        project = await stack.get(f"{BASE}/folders/{chain.project}/available-rule-sets", token=pam)
        assert all(not item["mandatory"] for item in project.json()["items"])


async def test_zero_warn_days_enforces_a_newly_mandatory_version_at_once(make_stack: Any) -> None:
    async with make_stack(mandatory_warn_days=0) as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [(1, "enforce")]
        await publish_next(stack, rule_set_id, pam, 2)
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [(2, "enforce")]


async def test_an_archived_folder_refuses_mandate_changes_and_still_reads(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        await stack.archive(chain.org)
        response = await mandate(stack, rule_set_id, pam, False)
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "urn:dc:problem:folder-archived"
        early = await enforce_now(stack, rule_set_id, 1, pam)
        assert early.status_code == 409 and early.json()["type"].endswith("folder-archived")
        assert (await view(stack, chain.team, ted, rule_set_id))["mandatory"] is True


async def test_a_manager_moves_a_warning_version_later_and_to_the_past_and_it_is_audited(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        seeded = enforce_at(stack, rule_set_id)[1]
        later = datetime.now(UTC) + timedelta(days=60)

        moved = await reschedule(stack, rule_set_id, 1, pam, later)

        assert moved.status_code == 200, moved.text
        assert datetime.fromisoformat(moved.json()["enforce_at"]) == later
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [(1, "warn")]
        (event,) = events(stack, "ruleset.enforcement_rescheduled")
        assert event["before"]["enforce_at"] == seeded.isoformat()  # type: ignore[union-attr]
        assert event["after"]["previous_mode"] == "warn"
        assert event["after"]["mode"] == "warn"
        assert event["after"]["paused"] is False

        repeat = await reschedule(stack, rule_set_id, 1, pam, later)
        assert repeat.status_code == 200
        assert len(events(stack, "ruleset.enforcement_rescheduled")) == 1

        past = await reschedule(stack, rule_set_id, 1, pam, "2000-01-01T00:00:00Z")
        assert past.status_code == 200, past.text
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [(1, "enforce")]
        stored = enforce_at(stack, rule_set_id)[1]
        assert stored is not None and datetime.now(UTC) - stored < timedelta(minutes=1)
        assert len(events(stack, "ruleset.enforcement_rescheduled")) == 2
        assert events(stack, "ruleset.enforcement_rescheduled")[1]["after"]["mode"] == "enforce"

        again = await reschedule(stack, rule_set_id, 1, pam, "2001-01-01T00:00:00Z")
        assert again.status_code == 200
        assert len(events(stack, "ruleset.enforcement_rescheduled")) == 2

        postponed = await reschedule(stack, rule_set_id, 1, pam, later)
        assert postponed.status_code == 200
        assert modes(await view(stack, chain.team, ted, rule_set_id)) == [(1, "warn")]
        last = events(stack, "ruleset.enforcement_rescheduled")[2]["after"]
        assert (last["previous_mode"], last["mode"]) == ("enforce", "warn")


async def test_reschedule_refusals_and_enforce_now_stays_unchanged(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        when = datetime.now(UTC) + timedelta(days=5)

        not_mandatory = await reschedule(stack, rule_set_id, 1, pam, when)
        assert not_mandatory.status_code == 409, not_mandatory.text
        assert not_mandatory.json()["type"] == "urn:dc:problem:rule-set-not-mandatory"
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        assert (await reschedule(stack, rule_set_id, 9, pam, when)).status_code == 404
        naive = await reschedule(stack, rule_set_id, 1, pam, "2030-01-01T00:00:00")
        assert naive.status_code == 422
        extra = await stack.client.put(
            f"{BASE}/rule-sets/{rule_set_id}/versions/1/enforce-at",
            json={"enforce_at": when.isoformat(), "x": 1},
            headers=auth(pam),
        )
        assert extra.status_code == 422
        anonymous = await stack.client.put(
            f"{BASE}/rule-sets/{rule_set_id}/versions/1/enforce-at",
            json={"enforce_at": when.isoformat()},
        )
        assert anonymous.status_code == 401
        low, low_id = stack.token("lowpm")
        stack.grant_sql(low_id, "policy_manager", chain.team)
        for token in (low, ted):
            response = await reschedule(stack, rule_set_id, 1, token, when)
            assert response.status_code in (403, 404), response.text
        assert events(stack, "ruleset.enforcement_rescheduled") == []

        early = await enforce_now(stack, rule_set_id, 1, pam)
        assert early.status_code == 200, early.text
        assert len(events(stack, "ruleset.enforced_early")) == 1
        assert events(stack, "ruleset.enforcement_rescheduled") == []


async def test_a_platform_set_id_is_a_404_and_an_archived_folder_refuses_reschedule(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, pam, _, _ = await setup(stack)
        when = datetime.now(UTC) + timedelta(days=5)
        platform = await reschedule(stack, "05d2384d-a57d-4922-a576-921b2f7113bd", 1, pam, when)
        assert platform.status_code == 404, platform.text
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        await stack.archive(chain.org)

        response = await reschedule(stack, rule_set_id, 1, pam, when)

        assert response.status_code == 409, response.text
        assert response.json()["type"] == "urn:dc:problem:folder-archived"
        assert events(stack, "ruleset.enforcement_rescheduled") == []
