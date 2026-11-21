"""Per-rule create, edit and delete in the draft over HTTP (plan 05-08 task 1, RULE-01).

Both routes are whole-draft saves under the draft's ``If-Match``, so they share its ETag, row lock
and ``ruleset.draft_updated`` event with ``PUT .../draft``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"
HAS_ID = "this.properties.exists(p, p.name == 'id')"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def rule_body(
    *,
    key: str | None = "names-unique",
    condition: str = UNIQUE_NAMES,
    message: str = "Property names must be unique within a table.",
    passing: dict[str, Any] | None = None,
    failing: dict[str, Any] | None = None,
) -> str:
    body: dict[str, Any] = {
        "target": "$.schema[*]",
        "condition": condition,
        "severity": "error",
        "message": message,
        "fixtures": {
            "pass": [{"document": passing or table("id", "name")}],
            "fail": [{"document": failing or table("id", "id")}],
        },
    }
    if key is not None:
        body["key"] = key
    return json.dumps(body)


def pk_body() -> str:
    return rule_body(
        key=None,
        condition=HAS_ID,
        message="Every table needs an id property.",
        passing=table("id", "name"),
        failing=table("name"),
    )


async def new_rule_set(stack: Any) -> tuple[str, str, str]:
    """A team rule set with an empty draft; returns (policy manager token, rule set id, team)."""
    chain = await stack.chain()
    token, principal = stack.token("pam")
    stack.grant_sql(principal, "policy_manager", chain.org)
    created = await stack.create_rule_set(chain.team, "Naming", token=token)
    assert created.status_code == 201, created.text
    return token, created.json()["id"], chain.team


async def test_a_rule_is_added_replaced_deleted_and_the_result_is_published(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        pam, rule_set_id, _ = await new_rule_set(stack)

        first = await stack.put_rule(
            rule_set_id, "names-unique", rule_body(key=None), '"r0"', token=pam
        )
        assert first.status_code == 200, first.text
        assert first.headers["ETag"] == '"r1"'
        assert [r["key"] for r in first.json()["rules"]] == ["names-unique"]
        assert first.json()["check"]["ok"] is True

        changed = await stack.put_rule(
            rule_set_id, "names-unique", rule_body(message="Names must differ."), '"r1"', token=pam
        )
        assert changed.status_code == 200, changed.text
        assert changed.headers["ETag"] == '"r2"'
        assert [r["message"] for r in changed.json()["rules"]] == ["Names must differ."]

        second = await stack.put_rule(rule_set_id, "pk-valid", pk_body(), '"r2"', token=pam)
        assert second.status_code == 200, second.text
        assert second.headers["ETag"] == '"r3"'
        # A replaced rule keeps its place; a new one is appended.
        assert [r["key"] for r in second.json()["rules"]] == ["names-unique", "pk-valid"]

        removed = await stack.delete_rule(rule_set_id, "names-unique", '"r3"', token=pam)
        assert removed.status_code == 200, removed.text
        assert removed.headers["ETag"] == '"r4"'
        assert [r["key"] for r in removed.json()["rules"]] == ["pk-valid"]
        assert removed.json()["check"] is not None

        published = await stack.publish(rule_set_id, '"r4"', token=pam)
        assert published.status_code == 201, published.text
        assert published.json()["rule_keys"] == ["pk-valid"]
        version = await stack.get(f"{BASE}/rule-sets/{rule_set_id}/versions/1", token=pam)
        assert [r["key"] for r in version.json()["rules"]] == ["pk-valid"]
        assert stack.outbox_actions() == [
            "ruleset.created",
            "ruleset.draft_updated",
            "ruleset.draft_updated",
            "ruleset.draft_updated",
            "ruleset.draft_updated",
            "ruleset.version_published",
        ]


async def test_an_identical_rule_put_is_a_200_no_op_without_an_event(make_stack: Any) -> None:
    async with make_stack() as stack:
        pam, rule_set_id, _ = await new_rule_set(stack)
        first = await stack.put_rule(rule_set_id, "names-unique", rule_body(), '"r0"', token=pam)
        assert first.status_code == 200, first.text
        again = await stack.put_rule(rule_set_id, "names-unique", rule_body(), '"r1"', token=pam)
        assert again.status_code == 200, again.text
        assert again.headers["ETag"] == '"r1"'
        assert stack.outbox_actions().count("ruleset.draft_updated") == 1


async def test_a_body_key_that_differs_or_a_bad_path_key_is_422(make_stack: Any) -> None:
    async with make_stack() as stack:
        pam, rule_set_id, _ = await new_rule_set(stack)
        mismatch = await stack.put_rule(
            rule_set_id, "names-unique", rule_body(key="other-key"), '"r0"', token=pam
        )
        assert mismatch.status_code == 422, mismatch.text
        assert mismatch.json()["type"] == "urn:dc:problem:validation-error"
        upper = await stack.put_rule(
            rule_set_id, "Names-Unique", rule_body(key=None), '"r0"', token=pam
        )
        assert upper.status_code == 422, upper.text
        assert upper.json()["type"] == "urn:dc:problem:validation-error"
        upper_delete = await stack.delete_rule(rule_set_id, "Names_Unique", '"r0"', token=pam)
        assert upper_delete.status_code == 422, upper_delete.text
        extra = await stack.put_rule(
            rule_set_id,
            "names-unique",
            json.dumps({**json.loads(rule_body()), "bogus": 1}),
            '"r0"',
            token=pam,
        )
        assert extra.status_code == 422, extra.text
        assert "bogus" not in json.dumps(extra.json()["errors"][0]["detail"])
        wrong_type = await stack.put_rule(
            rule_set_id, "names-unique", rule_body(), '"r0"', token=pam, content_type="text/plain"
        )
        assert wrong_type.status_code == 415, wrong_type.text
        assert stack.outbox_actions() == ["ruleset.created"]


async def test_delete_of_an_unknown_key_is_404_and_without_if_match_is_428(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        pam, rule_set_id, _ = await new_rule_set(stack)
        await stack.put_rule(rule_set_id, "names-unique", rule_body(), '"r0"', token=pam)

        unknown = await stack.delete_rule(rule_set_id, "unknown", '"r1"', token=pam)
        assert unknown.status_code == 404, unknown.text
        missing = await stack.delete_rule(rule_set_id, "names-unique", None, token=pam)
        assert missing.status_code == 428, missing.text
        no_put = await stack.put_rule(rule_set_id, "pk-valid", pk_body(), None, token=pam)
        assert no_put.status_code == 428, no_put.text
        stale = await stack.put_rule(rule_set_id, "pk-valid", pk_body(), '"r0"', token=pam)
        assert stale.status_code == 412, stale.text
        stale_delete = await stack.delete_rule(rule_set_id, "names-unique", '"r0"', token=pam)
        assert stale_delete.status_code == 412, stale_delete.text
        current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=pam)
        assert [r["key"] for r in current.json()["rules"]] == ["names-unique"]
        assert current.headers["ETag"] == '"r1"'


async def test_editor_foreign_manager_and_archived_folder_cannot_change_a_rule(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        pam, rule_set_id, team = await new_rule_set(stack)
        await stack.put_rule(rule_set_id, "names-unique", rule_body(), '"r0"', token=pam)

        editor, editor_id = stack.token("ed")
        stack.grant_sql(editor_id, "team_editor", team)
        for response in (
            await stack.put_rule(rule_set_id, "pk-valid", pk_body(), '"r1"', token=editor),
            await stack.delete_rule(rule_set_id, "names-unique", '"r1"', token=editor),
        ):
            assert response.status_code in (403, 404), response.text

        other = await stack.chain()
        outsider, outsider_id = stack.token("pam-b")
        stack.grant_sql(outsider_id, "policy_manager", other.org)
        foreign = await stack.put_rule(rule_set_id, "pk-valid", pk_body(), '"r1"', token=outsider)
        unknown = await stack.put_rule(
            str(uuid.uuid4()), "pk-valid", pk_body(), '"r1"', token=outsider
        )
        assert foreign.status_code == 404
        assert unknown.status_code == 404
        assert foreign.json()["type"] == unknown.json()["type"]
        anonymous = await stack.client.put(
            f"{BASE}/rule-sets/{rule_set_id}/draft/rules/pk-valid", content=pk_body()
        )
        assert anonymous.status_code == 401

        await stack.archive(team)
        put = await stack.put_rule(rule_set_id, "pk-valid", pk_body(), '"r1"', token=pam)
        gone = await stack.delete_rule(rule_set_id, "names-unique", '"r1"', token=pam)
        assert put.status_code == 409 and put.json()["type"] == "urn:dc:problem:folder-archived"
        assert gone.status_code == 409
        read = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=pam)
        assert read.status_code == 200
        assert [r["key"] for r in read.json()["rules"]] == ["names-unique"]
        assert stack.outbox_actions().count("ruleset.draft_updated") == 1


async def test_an_oversized_rule_body_is_413_and_nothing_is_written(make_stack: Any) -> None:
    async with make_stack(max_fixture_bytes=100, max_fixtures_per_kind=1) as stack:
        pam, rule_set_id, _ = await new_rule_set(stack)
        huge = rule_body(message="x" * 70_000)
        response = await stack.put_rule(rule_set_id, "names-unique", huge, '"r0"', token=pam)
        assert response.status_code == 413, response.text
        assert stack.outbox_actions() == ["ruleset.created"]
