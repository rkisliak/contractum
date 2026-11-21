"""The D-141 hostile-rule exit gate through the validation HTTP surface (plan 05-14, RULE-09).

Plan 04 proved the engine-level cases. Here the same shapes go through the real routes of the real
app (identity's PDP, the sandbox, the database): hostile rules that slip past publish (inserted by
SQL as published versions) are bounded at evaluation and fail closed, an exhausted run budget never
skips a rule, and the service keeps answering while hostile runs hold the workers. Assertions are
on the hostile rules' own findings and counts, never on totals: the platform seed may or may not
be present.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import uuid
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
MAX_DOCUMENT_BYTES = 1_048_576


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def contract(**extra: Any) -> dict[str, Any]:
    """A conformant ODCS v3.2.0 document; ``extra`` keys are added at the top level."""
    document: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": "urn:datacontract:test:orders",
        "version": "1.0.0",
        "status": "draft",
        "schema": [{"name": "orders", "properties": [{"name": "id", "logicalType": "string"}]}],
    }
    document.update(extra)
    return document


def encode(document: dict[str, Any]) -> bytes:
    """Compact JSON bytes (the run parses JSON as YAML's subset by media type)."""
    return json.dumps(document, separators=(",", ":")).encode()


def rule_json(
    key: str,
    condition: str,
    *,
    target: str = "$",
    severity: str = "error",
    pass_fixtures: list[dict[str, Any]] | None = None,
    fail_fixtures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "target": target,
        "condition": condition,
        "severity": severity,
        "message": f"Rule {key} must hold.",
        "fixtures": {"pass": pass_fixtures or [], "fail": fail_fixtures or []},
    }


async def team_with_pm_and_editor(stack: Any) -> tuple[Any, str, str]:
    """Org with a Policy Manager at the org and a team editor at the team (and a second team)."""
    chain = await stack.chain()
    pam, pam_id = stack.token("pam")
    stack.grant_sql(pam_id, "policy_manager", chain.org)
    editor, editor_id = stack.token("ed")
    stack.grant_sql(editor_id, "team_editor", chain.org)
    return chain, pam, editor


async def insert_published_set(
    stack: Any,
    folder: str,
    token: str,
    rules: list[dict[str, Any]],
    *,
    name: str = "Hostile",
    enforced: bool = True,
) -> str:
    """A rule set whose version 1 holds ``rules`` as stored data, bypassing every publish check.

    The set is created through the API (so its ancestry comes from identity); the version row is
    inserted by SQL, which the immutability triggers allow, and the set is made mandatory and
    already enforced (or, with ``enforced=False``, in its warn period), so every run at or below
    ``folder`` evaluates it as blocking (or warn only).
    """
    created = await stack.create_rule_set(folder, name, token=token)
    assert created.status_code == 201, created.text
    rule_set_id: str = created.json()["id"]
    stack.validation_sql(
        "INSERT INTO rule_set_version (rule_set_id, version_no, dsl_version, rules,"
        " content_sha256, rule_keys, enforce_at, created_by, updated_by)"
        " VALUES (%s, 1, 'cel-1', %s::jsonb, %s, %s, now() + %s * interval '1 minute',"
        " 'system:test', 'system:test')",
        rule_set_id,
        json.dumps(rules),
        uuid.uuid4().hex + uuid.uuid4().hex,
        [r["key"] for r in rules],
        -1 if enforced else 14 * 24 * 60,
    )
    stack.validation_sql(
        "UPDATE rule_set SET publish_counter = 1, mandatory = true,"
        " mandatory_set_at = now(), mandatory_set_by = 'system:test' WHERE id = %s",
        rule_set_id,
    )
    return rule_set_id


def run_url(folder_id: str) -> str:
    return f"/internal/v1/validation/folders/{folder_id}/runs"


async def post_run(stack: Any, folder_id: str, token: str, document: bytes) -> Any:
    payload: dict[str, Any] = {
        "mode": "save",
        "media_type": "application/json",
        "document_b64": base64.b64encode(document).decode(),
        "attachments": [],
        "attachments_revision": None,
    }
    return await stack.client.post(run_url(folder_id), json=payload, headers=auth(token))


def findings_for(body: dict[str, Any], rule_set_id: str, rule_key: str) -> list[dict[str, Any]]:
    # A finding names its set through ``rule_set.id``.
    return [
        f
        for f in body["report"]["findings"]
        if f["rule_set"]["id"] == rule_set_id and f["rule_key"] == rule_key
    ]


def counts_of(body: dict[str, Any], rule_set_id: str) -> dict[str, Any]:
    (entry,) = [c for c in body["report"]["rule_sets"] if c["rule_set_id"] == rule_set_id]
    result: dict[str, Any] = entry
    return result


# -- tracer: hostile rules at evaluation are bounded, blocking, and do not starve others ---------


@pytest.mark.timing  # wall-clock bounds: the full gate runs it without coverage tracing
async def test_a_hostile_rule_that_slipped_past_publish_is_bounded_and_a_cheap_run_still_completes(
    make_stack: Any,
) -> None:
    async with make_stack(eval_workers=2, max_concurrent_runs=4) as stack:
        chain, pam, editor = await team_with_pm_and_editor(stack)
        other_team = await stack.folder(chain.project, "team2")
        rule_set_id = await insert_published_set(
            stack,
            chain.org,
            pam,
            [
                rule_json("items-all", "this.items.all(i, has(i.n))"),
                rule_json("many-matches", "true", target="$.items[*]"),
                rule_json("normal", "has(this.kind)"),
            ],
        )
        huge = encode(contract(items=[{"n": 1}] * 100_000))
        assert len(huge) < MAX_DOCUMENT_BYTES  # the evaluation bounds are tested, not the body cap
        budget = stack.app.state.settings.run_budget_s
        cheap_document = yaml.safe_dump(contract()).encode()

        async def hostile() -> tuple[Any, float]:
            started = time.monotonic()
            response = await post_run(stack, chain.team, editor, huge)
            return response, time.monotonic() - started

        async def cheap() -> tuple[Any, float]:
            await asyncio.sleep(0.05)  # the hostile run is already holding a worker
            started = time.monotonic()
            response = await stack.client.post(
                f"{BASE}/folders/{other_team}/validate",
                content=cheap_document,
                headers={**auth(editor), "Content-Type": "application/yaml"},
            )
            return response, time.monotonic() - started

        (hostile_response, hostile_s), (cheap_response, cheap_s) = await asyncio.gather(
            hostile(), cheap()
        )

        assert hostile_response.status_code == 200, hostile_response.text
        assert hostile_s < budget + 1
        body = hostile_response.json()
        (items_all,) = findings_for(body, rule_set_id, "items-all")
        assert (items_all["outcome"], items_all["error_code"]) == ("error", "budget_exceeded")
        assert items_all["blocking"] is True
        (many,) = findings_for(body, rule_set_id, "many-matches")
        assert (many["outcome"], many["error_code"]) == ("error", "too_many_matches")
        assert many["blocking"] is True
        counts = counts_of(body, rule_set_id)
        assert counts["errored"] == 2
        assert counts["passed"] == 1  # the normal rule still ran to a verdict
        assert body["report"]["summary"]["publishable"] is False

        assert cheap_response.status_code == 200, cheap_response.text
        assert cheap_s < 2


# -- every D-141 case is refused at publish ------------------------------------------------------

ALWAYS_TRUE = "true"
OVERSIZED = "true" + " " * 2045  # 2,049 bytes, one over max_expression_bytes
SEARCH_TARGET = "$.a[?search(@.n, '(a+)+$')]"
DEEP = "(" * 33 + "true" + ")" * 33
LINUX = sys.platform == "linux"
# 4 GiB of string: cannot finish under the worker's 1 GiB RLIMIT_AS (Linux) and outlives the
# per-rule deadline elsewhere, where no limit is enforced.
STRING_BUILD = "['aaaaaaaaaaaaaaaa']" + ".map(s, s+s)" * 28 + "[0].size() > 0"
PASS_FIXTURE = {"document": {"x": 1}}
FAIL_FIXTURE = {"document": {"x": 2}}


async def refuse_publish(stack: Any, token: str, folder: str, rules: list[dict[str, Any]]) -> Any:
    """Save ``rules`` as a draft, publish it and expect 422; nothing may be recorded."""
    created = await stack.create_rule_set(folder, f"Hostile {uuid.uuid4().hex[:8]}", token=token)
    assert created.status_code == 201, created.text
    rule_set_id = created.json()["id"]
    saved = await stack.put_draft(
        rule_set_id, json.dumps({"rules": rules}), '"r0"', token=token, content_type="text/yaml"
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["check"]["ok"] is False  # the advisory check already says so
    refused = await stack.publish(rule_set_id, saved.headers["ETag"], token=token)
    assert refused.status_code == 422, refused.text
    rows = stack.validation_sql(
        "SELECT count(*) AS n FROM rule_set_version WHERE rule_set_id = %s", rule_set_id
    )
    assert rows[0]["n"] == 0
    assert "ruleset.version_published" not in stack.outbox_actions()
    published = stack.validation_sql(
        "SELECT publish_counter FROM rule_set WHERE id = %s", rule_set_id
    )
    assert published[0]["publish_counter"] == 0
    return refused.json()


def problems_of(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for rule in body["check"]["rules"] for p in rule["problems"]]


async def test_an_oversized_expression_is_refused_at_publish(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, _ = await team_with_pm_and_editor(stack)
        body = await refuse_publish(
            stack,
            pam,
            chain.org,
            [
                rule_json(
                    "big", OVERSIZED, pass_fixtures=[PASS_FIXTURE], fail_fixtures=[FAIL_FIXTURE]
                )
            ],
        )
        assert body["type"] == "urn:dc:problem:rule-invalid"
        assert [p["code"] for p in problems_of(body)] == ["expression_too_large"]


async def test_a_jsonpath_using_search_is_refused_at_publish_as_target_invalid(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ = await team_with_pm_and_editor(stack)
        body = await refuse_publish(
            stack,
            pam,
            chain.org,
            [
                rule_json(
                    "regex-target",
                    "true",
                    target=SEARCH_TARGET,
                    pass_fixtures=[PASS_FIXTURE],
                    fail_fixtures=[FAIL_FIXTURE],
                )
            ],
        )
        assert body["type"] == "urn:dc:problem:rule-invalid"
        assert [p["code"] for p in problems_of(body)] == ["target_invalid"]


async def test_a_33_deep_expression_is_refused_at_publish_as_a_compile_error(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ = await team_with_pm_and_editor(stack)
        body = await refuse_publish(
            stack,
            pam,
            chain.org,
            [rule_json("deep", DEEP, pass_fixtures=[PASS_FIXTURE], fail_fixtures=[FAIL_FIXTURE])],
        )
        assert body["type"] == "urn:dc:problem:rule-invalid"
        assert [p["code"] for p in problems_of(body)] == ["compile_error"]


async def test_a_nested_comprehension_over_10000_elements_in_a_fixture_is_refused(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ = await team_with_pm_and_editor(stack)
        rule = rule_json(
            "iterate",
            "this.l.all(x, x >= 0)",
            pass_fixtures=[{"document": {"l": [0]}}],
            fail_fixtures=[{"document": {"l": [0] * 10_000}}],
        )
        body = await refuse_publish(stack, pam, chain.org, [rule])
        assert body["type"] == "urn:dc:problem:ruleset-fixtures-failed"
        (problem,) = problems_of(body)
        assert problem["code"] == "fixture_misbehaved"
        assert (problem["fixture_kind"], problem["fixture_index"]) == ("fail", 0)
        assert problem["error_code"] == "budget_exceeded"


async def test_a_huge_string_build_in_a_fixture_is_refused_with_the_limit_that_stopped_it(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ = await team_with_pm_and_editor(stack)
        rule = rule_json(
            "string-build",
            STRING_BUILD,
            pass_fixtures=[PASS_FIXTURE],
            fail_fixtures=[PASS_FIXTURE],
        )
        started = time.monotonic()
        body = await refuse_publish(stack, pam, chain.org, [rule])
        assert time.monotonic() - started < 10  # bounded: no hang
        # The scale probe cannot finish either, and a scale problem is not a fixture problem, so
        # the whole refusal is rule-invalid (docs/user/api-guide.md, "Which refusal slug").
        assert body["type"] == "urn:dc:problem:rule-invalid"
        assert {p["code"] for p in problems_of(body)} == {
            "fixture_misbehaved",
            "scale_probe_failed",
        }
        fixture_problems = [p for p in problems_of(body) if p["code"] == "fixture_misbehaved"]
        fail_fixture = next(p for p in fixture_problems if p["fixture_kind"] == "fail")
        # Linux enforces the worker's RLIMIT_AS (memory_exceeded); elsewhere the deadline stops it.
        assert fail_fixture["error_code"] == ("memory_exceeded" if LINUX else "timeout")
        # The service keeps answering after the kill and respawn.
        again = await stack.client.get("/healthz")
        assert again.status_code == 200


async def test_an_always_true_rule_whose_fail_fixture_passes_is_refused(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, _ = await team_with_pm_and_editor(stack)
        rule = rule_json(
            "always",
            ALWAYS_TRUE,
            pass_fixtures=[PASS_FIXTURE],
            fail_fixtures=[{"document": {"x": 2}}],
        )
        body = await refuse_publish(stack, pam, chain.org, [rule])
        assert body["type"] == "urn:dc:problem:ruleset-fixtures-failed"
        (problem,) = problems_of(body)
        assert problem["code"] == "fixture_misbehaved"
        assert (problem["fixture_kind"], problem["fixture_index"]) == ("fail", 0)


async def test_an_always_true_rule_with_no_fail_fixture_is_refused_as_missing_fail_fixture(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ = await team_with_pm_and_editor(stack)
        rule = rule_json("always", ALWAYS_TRUE, pass_fixtures=[PASS_FIXTURE])
        body = await refuse_publish(stack, pam, chain.org, [rule])
        assert body["type"] == "urn:dc:problem:ruleset-fixtures-failed"
        assert body["check"]["rules"][0]["problems"][0]["code"] == "missing_fail_fixture"
        assert body["check"]["ok"] is False


# -- bounded at evaluation -----------------------------------------------------------------------


@pytest.mark.timing  # wall-clock bounds: the full gate runs it without coverage tracing
async def test_a_catastrophic_regex_publishes_and_answers_within_the_budget_at_evaluation(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, editor = await team_with_pm_and_editor(stack)
        rule = rule_json(
            "regex",
            'this.s.matches("(x+x+)+y")',
            pass_fixtures=[{"document": {"s": "xxy"}}],
            fail_fixtures=[{"document": {"s": "xxx"}}],
        )
        created = await stack.create_rule_set(chain.org, "Regex", token=pam)
        rule_set_id = created.json()["id"]
        saved = await stack.put_draft(
            rule_set_id, json.dumps({"rules": [rule]}), '"r0"', token=pam, content_type="text/yaml"
        )
        assert saved.status_code == 200 and saved.json()["check"]["ok"] is True, saved.text
        published = await stack.publish(rule_set_id, saved.headers["ETag"], token=pam)
        assert published.status_code == 201, published.text
        mandated = await stack.client.put(
            f"{BASE}/rule-sets/{rule_set_id}/mandatory",
            json={"mandatory": True},
            headers=auth(pam),
        )
        assert mandated.status_code == 200, mandated.text
        enforced = await stack.client.post(
            f"{BASE}/rule-sets/{rule_set_id}/versions/1/enforce-now", headers=auth(pam)
        )
        assert enforced.status_code == 200, enforced.text

        document = encode(contract(s="x" * 100_000))
        started = time.monotonic()
        response = await post_run(stack, chain.team, editor, document)
        elapsed = time.monotonic() - started

        assert response.status_code == 200, response.text
        assert elapsed < stack.app.state.settings.run_budget_s
        body = response.json()
        # RE2 is linear: the rule gets a real verdict ("no y in the string" fails it), no error.
        (finding,) = findings_for(body, rule_set_id, "regex")
        assert (finding["outcome"], finding["error_code"]) == ("fail", None)
        assert counts_of(body, rule_set_id)["errored"] == 0


async def test_a_hostile_error_in_a_set_not_yet_enforced_does_not_block(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, editor = await team_with_pm_and_editor(stack)
        rule_set_id = await insert_published_set(
            stack,
            chain.org,
            pam,
            [rule_json("many-matches", "true", target="$.customProperties[0].value[*]")],
            enforced=False,
        )
        document = encode(
            contract(customProperties=[{"property": "items", "value": [1] * 100_000}])
        )
        assert len(document) < MAX_DOCUMENT_BYTES

        response = await post_run(stack, chain.team, editor, document)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["report"]["structural"]["valid"] is True
        (finding,) = findings_for(body, rule_set_id, "many-matches")
        assert (finding["outcome"], finding["error_code"]) == ("error", "too_many_matches")
        assert (finding["mode"], finding["blocking"]) == ("warn", False)
        assert body["report"]["summary"] == {"publishable": True, "blocking_count": 0}


# -- the run budget ------------------------------------------------------------------------------

# CPU-bound and memory-neutral: 9,000 iterations, each a regex over a 200,000-character string.
# About 4.7 s per evaluation with about 24 MB RSS (measured at planning time), so every evaluated
# rule ends as `timeout` on every platform and never as `memory_exceeded`.
SLOW = "this.items.all(i, !this.s.matches('(a|b)+c'))"
BUDGET_RULES = [rule_json(f"slow-{n}", SLOW) for n in range(1, 9)]


def slow_document() -> bytes:
    document = encode(contract(items=[1] * 9_000, s="a" * 200_000))
    assert len(document) < MAX_DOCUMENT_BYTES
    return document


@pytest.mark.timing  # wall-clock bounds: the full gate runs it without coverage tracing
async def test_an_exhausted_run_budget_reports_every_unevaluated_rule_as_a_blocking_error(
    make_stack: Any,
) -> None:
    async with make_stack(run_budget_s=1.0, rule_timeout_ms=400) as stack:
        chain, pam, editor = await team_with_pm_and_editor(stack)
        rule_set_id = await insert_published_set(stack, chain.org, pam, BUDGET_RULES)

        started = time.monotonic()
        response = await post_run(stack, chain.team, editor, slow_document())
        elapsed = time.monotonic() - started

        assert response.status_code == 200, response.text
        assert elapsed < 2.0  # run_budget_s plus one second
        body = response.json()
        found = [f for f in body["report"]["findings"] if f["rule_set"]["id"] == rule_set_id]
        assert sorted(f["rule_key"] for f in found) == sorted(r["key"] for r in BUDGET_RULES)
        assert {f["outcome"] for f in found} == {"error"}
        codes = {f["error_code"] for f in found}
        assert codes <= {"timeout", "run_budget_exceeded"}
        assert "timeout" in codes  # the budget was spent on real evaluations
        assert "run_budget_exceeded" in codes  # and the rest are reported, not skipped
        assert all(f["blocking"] is True for f in found)
        counts = counts_of(body, rule_set_id)
        assert (counts["passed"], counts["failed"], counts["errored"]) == (0, 0, 8)
        assert body["report"]["summary"]["publishable"] is False


@pytest.mark.timing  # wall-clock bounds: the full gate runs it without coverage tracing
async def test_the_service_answers_healthz_while_a_run_spends_its_budget(make_stack: Any) -> None:
    async with make_stack(run_budget_s=1.0, rule_timeout_ms=400) as stack:
        chain, pam, editor = await team_with_pm_and_editor(stack)
        await insert_published_set(stack, chain.org, pam, BUDGET_RULES)
        run = asyncio.create_task(post_run(stack, chain.team, editor, slow_document()))
        await asyncio.sleep(0.4)  # the run is mid-budget, holding a worker

        started = time.monotonic()
        health = await stack.client.get("/healthz")
        health_s = time.monotonic() - started

        assert health.status_code == 200
        assert health_s < 1
        assert run.done() is False or run.result().status_code == 200
        assert (await run).status_code == 200
