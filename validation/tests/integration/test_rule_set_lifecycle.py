"""Rule-set lifecycle over HTTP: listing, versions, deprecate and delete (plan 05-08 task 2).

RULE-03 (who holds what, names per folder) and RULE-04 (immutable numbered versions, deprecation
keeps a version readable, a published set is never deleted).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def rule_body(message: str = "Property names must be unique within a table.") -> str:
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


async def manager(stack: Any) -> tuple[Any, str]:
    """ROOT -> org A -> project P -> team T with fake:pam granted policy_manager at org A."""
    chain = await stack.chain()
    token, principal = stack.token("pam")
    stack.grant_sql(principal, "policy_manager", chain.org)
    return chain, token


async def publish_versions(stack: Any, rule_set_id: str, token: str, count: int) -> None:
    """Publish ``count`` distinct versions of the one-rule set by editing its message."""
    etag = '"r0"'
    current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=token)
    etag = current.headers["ETag"]
    for number in range(1, count + 1):
        put = await stack.put_rule(
            rule_set_id, "names-unique", rule_body(f"Message {number}"), etag, token=token
        )
        assert put.status_code == 200, put.text
        published = await stack.publish(rule_set_id, put.headers["ETag"], token=token)
        assert published.status_code == 201, published.text
        etag = put.headers["ETag"]


async def test_authors_list_a_folders_rule_sets_by_lower_name_then_id(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam = await manager(stack)
        empty = await stack.get(f"{BASE}/folders/{chain.team}/rule-sets", token=pam)
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"items": []}

        for name in ("Zeta", "alpha", "Beta"):
            assert (await stack.create_rule_set(chain.team, name, token=pam)).status_code == 201
        listed = await stack.get(f"{BASE}/folders/{chain.team}/rule-sets", token=pam)
        assert [i["name"] for i in listed.json()["items"]] == ["alpha", "Beta", "Zeta"]
        first = listed.json()
        again = await stack.get(f"{BASE}/folders/{chain.team}/rule-sets", token=pam)
        assert again.json() == first
        item = first["items"][0]
        assert item["draft_revision"] == 0 and item["publish_counter"] == 0
        # Only the sets held at that folder, not those of its ancestors.
        parent = await stack.get(f"{BASE}/folders/{chain.project}/rule-sets", token=pam)
        assert parent.json() == {"items": []}


async def test_names_collide_case_insensitively_per_folder_but_not_across_folders(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam = await manager(stack)
        assert (await stack.create_rule_set(chain.team, "Naming", token=pam)).status_code == 201
        clash = await stack.create_rule_set(chain.team, "naming", token=pam)
        assert clash.status_code == 409, clash.text
        assert clash.json()["type"] == "urn:dc:problem:rule-set-name-taken"
        assert (await stack.create_rule_set(chain.project, "Naming", token=pam)).status_code == 201
        listed = await stack.get(f"{BASE}/folders/{chain.team}/rule-sets", token=pam)
        assert [i["name"] for i in listed.json()["items"]] == ["Naming"]


async def test_versions_are_listed_newest_first_and_an_unpublished_set_has_none(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam = await manager(stack)
        fresh = (await stack.create_rule_set(chain.team, "Fresh", token=pam)).json()["id"]
        none = await stack.get(f"{BASE}/rule-sets/{fresh}/versions", token=pam)
        assert none.status_code == 200, none.text
        assert none.json() == {"items": []}
        assert (
            await stack.get(f"{BASE}/rule-sets/{fresh}/versions/1", token=pam)
        ).status_code == 404

        busy = (await stack.create_rule_set(chain.team, "Busy", token=pam)).json()["id"]
        await publish_versions(stack, busy, pam, 3)
        listed = await stack.get(f"{BASE}/rule-sets/{busy}/versions", token=pam)
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert [i["version_no"] for i in items] == [3, 2, 1]
        assert all(i["deprecated_at"] is None for i in items)
        assert set(items[0]) == {
            "version_no",
            "content_sha256",
            "rule_keys",
            "deprecated_at",
            "enforce_at",
            "published_at",
            "published_by",
        }
        assert items[0]["rule_keys"] == ["names-unique"]


async def test_a_version_is_deprecated_once_and_stays_readable(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam = await manager(stack)
        rule_set_id = (await stack.create_rule_set(chain.team, "Busy", token=pam)).json()["id"]
        await publish_versions(stack, rule_set_id, pam, 2)
        before = stack.outbox_actions().count("ruleset.version_deprecated")
        assert before == 0

        url = f"{BASE}/rule-sets/{rule_set_id}/versions/2/deprecate"
        first = await stack.client.post(url, headers={"Authorization": f"Bearer {pam}"})
        assert first.status_code == 200, first.text
        assert first.json()["deprecated_at"] is not None
        assert first.json()["deprecated_by"] == "fake:pam"
        stamp = first.json()["deprecated_at"]
        again = await stack.client.post(url, headers={"Authorization": f"Bearer {pam}"})
        assert again.status_code == 200, again.text
        assert again.json()["deprecated_at"] == stamp
        assert stack.outbox_actions().count("ruleset.version_deprecated") == 1

        read = await stack.get(f"{BASE}/rule-sets/{rule_set_id}/versions/2", token=pam)
        assert read.status_code == 200, read.text
        assert read.json()["rules"][0]["message"] == "Message 2"
        missing = await stack.client.post(
            f"{BASE}/rule-sets/{rule_set_id}/versions/9/deprecate",
            headers={"Authorization": f"Bearer {pam}"},
        )
        assert missing.status_code == 404
        event = stack.validation_sql(
            "SELECT before, after FROM outbox WHERE action = 'ruleset.version_deprecated'"
        )[0]
        assert event["before"]["deprecated_at"] is None
        assert event["after"]["deprecated_by"] == "fake:pam"
        assert "condition" not in json.dumps(event) and "Message" not in json.dumps(event)


async def test_an_unpublished_set_is_deleted_and_a_published_one_is_not(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam = await manager(stack)
        draft_only = (await stack.create_rule_set(chain.team, "Draft", token=pam)).json()["id"]
        gone = await stack.client.delete(
            f"{BASE}/rule-sets/{draft_only}", headers={"Authorization": f"Bearer {pam}"}
        )
        assert gone.status_code == 204, gone.text
        assert gone.content == b""
        assert (await stack.get(f"{BASE}/rule-sets/{draft_only}", token=pam)).status_code == 404
        assert stack.outbox_actions().count("ruleset.deleted") == 1
        # The name is free again.
        assert (await stack.create_rule_set(chain.team, "Draft", token=pam)).status_code == 201

        published = (await stack.create_rule_set(chain.team, "Live", token=pam)).json()["id"]
        await publish_versions(stack, published, pam, 1)
        refused = await stack.client.delete(
            f"{BASE}/rule-sets/{published}", headers={"Authorization": f"Bearer {pam}"}
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["type"] == "urn:dc:problem:rule-set-published"
        assert (await stack.get(f"{BASE}/rule-sets/{published}", token=pam)).status_code == 200
        assert stack.outbox_actions().count("ruleset.deleted") == 1


async def test_an_archived_folder_refuses_every_write_and_still_reads(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam = await manager(stack)
        rule_set_id = (await stack.create_rule_set(chain.team, "Busy", token=pam)).json()["id"]
        await publish_versions(stack, rule_set_id, pam, 1)
        spare = (await stack.create_rule_set(chain.team, "Spare", token=pam)).json()["id"]
        current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=pam)
        etag = current.headers["ETag"]
        await stack.archive(chain.team)

        auth = {"Authorization": f"Bearer {pam}"}
        put = await stack.put_draft(rule_set_id, json.dumps({"rules": []}), etag, token=pam)
        publish = await stack.publish(rule_set_id, etag, token=pam)
        deprecate = await stack.client.post(
            f"{BASE}/rule-sets/{rule_set_id}/versions/1/deprecate", headers=auth
        )
        delete = await stack.client.delete(f"{BASE}/rule-sets/{spare}", headers=auth)
        for response in (put, publish, deprecate, delete):
            assert response.status_code == 409, response.text
            assert response.json()["type"] == "urn:dc:problem:folder-archived"
        for path in (
            f"{BASE}/folders/{chain.team}/rule-sets",
            f"{BASE}/rule-sets/{rule_set_id}",
            f"{BASE}/rule-sets/{rule_set_id}/versions",
            f"{BASE}/rule-sets/{rule_set_id}/versions/1",
        ):
            assert (await stack.get(path, token=pam)).status_code == 200, path


async def test_a_team_editor_and_a_team_reader_reach_no_author_route(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam = await manager(stack)
        rule_set_id = (await stack.create_rule_set(chain.team, "Busy", token=pam)).json()["id"]
        await publish_versions(stack, rule_set_id, pam, 1)
        editor, editor_id = stack.token("ed")
        stack.grant_sql(editor_id, "team_editor", chain.team)
        reader, reader_id = stack.token("rd")
        stack.grant_sql(reader_id, "team_reader", chain.team)
        for token in (editor, reader):
            auth = {"Authorization": f"Bearer {token}"}
            for response in (
                await stack.get(f"{BASE}/folders/{chain.team}/rule-sets", token=token),
                await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=token),
                await stack.get(f"{BASE}/rule-sets/{rule_set_id}/versions", token=token),
                await stack.client.post(
                    f"{BASE}/rule-sets/{rule_set_id}/versions/1/deprecate", headers=auth
                ),
                await stack.client.delete(f"{BASE}/rule-sets/{rule_set_id}", headers=auth),
            ):
                assert response.status_code in (403, 404), response.text
        unknown = await stack.get(f"{BASE}/rule-sets/{uuid.uuid4()}/versions", token=reader)
        assert unknown.status_code == 404
        assert stack.outbox_actions().count("ruleset.version_deprecated") == 0
        assert stack.outbox_actions().count("ruleset.deleted") == 0
