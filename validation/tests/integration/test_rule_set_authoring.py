"""Rule-set authoring through HTTP, identity's PDP and the sandbox (plan 05-07, RULE-01..04).

Identity and validation run in one process (``make_stack``); every decision is a real PDP call and
every check runs in the real sandbox.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"
BASE = "/api/v1/validation"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def rule(
    *,
    key: str = "unique-property-names",
    condition: str = UNIQUE_NAMES,
    passing: dict[str, Any] | None = None,
    failing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "target": "$.schema[*]",
        "condition": condition,
        "severity": "error",
        "message": "Property names must be unique within a table.",
        "fixtures": {
            "pass": [{"document": passing or table("id", "name")}],
            "fail": [{"document": failing or table("id", "id")}],
        },
    }


def draft_yaml(*rules: dict[str, Any], description: str | None = None) -> str:
    # JSON is a YAML subset: the body is YAML on the wire without a YAML writer in the tests.
    body: dict[str, Any] = {"rules": list(rules)}
    if description is not None:
        body["description"] = description
    return json.dumps(body, indent=1)


async def new_team_with_pm(stack: Any) -> tuple[Any, str, str, str]:
    """ROOT -> org A -> project P -> team T with fake:pam granted policy_manager at org A."""
    chain = await stack.chain()
    token, principal = stack.token("pam")
    stack.grant_sql(principal, "policy_manager", chain.org)
    return chain, token, principal, chain.team


async def test_a_policy_manager_creates_edits_and_publishes_a_rule_set_over_http(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _, team = await new_team_with_pm(stack)

        created = await stack.create_rule_set(team, "Naming", token=pam)
        assert created.status_code == 201, created.text
        body = created.json()
        rule_set_id = body["id"]
        assert created.headers["Location"] == f"{BASE}/rule-sets/{rule_set_id}"
        assert created.headers["ETag"] == '"r0"'
        assert body["folder_id"] == team
        assert body["folder_kind"] == "team"
        assert body["publish_counter"] == 0
        stored = stack.validation_sql(
            "SELECT ancestor_ids::text[] AS ids FROM rule_set WHERE id = %s", rule_set_id
        )[0]["ids"]
        assert stored == [stack.root_id, chain.org, chain.project, team]

        saved = await stack.put_draft(rule_set_id, draft_yaml(rule()), '"r0"', token=pam)
        assert saved.status_code == 200, saved.text
        assert saved.headers["ETag"] == '"r1"'
        assert saved.json()["check"]["ok"] is True
        assert [r["key"] for r in saved.json()["rules"]] == ["unique-property-names"]

        published = await stack.publish(rule_set_id, '"r1"', token=pam)
        assert published.status_code == 201, published.text
        assert published.json()["version_no"] == 1
        assert published.json()["dsl_version"] == "cel-2"
        assert published.headers["Location"] == f"{BASE}/rule-sets/{rule_set_id}/versions/1"

        version = await stack.get(f"{BASE}/rule-sets/{rule_set_id}/versions/1", token=pam)
        assert version.status_code == 200, version.text
        rules = version.json()["rules"]
        assert rules[0]["key"] == "unique-property-names"
        assert rules[0]["fixtures"]["pass"][0]["document"] == table("id", "name")
        assert rules[0]["fixtures"]["fail"][0]["document"] == table("id", "id")

        current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=pam)
        assert current.status_code == 200
        assert current.headers["ETag"] == '"r1"'
        assert current.json()["rules"][0]["key"] == "unique-property-names"
        assert stack.outbox_actions() == [
            "ruleset.created",
            "ruleset.draft_updated",
            "ruleset.version_published",
        ]


async def test_a_team_editor_a_foreign_policy_manager_and_no_token_are_refused(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _, team = await new_team_with_pm(stack)
        created = await stack.create_rule_set(team, "Naming", token=pam)
        rule_set_id = created.json()["id"]

        editor, editor_id = stack.token("ed")
        stack.grant_sql(editor_id, "team_editor", team)
        denied_create = await stack.create_rule_set(team, "Other", token=editor)
        assert denied_create.status_code in (403, 404)
        denied_put = await stack.put_draft(rule_set_id, draft_yaml(rule()), '"r0"', token=editor)
        assert denied_put.status_code in (403, 404)
        denied_publish = await stack.publish(rule_set_id, '"r0"', token=editor)
        assert denied_publish.status_code in (403, 404)
        assert stack.validation_sql("SELECT count(*) AS n FROM rule_set")[0]["n"] == 1

        other_org = await stack.chain()
        other, other_id = stack.token("pam-b")
        stack.grant_sql(other_id, "policy_manager", other_org.org)
        foreign = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=other)
        unknown = await stack.get(f"{BASE}/rule-sets/{uuid.uuid4()}", token=other)
        assert foreign.status_code == 404
        assert unknown.status_code == 404
        assert foreign.json()["type"] == unknown.json()["type"]
        foreign_create = await stack.create_rule_set(team, "Sneaky", token=other)
        assert foreign_create.status_code in (403, 404)

        anonymous = await stack.client.get(f"{BASE}/rule-sets/{rule_set_id}")
        assert anonymous.status_code == 401


async def draft_in_team(stack: Any, *rules: dict[str, Any]) -> tuple[str, str, str]:
    """A team rule set holding ``rules`` as its draft; returns (token, id, ETag of the draft)."""
    _, pam, _, team = await new_team_with_pm(stack)
    created = await stack.create_rule_set(team, "Naming", token=pam)
    rule_set_id = created.json()["id"]
    saved = await stack.put_draft(rule_set_id, draft_yaml(*rules), '"r0"', token=pam)
    assert saved.status_code == 200, saved.text
    return pam, rule_set_id, saved.headers["ETag"]


async def test_a_yaml_draft_with_the_pass_key_is_saved_and_published(make_stack: Any) -> None:
    yaml_body = f"""
