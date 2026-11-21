"""Who reads which published rules (plan 05-08 task 3, D-115, research Pitfall 4).

Drafts are for authors; published versions are readable by anyone who holds
``ruleset:read-published`` at the rule set's folder or any folder below it, through the caller's
own folder. Sibling branches, drafts and sets without a published version are all the same 404.
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


async def make_set(stack: Any, token: str, folder: str, name: str, *messages: str) -> str:
    """A rule set at ``folder`` with one published version per message; returns its id."""
    rule_set_id = (await stack.create_rule_set(folder, name, token=token)).json()["id"]
    etag = '"r0"'
    for message in messages:
        put = await stack.put_rule(
            rule_set_id, "names-unique", rule_body(message), etag, token=token
        )
        assert put.status_code == 200, put.text
        etag = put.headers["ETag"]
        published = await stack.publish(rule_set_id, etag, token=token)
        assert published.status_code == 201, published.text
    return rule_set_id


async def setup(stack: Any) -> tuple[Any, str]:
    """ROOT -> org A -> project P -> team T with fake:pam as policy_manager at org A."""
    chain = await stack.chain()
    pam, pam_id = stack.token("pam")
    stack.grant_sql(pam_id, "policy_manager", chain.org)
    return chain, pam


def names(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


async def test_an_editor_below_the_rule_set_reads_the_published_rules_but_no_draft(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam = await setup(stack)
        rule_set_id = await make_set(stack, pam, chain.org, "Global naming", "First", "Second")
        draft = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=pam)
        edit = await stack.put_rule(
            rule_set_id,
            "names-unique",
            rule_body("UNPUBLISHED DRAFT TEXT"),
            draft.headers["ETag"],
            token=pam,
        )
        assert edit.status_code == 200
        editor, editor_id = stack.token("ed")
        stack.grant_sql(editor_id, "team_editor", chain.team)

        listed = await stack.get(f"{BASE}/folders/{chain.team}/available-rule-sets", token=editor)
        assert names(listed) == ["Global naming"]
        item = listed.json()["items"][0]
        assert item["id"] == rule_set_id
        assert item["scope"] == "folder"
        assert item["folder_id"] == chain.org
        assert item["folder_kind"] == "organization"
        assert item["latest_version"] == 2
        assert item["latest_active_version"] == 2
        assert [v["version_no"] for v in item["versions"]] == [2, 1]
        assert "UNPUBLISHED DRAFT TEXT" not in listed.text
        assert "draft" not in listed.text

        one = await stack.get(
            f"{BASE}/folders/{chain.team}/available-rule-sets/{rule_set_id}", token=editor
        )
        assert one.status_code == 200, one.text
        assert one.json()["id"] == rule_set_id
        assert "UNPUBLISHED DRAFT TEXT" not in one.text

        version = await stack.get(
            f"{BASE}/folders/{chain.team}/available-rule-sets/{rule_set_id}/versions/1",
            token=editor,
        )
        assert version.status_code == 200, version.text
        rule = version.json()["rules"][0]
        assert rule["message"] == "First"
        assert rule["fixtures"]["fail"][0]["document"] == table("id", "id")
        assert version.json()["version_no"] == 1
        # The author routes stay closed to the editor.
        assert (
            await stack.get(f"{BASE}/rule-sets/{rule_set_id}/versions/1", token=editor)
        ).status_code in (403, 404)


async def test_a_consumer_and_a_reader_see_the_list_and_an_outsider_does_not(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam = await setup(stack)
        await make_set(stack, pam, chain.org, "Global naming", "First")
        url = f"{BASE}/folders/{chain.team}/available-rule-sets"
        for sub, role in (("co", "consumer"), ("rd", "team_reader")):
            token, principal = stack.token(sub)
            stack.grant_sql(principal, role, chain.team)
            assert names(await stack.get(url, token=token)) == ["Global naming"], role
        outsider, _ = stack.token("nobody")
        denied = await stack.get(url, token=outsider)
        assert denied.status_code == 404, denied.text
        anonymous = await stack.client.get(url)
        assert anonymous.status_code == 401


async def test_sibling_branches_unpublished_and_unknown_sets_are_one_404(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam = await setup(stack)
        sibling = await stack.folder(chain.project, "team2")
        sibling_set = await make_set(stack, pam, sibling, "Sibling rules", "First")
        unpublished = (await stack.create_rule_set(chain.org, "Not yet", token=pam)).json()["id"]
        visible = await make_set(stack, pam, chain.org, "Visible", "First")
        editor, editor_id = stack.token("ed")
        stack.grant_sql(editor_id, "team_editor", chain.team)
        base = f"{BASE}/folders/{chain.team}/available-rule-sets"

        listed = await stack.get(base, token=editor)
        assert names(listed) == ["Visible"]
        bodies = []
        for rule_set_id in (sibling_set, unpublished, str(uuid.uuid4())):
            response = await stack.get(f"{base}/{rule_set_id}", token=editor)
            assert response.status_code == 404, response.text
            version = await stack.get(f"{base}/{rule_set_id}/versions/1", token=editor)
            assert version.status_code == 404, version.text
            bodies.append((response.json()["type"], response.json()["detail"]))
        assert len(set(bodies)) == 1
        missing_version = await stack.get(f"{base}/{visible}/versions/9", token=editor)
        assert missing_version.status_code == 404

        # A set held below the caller's folder is not available at the folder above it.
        at_project = await stack.get(
            f"{BASE}/folders/{chain.project}/available-rule-sets", token=pam
        )
        assert names(at_project) == ["Visible"]  # the unpublished "Not yet" is not listed
        await make_set(stack, pam, chain.team, "Team only", "First")
        assert names(
            await stack.get(f"{BASE}/folders/{chain.project}/available-rule-sets", token=pam)
        ) == ["Visible"]


async def test_the_list_is_ordered_by_holder_depth_then_name_and_is_stable(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam = await setup(stack)
        await make_set(stack, pam, chain.team, "Naming", "At the team")
        await make_set(stack, pam, chain.org, "Naming", "At the org")
        await make_set(stack, pam, chain.org, "alpha", "x")
        await make_set(stack, pam, chain.team, "Beta", "x")
        url = f"{BASE}/folders/{chain.team}/available-rule-sets"
        first = await stack.get(url, token=pam)
        items = first.json()["items"]
        assert [(i["folder_kind"], i["name"]) for i in items] == [
            ("organization", "alpha"),
            ("organization", "Naming"),
            ("team", "Beta"),
            ("team", "Naming"),
        ]
        assert (await stack.get(url, token=pam)).json() == first.json()


async def test_latest_active_version_follows_deprecation(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam = await setup(stack)
        rule_set_id = await make_set(stack, pam, chain.org, "Global naming", "First", "Second")
        url = f"{BASE}/folders/{chain.team}/available-rule-sets/{rule_set_id}"
        auth = {"Authorization": f"Bearer {pam}"}

        await stack.client.post(
            f"{BASE}/rule-sets/{rule_set_id}/versions/2/deprecate", headers=auth
        )
        one = (await stack.get(url, token=pam)).json()
        assert one["latest_version"] == 2
        assert one["latest_active_version"] == 1
        assert [v["deprecated_at"] is not None for v in one["versions"]] == [True, False]
        # A deprecated version stays readable.
        assert (await stack.get(f"{url}/versions/2", token=pam)).status_code == 200

        await stack.client.post(
            f"{BASE}/rule-sets/{rule_set_id}/versions/1/deprecate", headers=auth
        )
        none_left = (await stack.get(url, token=pam)).json()
        assert none_left["latest_version"] == 2
        assert none_left["latest_active_version"] is None


async def test_an_empty_folder_lists_nothing_and_platform_sets_come_first(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam = await setup(stack)
        url = f"{BASE}/folders/{chain.team}/available-rule-sets"
        assert (await stack.get(url, token=pam)).json() == {"items": []}

        stack.validation_sql(
            "INSERT INTO rule_set (scope, name, draft_content_sha256, publish_counter, "
            "created_by, updated_by) VALUES ('platform', 'Structural', 'x', 1, 'system:test', "
            "'system:test')"
        )
        platform_id = stack.validation_sql("SELECT id FROM rule_set WHERE scope = 'platform'")[0][
            "id"
        ]
        stack.validation_sql(
            "INSERT INTO rule_set_version (rule_set_id, version_no, dsl_version, rules, "
            "content_sha256, rule_keys, created_by, updated_by) VALUES (%s, 1, 'cel-1', '[]', "
            "'x', '{}', 'system:test', 'system:test')",
            platform_id,
        )
        await make_set(stack, pam, chain.org, "Aardvark", "First")
        listed = await stack.get(url, token=pam)
        assert names(listed) == ["Structural", "Aardvark"]
        platform = listed.json()["items"][0]
        assert platform["scope"] == "platform"
        assert platform["folder_id"] is None and platform["folder_kind"] is None
        one = await stack.get(f"{url}/{platform_id}", token=pam)
        assert one.status_code == 200, one.text


async def test_available_reads_keep_working_in_an_archived_folder(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam = await setup(stack)
        rule_set_id = await make_set(stack, pam, chain.org, "Global naming", "First")
        await stack.archive(chain.team)
        url = f"{BASE}/folders/{chain.team}/available-rule-sets"
        assert names(await stack.get(url, token=pam)) == ["Global naming"]
        assert (await stack.get(f"{url}/{rule_set_id}/versions/1", token=pam)).status_code == 200
