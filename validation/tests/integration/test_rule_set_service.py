"""Rule-set authoring service: create, draft, check and publish (plan 05-06, RULE-01..04).

Real Postgres 18, a real sandbox (one worker process) and the real CEL engine. The service is
driven directly (no HTTP: the routes arrive in Plan 07).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from dc_core.problems import ProblemError
from dc_rules.checks import FIXTURE_CODES
from dc_rules.model import RuleDef, RuleSetContent, content_sha256
from dc_validation.rulesets import (
    create_rule_set,
    etag_for_rule_set,
    publish_rule_set,
    save_draft,
    set_paused,
)
from dc_validation.sandbox import Sandbox
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from conftest import RuleSetEnv

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"


def table_doc(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def make_rule(
    condition: str = UNIQUE_NAMES,
    *,
    key: str = "unique-property-names",
    target: str = "$.schema[*]",
    passing: list[Any] | None = None,
    failing: list[Any] | None = None,
) -> RuleDef:
    return RuleDef.model_validate(
        {
            "key": key,
            "target": target,
            "condition": condition,
            "severity": "error",
            "message": "Property names must be unique within a table.",
            "fixtures": {
                "pass": [{"document": d} for d in (passing or [table_doc("id", "name")])],
                "fail": [{"document": d} for d in (failing or [table_doc("id", "id")])],
            },
        }
    )


async def new_set(env: RuleSetEnv, name: str = "Naming", kind: str = "team") -> uuid.UUID:
    async with env.uow() as uow:
        rule_set = await create_rule_set(uow, env.facts(kind), name, "naming rules")
        await uow.commit()
        return rule_set.id


async def draft(
    env: RuleSetEnv, rule_set_id: uuid.UUID, rules: list[RuleDef], if_match: str
) -> tuple[Any, bool]:
    async with env.uow() as uow:
        saved, changed = await save_draft(uow, rule_set_id, None, rules, if_match)
        await uow.commit_if_pending()
        uow.session.expunge(saved)  # the unit's rollback on exit would expire it
        return saved, changed


async def publish(
    env: RuleSetEnv, rule_set_id: uuid.UUID, if_match: str | None, **kwargs: Any
) -> tuple[Any, bool]:
    return await publish_rule_set(
        env.session_factory, env.sandbox, env.settings, env.actor, rule_set_id, if_match, **kwargs
    )


def slug(exc: ProblemError) -> str:
    return exc.slug


@pytest.mark.parametrize("kind", ["root", "organization", "project", "team"])
async def test_a_rule_set_is_created_at_any_folder_level_from_the_facts_it_is_given(
    rule_set_env: RuleSetEnv, kind: str
) -> None:
    env = rule_set_env
    facts = env.facts(kind)
    async with env.uow() as uow:
        rule_set = await create_rule_set(uow, facts, "Naming", "naming rules")
        await uow.commit()
    (row,) = env.sql("SELECT * FROM rule_set")
    assert row["scope"] == "folder"
    assert row["folder_id"] == facts.id
    assert row["folder_kind"] == kind
    assert row["ancestor_ids"] == list(facts.ancestor_ids)
    assert row["organization_id"] == facts.organization_id
    assert row["publish_counter"] == 0
    assert row["draft_revision"] == 0
    assert row["draft_rules"] == []
    assert row["created_by"] == env.actor.id
    assert rule_set.id == row["id"]
    (event,) = env.events("ruleset.created")
    assert event["aggregate_id"] == str(row["id"])
    assert event["before"] is None
    assert event["after"]["folder_kind"] == kind
    assert event["scope_path"] == list(facts.ancestor_ids)


async def test_a_name_that_differs_only_in_case_is_409_in_the_same_folder_only(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    await new_set(env, "Naming")
    with pytest.raises(ProblemError) as taken:
        await new_set(env, "naming")
    assert taken.value.status == 409
    assert slug(taken.value) == "rule-set-name-taken"
    # The same name one level up is a different folder, so it is fine.
    await new_set(env, "naming", kind="project")
    assert len(env.events("ruleset.created")) == 2


async def test_the_unique_index_is_the_backstop_when_two_creates_race_past_the_name_check(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    outcomes = await asyncio.gather(
        new_set(env, "Racing"), new_set(env, "racing"), return_exceptions=True
    )
    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], ProblemError)
    assert failures[0].slug == "rule-set-name-taken"
    assert len(env.sql("SELECT 1 FROM rule_set")) == 1


async def test_a_draft_is_saved_under_if_match_and_its_event_carries_keys_and_hash_only(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    rule = make_rule()
    saved, changed = await draft(env, rule_set_id, [rule], etag_for_rule_set(0))
    assert changed
    assert saved.draft_revision == 1
    (row,) = env.sql("SELECT * FROM rule_set")
    assert row["draft_revision"] == 1
    assert row["draft_rules"][0]["key"] == "unique-property-names"
    assert row["draft_rules"][0]["fixtures"]["pass"][0]["document"] == table_doc("id", "name")
    (event,) = env.events("ruleset.draft_updated")
    after = event["after"]
    assert after["rule_keys"] == ["unique-property-names"]
    assert after["content_sha256"] == row["draft_content_sha256"]
    assert after["draft_revision"] == 1
    text = json.dumps(event, default=str)
    assert "condition" not in after
    assert "fixtures" not in after
    assert "draft_rules" not in after
    assert UNIQUE_NAMES not in text
    assert "Property names must be unique" not in text


async def test_a_draft_save_needs_a_current_if_match(rule_set_env: RuleSetEnv) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    with pytest.raises(ProblemError) as missing:
        await draft(env, rule_set_id, [make_rule()], None)  # type: ignore[arg-type]
    assert missing.value.status == 428
    with pytest.raises(ProblemError) as stale:
        await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(7))
    assert stale.value.status == 412
    assert env.events("ruleset.draft_updated") == []


async def test_publishing_the_checked_draft_creates_version_one_with_a_canonical_hash(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    rule = make_rule()
    await draft(env, rule_set_id, [rule], etag_for_rule_set(0))

    version, created = await publish(env, rule_set_id, etag_for_rule_set(1))

    assert created
    assert version.version_no == 1
    assert version.dsl_version == "cel-2"
    canonical = json.dumps(
        {"dsl_version": "cel-2", "rules": [rule.model_dump(mode="json", by_alias=True)]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert version.content_sha256 == hashlib.sha256(canonical.encode()).hexdigest()
    (row,) = env.sql("SELECT * FROM rule_set_version")
    assert row["rule_set_id"] == rule_set_id
    assert row["rule_keys"] == ["unique-property-names"]
    assert row["rules"][0]["condition"] == UNIQUE_NAMES
    assert env.sql("SELECT publish_counter FROM rule_set")[0]["publish_counter"] == 1

    (event,) = env.events("ruleset.version_published")
    shown = event["after"]["version"]
    assert shown["rule_keys"] == ["unique-property-names"]
    assert shown["content_sha256"] == version.content_sha256
    assert shown["version_no"] == 1
    text = json.dumps(event, default=str)
    assert UNIQUE_NAMES not in text
    assert "fixtures" not in text
    assert "rules" not in shown
    assert event["aggregate_version"] == 1


async def test_a_draft_carried_over_from_a_cel1_version_is_published_as_cel2(
    rule_set_env: RuleSetEnv,
) -> None:
    """D-152: drafts compile under cel-2; the published cel-1 version stays untouched."""
    env = rule_set_env
    rule_set_id = await new_set(env)
    rule = make_rule()
    await draft(env, rule_set_id, [rule], etag_for_rule_set(0))
    rules_json = [rule.model_dump(mode="json", by_alias=True)]
    legacy = content_sha256(
        RuleSetContent.model_validate({"dsl_version": "cel-1", "rules": rules_json})
    )
    env.sql(
        "INSERT INTO rule_set_version (rule_set_id, version_no, dsl_version, rules,"
        " content_sha256, rule_keys, created_by, updated_by)"
        " VALUES (%s, 1, 'cel-1', %s::jsonb, %s, %s, 'test', 'test') RETURNING id",
        rule_set_id,
        json.dumps(rules_json),
        legacy,
        [rule.key],
    )
    env.sql("UPDATE rule_set SET publish_counter = 1 WHERE id = %s RETURNING id", rule_set_id)

    version, created = await publish(env, rule_set_id, etag_for_rule_set(1))

    assert created
    assert (version.version_no, version.dsl_version) == (2, "cel-2")
    assert version.content_sha256 != legacy
    rows = env.sql("SELECT version_no, dsl_version FROM rule_set_version ORDER BY version_no")
    assert [(r["version_no"], r["dsl_version"]) for r in rows] == [(1, "cel-1"), (2, "cel-2")]


async def test_a_rule_without_a_target_defaults_to_the_root_and_description_may_be_null(
    rule_set_env: RuleSetEnv,
) -> None:
    rule = RuleDef.model_validate(
        {
            "key": "has-name",
            "condition": "has(this.name)",
            "severity": "error",
            "message": "A contract needs a name.",
            "fixtures": {"pass": [{"document": {"name": "x"}}], "fail": [{"document": {}}]},
        }
    )
    assert rule.target == "$"
    assert rule.description is None
    env = rule_set_env
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [rule], etag_for_rule_set(0))
    _, created = await publish(env, rule_set_id, etag_for_rule_set(1))
    assert created


# --- empty, idempotency ------------------------------------------------------------------------


async def test_an_empty_draft_can_be_saved_but_not_published(rule_set_env: RuleSetEnv) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    saved, changed = await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(0))
    assert changed
    saved, changed = await draft(env, rule_set_id, [], etag_for_rule_set(1))
    assert changed
    assert saved.draft_rules == []
    assert saved.draft_revision == 2

    with pytest.raises(ProblemError) as refused:
        await publish(env, rule_set_id, etag_for_rule_set(2))
    assert refused.value.status == 422
    assert slug(refused.value) == "rule-invalid"
    codes = [p["code"] for p in refused.value.extensions["check"]["rule_set_problems"]]
    assert codes == ["empty_rule_set"]
    assert env.sql("SELECT 1 FROM rule_set_version") == []


async def test_saving_unchanged_content_is_a_no_op_with_no_event(rule_set_env: RuleSetEnv) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(0))
    saved, changed = await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(1))
    assert not changed
    assert saved.draft_revision == 1
    assert len(env.events("ruleset.draft_updated")) == 1


async def test_publishing_an_already_published_draft_returns_that_version_and_no_event(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(0))
    first, created = await publish(env, rule_set_id, etag_for_rule_set(1))
    assert created
    again, created_again = await publish(env, rule_set_id, etag_for_rule_set(1))
    assert not created_again
    assert again.id == first.id
    assert len(env.sql("SELECT 1 FROM rule_set_version")) == 1
    assert len(env.events("ruleset.version_published")) == 1

    # A changed draft publishes version 2 and keeps version 1 untouched.
    other = make_rule(key="second-rule")
    await draft(env, rule_set_id, [make_rule(), other], etag_for_rule_set(1))
    second, created_second = await publish(env, rule_set_id, etag_for_rule_set(2))
    assert created_second
    assert second.version_no == 2


# --- refusals ----------------------------------------------------------------------------------


async def test_a_rule_whose_fail_fixture_passes_is_refused_with_a_per_rule_report(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule("true")], etag_for_rule_set(0))
    with pytest.raises(ProblemError) as refused:
        await publish(env, rule_set_id, etag_for_rule_set(1))
    assert refused.value.status == 422
    assert slug(refused.value) == "ruleset-fixtures-failed"
    problems = refused.value.extensions["check"]["rules"][0]["problems"]
    assert problems[0]["code"] == "fixture_misbehaved"
    assert problems[0]["fixture_kind"] == "fail"
    assert problems[0]["fixture_index"] == 0
    assert {p["code"] for p in problems} <= FIXTURE_CODES
    assert env.sql("SELECT 1 FROM rule_set_version") == []
    assert env.events("ruleset.version_published") == []


async def test_a_wrong_pass_fixture_alone_is_also_a_fixture_failure(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    bad_pass = make_rule(passing=[table_doc("id", "id")])
    await draft(env, rule_set_id, [bad_pass], etag_for_rule_set(0))
    with pytest.raises(ProblemError) as refused:
        await publish(env, rule_set_id, etag_for_rule_set(1))
    assert slug(refused.value) == "ruleset-fixtures-failed"


async def test_the_sandbox_check_runs_with_no_session_or_lock_held(
    rule_set_env: RuleSetEnv,
) -> None:
    """While the check runs, another connection can lock the rule set row (nothing is held)."""
    env = rule_set_env
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(0))
    seen: list[bool] = []

    async def probe() -> None:
        async with env.session_factory() as session:
            from sqlalchemy import text

            row = await session.execute(
                text("SELECT id FROM rule_set WHERE id = :i FOR UPDATE NOWAIT"), {"i": rule_set_id}
            )
            seen.append(row.first() is not None)

    await publish(env, rule_set_id, etag_for_rule_set(1), after_check=probe)
    assert seen == [True]


async def test_a_saturated_sandbox_is_503_with_retry_after_never_a_pass(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(0))
    small = env.settings.model_copy(update={"max_concurrent_runs": 1, "queue_wait_s": 0.1})
    sandbox = Sandbox(small)
    try:
        async with sandbox.slot():
            with pytest.raises(ProblemError) as busy:
                await publish_rule_set(
                    env.session_factory,
                    sandbox,
                    small,
                    env.actor,
                    rule_set_id,
                    etag_for_rule_set(1),
                )
    finally:
        await sandbox.aclose()
    assert busy.value.status == 503
    assert slug(busy.value) == "validation-unavailable"
    assert busy.value.headers == {"Retry-After": "1"}
    assert env.sql("SELECT 1 FROM rule_set_version") == []


# --- refusal codes (plan 05-06 task 2) ---------------------------------------------------------


class SpySandbox:
    """Records every unit list a check sends, then delegates to the real sandbox."""

    def __init__(self, inner: Sandbox) -> None:
        self.inner = inner
        self.calls: list[list[Any]] = []

    async def check(self, units: list[Any]) -> Any:
        self.calls.append(units)
        return await self.inner.check(units)


async def publish_refused(env: RuleSetEnv, rules: list[RuleDef]) -> ProblemError:
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, rules, etag_for_rule_set(0))
    with pytest.raises(ProblemError) as refused:
        await publish(env, rule_set_id, etag_for_rule_set(1))
    assert refused.value.status == 422
    assert env.sql("SELECT 1 FROM rule_set_version") == []
    assert env.events("ruleset.version_published") == []
    return refused.value


def problem_codes(error: ProblemError) -> list[str]:
    check = error.extensions["check"]
    return [p["code"] for p in check["rule_set_problems"]] + [
        p["code"] for rule in check["rules"] for p in rule["problems"]
    ]


async def test_an_always_true_rule_with_no_fail_fixture_is_missing_a_fail_fixture(
    rule_set_env: RuleSetEnv,
) -> None:
    rule = RuleDef.model_validate(
        {
            "key": "always-true",
            "condition": "true",
            "severity": "error",
            "message": "Vacuous rule.",
            "fixtures": {"pass": [{"document": {}}], "fail": []},
        }
    )
    refused = await publish_refused(rule_set_env, [rule])
    assert refused.slug == "ruleset-fixtures-failed"
    assert problem_codes(refused) == ["missing_fail_fixture"]


async def test_a_cel_syntax_error_is_rule_invalid_with_a_compile_error(
    rule_set_env: RuleSetEnv,
) -> None:
    refused = await publish_refused(rule_set_env, [make_rule("this.properties.map(")])
    assert refused.slug == "rule-invalid"
    problem = refused.extensions["check"]["rules"][0]["problems"][0]
    assert problem["code"] == "compile_error"
    assert problem["detail"]


async def test_one_compile_error_among_fixture_failures_makes_the_whole_refusal_rule_invalid(
    rule_set_env: RuleSetEnv,
) -> None:
    rules = [make_rule("this.properties.map(", key="broken"), make_rule("true", key="vacuous")]
    refused = await publish_refused(rule_set_env, rules)
    assert refused.slug == "rule-invalid"
    codes = problem_codes(refused)
    assert "compile_error" in codes
    assert "fixture_misbehaved" in codes
    assert not set(codes) <= FIXTURE_CODES


async def test_a_2049_byte_condition_is_rule_invalid_and_never_reaches_the_sandbox(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    condition = "true" + " " * (2049 - 4)
    assert len(condition.encode()) == 2049
    spy = SpySandbox(env.sandbox)
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule(condition)], etag_for_rule_set(0))
    with pytest.raises(ProblemError) as refused:
        await publish_rule_set(
            env.session_factory,
            spy,  # type: ignore[arg-type]
            env.settings,
            env.actor,
            rule_set_id,
            etag_for_rule_set(1),
        )
    assert refused.value.slug == "rule-invalid"
    assert "expression_too_large" in problem_codes(refused.value)
    assert "compile_error" not in problem_codes(refused.value)
    assert spy.calls == []


# --- If-Match and the check-then-lock race -----------------------------------------------------


async def test_publish_needs_a_current_if_match_before_any_check_runs(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    spy = SpySandbox(env.sandbox)
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(0))
    for header, status in ((None, 428), (etag_for_rule_set(0), 412), ('W/"r1"', 412)):
        with pytest.raises(ProblemError) as refused:
            await publish_rule_set(
                env.session_factory,
                spy,  # type: ignore[arg-type]
                env.settings,
                env.actor,
                rule_set_id,
                header,
            )
        assert refused.value.status == status
    assert spy.calls == []
    assert env.sql("SELECT 1 FROM rule_set_version") == []


async def test_a_draft_saved_between_the_check_and_the_lock_makes_publish_412(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(0))

    async def edit_meanwhile() -> None:
        # The unchecked rules a second author saves while the first publish is between the two.
        await draft(env, rule_set_id, [make_rule("true", key="unchecked")], etag_for_rule_set(1))

    with pytest.raises(ProblemError) as refused:
        await publish(env, rule_set_id, etag_for_rule_set(1), after_check=edit_meanwhile)
    assert refused.value.status == 412
    assert slug(refused.value) == "precondition-failed"
    assert env.sql("SELECT 1 FROM rule_set_version") == []
    assert env.events("ruleset.version_published") == []
    assert env.sql("SELECT draft_revision FROM rule_set")[0]["draft_revision"] == 2


# --- concurrency -------------------------------------------------------------------------------


async def test_two_concurrent_draft_saves_with_the_same_if_match_one_wins_and_one_is_412(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    outcomes = await asyncio.gather(
        draft(env, rule_set_id, [make_rule(key="first")], etag_for_rule_set(0)),
        draft(env, rule_set_id, [make_rule(key="second")], etag_for_rule_set(0)),
        return_exceptions=True,
    )
    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], ProblemError)
    assert failures[0].status == 412
    assert env.sql("SELECT draft_revision FROM rule_set")[0]["draft_revision"] == 1
    assert len(env.events("ruleset.draft_updated")) == 1


async def test_two_concurrent_publishes_of_the_same_draft_create_exactly_one_version(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(0))
    outcomes = await asyncio.gather(
        publish(env, rule_set_id, etag_for_rule_set(1)),
        publish(env, rule_set_id, etag_for_rule_set(1)),
        return_exceptions=True,
    )
    results = [o for o in outcomes if not isinstance(o, BaseException)]
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert all(isinstance(e, ProblemError) and e.status == 412 for e in errors)
    assert [created for _, created in results].count(True) == 1
    assert len(env.sql("SELECT 1 FROM rule_set_version")) == 1
    assert len(env.events("ruleset.version_published")) == 1


async def test_version_numbers_stay_contiguous_under_concurrent_publishes(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    revision = 0
    for round_no in range(1, 4):
        rules = [make_rule(key=f"rule-{n}") for n in range(round_no)]
        await draft(env, rule_set_id, rules, etag_for_rule_set(revision))
        revision += 1
        outcomes = await asyncio.gather(
            *[publish(env, rule_set_id, etag_for_rule_set(revision)) for _ in range(3)]
        )
        assert [created for _, created in outcomes].count(True) == 1
        assert {version.version_no for version, _ in outcomes} == {round_no}
    numbers = [r["version_no"] for r in env.sql("SELECT version_no FROM rule_set_version")]
    assert sorted(numbers) == [1, 2, 3]
    assert env.sql("SELECT publish_counter FROM rule_set")[0]["publish_counter"] == 3


# --- fail-closed edges of the service ----------------------------------------------------------


async def test_a_draft_changed_under_the_same_revision_after_the_check_makes_publish_412(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    rule_set_id = await new_set(env)
    await draft(env, rule_set_id, [make_rule()], etag_for_rule_set(0))

    async def tamper() -> None:
        # Content that is not the checked content, while the revision (the ETag) stays the same.
        env.sql(
            "UPDATE rule_set SET draft_content_sha256 = repeat('0', 64) WHERE id = %s RETURNING id",
            rule_set_id,
        )

    with pytest.raises(ProblemError) as refused:
        await publish(env, rule_set_id, etag_for_rule_set(1), after_check=tamper)
    assert refused.value.status == 412
    assert slug(refused.value) == "precondition-failed"
    assert env.sql("SELECT 1 FROM rule_set_version") == []
    assert env.events("ruleset.version_published") == []


async def test_publishing_or_saving_an_unknown_rule_set_is_the_uniform_404(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    with pytest.raises(ProblemError) as publish_refused:
        await publish(env, uuid.uuid4(), etag_for_rule_set(0))
    with pytest.raises(ProblemError) as save_refused:
        await draft(env, uuid.uuid4(), [make_rule()], etag_for_rule_set(0))
    for refused in (publish_refused, save_refused):
        assert refused.value.status == 404
        assert slug(refused.value) == "not-found"
    assert env.events() == []


async def test_a_constraint_other_than_the_name_index_is_not_reported_as_a_taken_name(
    rule_set_env: RuleSetEnv,
) -> None:
    env = rule_set_env
    with pytest.raises(IntegrityError) as refused:
        await new_set(env, "n" * 201)  # the routes cap names first; the CHECK is the backstop
    assert "name_length" in str(refused.value)
    assert env.sql("SELECT 1 FROM rule_set") == []


async def test_only_a_mandatory_platform_rule_set_can_be_paused(rule_set_env: RuleSetEnv) -> None:
    env = rule_set_env
    folder_set = await new_set(env, "Folder set")
    platform_set = await new_set(env, "Platform set")
    env.sql(
        "UPDATE rule_set SET scope = 'platform', folder_id = NULL, folder_kind = NULL,"
        " mandatory = false WHERE id = %s RETURNING id",
        platform_set,
    )
    events_before = len(env.events())

    async def pause(rule_set_id: uuid.UUID) -> ProblemError:
        async with env.uow() as uow:
            with pytest.raises(ProblemError) as refused:
                await set_paused(uow, rule_set_id, True)
        return refused.value

    folder_refusal = await pause(folder_set)
    assert (folder_refusal.status, folder_refusal.slug) == (404, "not-found")
    optional_refusal = await pause(platform_set)
    assert (optional_refusal.status, optional_refusal.slug) == (409, "rule-set-not-mandatory")
    assert env.sql("SELECT count(*) AS n FROM rule_set WHERE paused_at IS NOT NULL")[0]["n"] == 0
    assert len(env.events()) == events_before