description: naming rules
rules:
  - key: unique-property-names
    target: "$.schema[*]"
    condition: "{UNIQUE_NAMES}"
    severity: error
    message: Property names must be unique within a table.
    fixtures:
      pass:
        - document:
            schema:
              - name: orders
                properties: [{{name: id}}, {{name: name}}]
      fail:
        - document:
            schema:
              - name: orders
                properties: [{{name: id}}, {{name: id}}]
"""
    async with make_stack() as stack:
        _, pam, _, team = await new_team_with_pm(stack)
        rule_set_id = (await stack.create_rule_set(team, "Naming", token=pam)).json()["id"]
        saved = await stack.put_draft(rule_set_id, yaml_body, '"r0"', token=pam)
        assert saved.status_code == 200, saved.text
        assert saved.json()["check"]["ok"] is True
        assert saved.json()["rule_set"]["description"] == "naming rules"
        published = await stack.publish(rule_set_id, saved.headers["ETag"], token=pam)
        assert published.status_code == 201, published.text


async def test_a_rule_whose_fail_fixture_passes_is_refused_with_the_per_rule_report(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        pam, rule_set_id, etag = await draft_in_team(
            stack,
            rule(failing=table("id", "name")),  # the "fail" fixture is actually fine
        )
        refused = await stack.publish(rule_set_id, etag, token=pam)
        assert refused.status_code == 422, refused.text
        body = refused.json()
        assert body["type"] == "urn:dc:problem:ruleset-fixtures-failed"
        problem = body["check"]["rules"][0]["problems"][0]
        assert problem["code"] == "fixture_misbehaved"
        assert problem["fixture_kind"] == "fail"
        assert problem["fixture_index"] == 0
        assert stack.validation_sql("SELECT count(*) AS n FROM rule_set_version")[0]["n"] == 0


async def test_a_cel_syntax_error_saves_with_an_unfavourable_report_and_publish_is_rule_invalid(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        _, pam, _, team = await new_team_with_pm(stack)
        rule_set_id = (await stack.create_rule_set(team, "Naming", token=pam)).json()["id"]
        saved = await stack.put_draft(
            rule_set_id, draft_yaml(rule(condition="this.properties.all(p, ")), '"r0"', token=pam
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["check"]["ok"] is False
        refused = await stack.publish(rule_set_id, saved.headers["ETag"], token=pam)
        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:rule-invalid"
        problem = refused.json()["check"]["rules"][0]["problems"][0]
        assert problem["code"] == "compile_error"
        assert problem["detail"]


async def test_the_draft_check_route_reports_without_writing_and_is_closed_to_a_team_reader(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        pam, rule_set_id, etag = await draft_in_team(stack, rule(failing=table("id", "name")))
        events_before = stack.outbox_actions()
        report = await stack.check(rule_set_id, token=pam)
        assert report.status_code == 200, report.text
        check = report.json()["check"]
        assert check["ok"] is False
        assert check["rules"][0]["problems"][0]["code"] == "fixture_misbehaved"
        assert report.headers["ETag"] == etag
        assert stack.outbox_actions() == events_before
        assert stack.validation_sql("SELECT draft_revision FROM rule_set")[0]["draft_revision"] == 1

        team = stack.validation_sql("SELECT folder_id FROM rule_set")[0]["folder_id"]
        reader, reader_id = stack.token("rd")
        stack.grant_sql(reader_id, "team_reader", str(team))
        denied = await stack.check(rule_set_id, token=reader)
        assert denied.status_code in (403, 404)


async def test_if_match_is_required_and_a_stale_one_is_412_and_an_unchanged_republish_is_200(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        pam, rule_set_id, etag = await draft_in_team(stack, rule())
        assert (await stack.publish(rule_set_id, None, token=pam)).status_code == 428
        assert (await stack.publish(rule_set_id, '"r0"', token=pam)).status_code == 412
        stale_put = await stack.put_draft(rule_set_id, draft_yaml(rule()), '"r0"', token=pam)
        assert stale_put.status_code == 412
        missing_put = await stack.put_draft(rule_set_id, draft_yaml(rule()), None, token=pam)
        assert missing_put.status_code == 428

        first = await stack.publish(rule_set_id, etag, token=pam)
        again = await stack.publish(rule_set_id, etag, token=pam)
        assert first.status_code == 201
        assert again.status_code == 200
        assert again.json()["version_no"] == first.json()["version_no"] == 1
        assert stack.outbox_actions().count("ruleset.version_published") == 1


async def test_a_saturated_sandbox_is_503_with_retry_after_but_a_draft_save_still_lands(
    make_stack: Any,
) -> None:
    async with make_stack(max_concurrent_runs=1) as stack:
        pam, rule_set_id, etag = await draft_in_team(stack, rule())
        async with stack.app.state.sandbox.slot():
            refused = await stack.publish(rule_set_id, etag, token=pam)
            assert refused.status_code == 503, refused.text
            assert refused.headers["Retry-After"] == "1"
            assert refused.json()["type"] == "urn:dc:problem:validation-unavailable"

            checked = await stack.check(rule_set_id, token=pam)
            assert checked.status_code == 503
            assert checked.headers["Retry-After"] == "1"

            saved = await stack.put_draft(
                rule_set_id, draft_yaml(rule(key="another-rule")), etag, token=pam
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["check"] == {"status": "unavailable"}
            assert saved.headers["ETag"] == '"r2"'
        assert stack.validation_sql("SELECT count(*) AS n FROM rule_set_version")[0]["n"] == 0


async def test_an_archived_folder_refuses_writes_with_409_and_still_reads(make_stack: Any) -> None:
    async with make_stack() as stack:
        pam, rule_set_id, etag = await draft_in_team(stack, rule())
        team = str(stack.validation_sql("SELECT folder_id FROM rule_set")[0]["folder_id"])
        await stack.archive(team)
        put = await stack.put_draft(rule_set_id, draft_yaml(rule()), etag, token=pam)
        assert put.status_code == 409, put.text
        assert put.json()["type"] == "urn:dc:problem:folder-archived"
        publish = await stack.publish(rule_set_id, etag, token=pam)
        assert publish.status_code == 409
        assert publish.json()["type"] == "urn:dc:problem:folder-archived"
        create = await stack.create_rule_set(team, "Second", token=pam)
        assert create.status_code == 409
        assert (await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=pam)).status_code == 200


async def test_a_draft_above_max_draft_bytes_is_413_and_a_bad_body_is_422_or_415(
    make_stack: Any,
) -> None:
    async with make_stack(max_draft_bytes=2000) as stack:
        pam, rule_set_id, etag = await draft_in_team(stack, rule())
        huge = "description: " + "x" * 3000 + "\nrules: []\n"
        too_big = await stack.put_draft(rule_set_id, huge, etag, token=pam)
        assert too_big.status_code == 413
        assert too_big.json()["type"] == "urn:dc:problem:payload-too-large"

        wrong_type = await stack.put_draft(
            rule_set_id, "x", etag, token=pam, content_type="text/csv"
        )
        assert wrong_type.status_code == 415
        not_a_mapping = await stack.put_draft(rule_set_id, "- a\n- b\n", etag, token=pam)
        assert not_a_mapping.status_code == 422
        assert not_a_mapping.json()["type"] == "urn:dc:problem:malformed-document"
        extra = await stack.put_draft(rule_set_id, '{"rules": [], "nope": 1}', etag, token=pam)
        assert extra.status_code == 422
        assert extra.json()["type"] == "urn:dc:problem:validation-error"
        assert extra.json()["errors"][0]["pointer"] == "/nope"
        assert stack.validation_sql("SELECT draft_revision FROM rule_set")[0]["draft_revision"] == 1


async def test_an_identical_save_releases_the_rule_set_lock_before_the_advisory_check(
    make_stack: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-2: a no-op draft save holds no row lock and no open transaction during the check."""
    async with make_stack() as stack:
        pam, rule_set_id, etag = await draft_in_team(stack, rule())
        sandbox = stack.app.state.sandbox
        real_check = sandbox.check
        probes: list[tuple[int, bool]] = []

        async def probing_check(units: Any) -> Any:
            idle = stack.validation_sql(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%%'"
            )[0]["n"]
            try:  # NOWAIT raises at once when another transaction holds the row lock
                stack.validation_sql(
                    "SELECT id FROM rule_set WHERE id = %s FOR UPDATE NOWAIT", rule_set_id
                )
                free = True
            except Exception:
                free = False
            probes.append((int(idle), free))
            return await real_check(units)

        monkeypatch.setattr(sandbox, "check", probing_check)

        whole = await stack.put_draft(rule_set_id, draft_yaml(rule()), etag, token=pam)
        one = await stack.put_rule(
            rule_set_id, "unique-property-names", json.dumps(rule()), etag, token=pam
        )

        assert whole.status_code == 200, whole.text
        assert one.status_code == 200, one.text
        assert whole.headers["ETag"] == etag and one.headers["ETag"] == etag  # both were no-ops
        assert whole.json()["check"]["ok"] is True
        assert probes == [(0, True), (0, True)]


async def test_rule_set_names_follow_the_one_name_rule(make_stack: Any) -> None:
    """Quick 261120-lbz, D-01: 422 at the field with the code point, valid names are trimmed."""
    async with make_stack() as stack:
        _, pam, _, team = await new_team_with_pm(stack)

        for name, point in (("Naming\u202eevil", "U+202E"), ("Naming \U0001f600", "U+1F600")):
            refused = await stack.create_rule_set(team, name, token=pam)
            assert refused.status_code == 422, refused.text
            body = refused.json()
            assert body["type"] == "urn:dc:problem:validation-error"
            assert body["errors"][0]["pointer"] == "/body/name"
            assert point in body["errors"][0]["detail"]
            assert "\u202e" not in refused.text
            assert "\U0001f600" not in refused.text

        created = await stack.create_rule_set(team, "  Règles de nommage  ", token=pam)
        assert created.status_code == 201, created.text
        assert created.json()["name"] == "Règles de nommage"
