"""The rule-set check: compile, size, fixtures and the scale probe (plan 05-04, RULE-02).

The worker loop is driven in-process through `Kit.check` (a real engine, no process kill, so a
case here must never hang). The same two tracer cases run through a real `Sandbox` in
`validation/tests/unit/test_sandbox.py`.
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

import pytest
from dc_rules.checks import (
    EMPTY_REFS,
    FIXTURE_CODES,
    check_units,
    scale_probe_document,
    static_problems,
)
from dc_rules.model import RuleSetContent
from dc_rules.ports import Limits
from dc_rules.protocol import CheckJob
from dc_rules.worker import serve

if TYPE_CHECKING:
    from conftest import Kit

pytestmark = pytest.mark.unit


def test_a_good_rule_with_a_pass_and_a_fail_fixture_checks_ok(kit: Kit) -> None:
    report = kit.check(kit.content(kit.rule()))
    assert report.ok
    assert report.rule_set_problems == []
    assert [(r.rule_key, r.ok, r.problems) for r in report.rules] == [("unique-names", True, [])]


def test_an_always_true_rules_fail_fixture_is_reported_as_misbehaving(kit: Kit) -> None:
    report = kit.check(kit.content(kit.rule("true")))
    assert not report.ok
    [problem] = report.rules[0].problems
    assert problem.code == "fixture_misbehaved"
    assert (problem.fixture_kind, problem.fixture_index) == ("fail", 0)
    assert problem.detail is not None and "pass" in problem.detail
    assert problem.error_code is None


def test_fixture_codes_are_exactly_the_six_fixture_problems() -> None:
    assert FIXTURE_CODES == {
        "fixture_changes_unavailable",
        "missing_pass_fixture",
        "missing_fail_fixture",
        "too_many_fixtures",
        "fixture_too_large",
        "fixture_misbehaved",
    }


def test_an_expression_over_the_byte_limit_is_refused_without_engine_work(kit: Kit) -> None:
    content = kit.content(kit.rule("true" + " " * 2045))  # 2,049 bytes
    assert len(content.rules[0].condition.encode()) == 2049
    assert check_units(content) == []
    assert kit.codes(kit.check(content)) == ["expression_too_large"]
    # the limit counts UTF-8 bytes, not characters
    wide = kit.content(kit.rule("'" + "é" * 1024 + "' == 'x'"))
    assert len(wide.rules[0].condition) < 2048 < len(wide.rules[0].condition.encode())
    assert kit.codes(kit.check(wide)) == ["expression_too_large"]


def test_a_target_over_the_character_limit_is_refused(kit: Kit) -> None:
    content = kit.content(kit.rule(target="$." + "a" * 511))  # 513 characters
    assert kit.codes(kit.check(content)) == ["target_too_long"]
    exact = kit.content(kit.rule(target="$." + "a" * 510))
    assert "target_too_long" not in kit.codes(kit.check(exact))


def test_a_compiled_expression_over_the_ast_limit_is_refused(kit: Kit) -> None:
    report = kit.check(kit.content(kit.rule()), Limits(max_compiled_bytes=50))
    assert kit.codes(report) == ["ast_too_large"]
    assert report.rules[0].problems[0].detail is not None


def test_compile_problems_are_named_and_hide_nothing_else(kit: Kit) -> None:
    bad_target = kit.check(kit.content(kit.rule(target="$.a[?search(@.n, 'x')]")))
    assert kit.codes(bad_target) == ["target_invalid"]
    syntax = kit.check(kit.content(kit.rule("this.properties.all(")))
    assert kit.codes(syntax) == ["compile_error"]
    not_bool = kit.check(kit.content(kit.rule("this.properties.size()")))
    assert kit.codes(not_bool) == ["not_boolean"]
    # author feedback is present and bounded
    [problem] = syntax.rules[0].problems
    assert problem.detail is not None and len(problem.detail) <= 300


def test_set_level_problems_are_reported_apart_from_rules(kit: Kit) -> None:
    empty = kit.check(kit.content())
    assert not empty.ok and [p.code for p in empty.rule_set_problems] == ["empty_rule_set"]

    many = [kit.rule(key=f"r{i}", passing=[], failing=[]) for i in range(201)]
    over = static_problems(kit.content(*many), Limits())
    assert [p.code for p in over][0] == "too_many_rules"

    dup = kit.check(kit.content(kit.rule(key="a"), kit.rule(key="a")))
    assert [(p.code, p.rule_key) for p in dup.rule_set_problems] == [("duplicate_rule_key", "a")]
    assert [r.ok for r in dup.rules] == [True, True]
    assert not dup.ok


def test_a_missing_key_error_on_a_fail_fixture_is_not_as_declared(kit: Kit) -> None:
    rule = kit.rule(
        "this.properties.all(p, has(p.name))",
        failing=[{"schema": [{"name": "orders"}]}],  # no `properties`: no_such_key
    )
    [problem] = kit.check(kit.content(rule)).rules[0].problems
    assert problem.code == "fixture_misbehaved"
    assert (problem.fixture_kind, problem.fixture_index) == ("fail", 0)
    assert problem.error_code == "no_such_key"


def test_a_pass_fixture_that_errors_is_not_as_declared(kit: Kit) -> None:
    rule = kit.rule(
        "this.properties.all(p, has(p.name))", passing=[{"schema": [{"name": "orders"}]}]
    )
    problems = kit.check(kit.content(rule)).rules[0].problems
    assert [(p.code, p.fixture_kind, p.error_code) for p in problems] == [
        ("fixture_misbehaved", "pass", "no_such_key"),
        ("fixture_misbehaved", "fail", None),  # the default fail fixture simply passes
    ]


def test_an_always_true_rule_without_a_fail_fixture_is_missing_a_fail_fixture(kit: Kit) -> None:
    report = kit.check(kit.content(kit.rule("true", failing=[])))
    assert kit.codes(report) == ["missing_fail_fixture"]  # and no fixture_misbehaved for it


def test_a_rule_without_a_pass_fixture_is_missing_a_pass_fixture(kit: Kit) -> None:
    report = kit.check(kit.content(kit.rule(passing=[])))
    assert kit.codes(report) == ["missing_pass_fixture"]
    both = kit.check(kit.content(kit.rule(passing=[], failing=[])))
    assert kit.codes(both) == ["missing_pass_fixture", "missing_fail_fixture"]


def test_the_same_document_as_pass_and_fail_always_misbehaves_once(kit: Kit) -> None:
    doc = kit.table("id", "name")
    report = kit.check(kit.content(kit.rule(passing=[doc], failing=[doc])))
    [problem] = report.rules[0].problems
    assert problem.code == "fixture_misbehaved" and problem.fixture_kind == "fail"
    doc = kit.table("id", "id")
    report = kit.check(kit.content(kit.rule(passing=[doc], failing=[doc])))
    [problem] = report.rules[0].problems
    assert problem.code == "fixture_misbehaved" and problem.fixture_kind == "pass"


def test_five_fixtures_of_a_kind_are_accepted_and_six_are_too_many(kit: Kit) -> None:
    five = kit.check(
        kit.content(kit.rule(passing=[kit.table("a", "b")] * 5, failing=[kit.table("a", "a")] * 5))
    )
    assert five.ok
    six = kit.check(
        kit.content(kit.rule(passing=[kit.table("a", "b")] * 6, failing=[kit.table("a", "a")]))
    )
    assert kit.codes(six) == ["too_many_fixtures"]
    assert six.rules[0].problems[0].fixture_kind == "pass"


def test_a_fixture_of_exactly_the_byte_limit_is_accepted_and_one_more_is_too_large(
    kit: Kit,
) -> None:
    limits = Limits(max_fixture_bytes=100)
    # {"k":"<n x>"} is n + 8 canonical JSON bytes
    exact = {"k": "x" * 92}
    over = {"k": "x" * 93}
    ok = kit.check(kit.content(kit.rule("true", passing=[exact], failing=[kit.table("a")])), limits)
    assert "fixture_too_large" not in kit.codes(ok)
    too_big = kit.check(kit.content(kit.rule("true", passing=[over], failing=[])), limits)
    problem = next(p for p in too_big.rules[0].problems if p.code == "fixture_too_large")
    assert (problem.fixture_kind, problem.fixture_index) == ("pass", 0)


@pytest.mark.parametrize("empty", [None, {}])
def test_a_null_or_empty_fixture_is_evaluated_as_given(kit: Kit, empty: object) -> None:
    # The target matches nothing in the fixture, so `on_empty` decides.
    as_pass = kit.check(
        kit.content(kit.rule(on_empty="pass", passing=[empty], failing=[kit.table("a", "a")]))
    )
    assert as_pass.ok
    as_fail = kit.check(
        kit.content(kit.rule(on_empty="pass", passing=[kit.table("a")], failing=[empty]))
    )
    [problem] = as_fail.rules[0].problems
    assert (problem.code, problem.fixture_kind) == ("fixture_misbehaved", "fail")
    declared = kit.check(
        kit.content(kit.rule(on_empty="fail", passing=[kit.table("a")], failing=[empty]))
    )
    assert declared.ok


def test_problems_come_in_the_fixed_order_and_repeat_identically(kit: Kit) -> None:
    rule = kit.rule(
        "true",
        passing=[kit.table("a", "b"), {"schema": [{"name": "t"}]}],  # 1: no properties, but true
        failing=[kit.table("a", "a"), kit.table("c", "c")],  # both misbehave (always true)
    )
    # fixtures come pass-by-index then fail-by-index; here only the fail fixtures misbehave
    content = kit.content(rule, kit.rule("true", key="second", failing=[]))
    first = kit.check(content)
    again = kit.check(content)
    assert first == again
    assert [r.rule_key for r in first.rules] == ["unique-names", "second"]
    assert [(p.code, p.fixture_kind, p.fixture_index) for p in first.rules[0].problems] == [
        ("fixture_misbehaved", "fail", 0),
        ("fixture_misbehaved", "fail", 1),
    ]
    assert kit.codes(first, 1) == ["missing_fail_fixture"]


def test_a_rule_that_does_not_scale_fails_the_probe_but_a_missing_key_does_not(kit: Kit) -> None:
    # Cubic in the number of tables and columns: tiny fixtures pass, the 100 x 100 probe cannot.
    cubic = (
        "contract.schema.all(s, contract.schema.all(t, "
        "s.properties.all(p, t.properties.size() > 0)))"
    )
    small = {"schema": [{"name": "t", "properties": [{"name": "a"}]}]}
    report = kit.check(kit.content(kit.rule(cubic, target="$", passing=[small], failing=[])))
    assert "scale_probe_failed" in kit.codes(report)
    probe = next(p for p in report.rules[0].problems if p.code == "scale_probe_failed")
    assert probe.error_code == "budget_exceeded"

    # A document without `info` makes this error no_such_key on the probe: not a scale problem.
    unguarded = kit.rule(
        "this.info.title != ''",
        target="$",
        passing=[{"info": {"title": "x"}}],
        failing=[{"info": {"title": ""}}],
    )
    assert kit.check(kit.content(unguarded)).ok


def test_the_two_level_nest_over_every_table_and_property_is_refused_by_the_probe(
    kit: Kit,
) -> None:
    # Orchestrator decision (plan 05-06): 100 x 100 = 10,100 iterations is over the engine budget,
    # so the plain nest from research Pitfall 6 is refused at authoring time, not in production.
    nested = "contract.schema.all(s, s.properties.all(p, has(p.name)))"
    small = {"schema": [{"name": "t", "properties": [{"name": "a"}]}]}
    nameless = {"schema": [{"name": "t", "properties": [{"description": "no name"}]}]}
    report = kit.check(
        kit.content(kit.rule(nested, target="$", passing=[small], failing=[nameless]))
    )
    assert kit.codes(report) == ["scale_probe_failed"]
    assert report.rules[0].problems[0].error_code == "budget_exceeded"

    # The same nest over a single table level is within budget and stays publishable.
    flat = kit.rule(
        "this.properties.all(p, has(p.name))",
        target="$.schema[*]",
        passing=[small],
        failing=[nameless],
    )
    assert kit.check(kit.content(flat)).ok


def test_the_scale_probe_document_has_100_tables_of_100_properties() -> None:
    document = scale_probe_document()
    assert [t["name"] for t in document["schema"]] == [f"t{i}" for i in range(100)]
    assert all(len(t["properties"]) == 100 for t in document["schema"])
    assert document["schema"][3]["properties"][7] == {"name": "p7", "logicalType": "string"}


def test_unit_ids_are_stable_and_a_missing_answer_fails_the_rule_closed(kit: Kit) -> None:
    from dc_rules.checks import assemble_check

    content: RuleSetContent = kit.content(kit.rule())
    units = check_units(content)
    assert [u.id for u in units] == list(range(len(units)))
    assert [u.kind for u in units] == ["compile", "fixture", "fixture", "probe"]
    report = assemble_check(content, [], {})  # the sandbox returned nothing at all
    assert not report.ok and not report.rules[0].ok


def test_a_compile_unit_that_errored_fails_the_rule_once_with_its_error_code(kit: Kit) -> None:
    from dc_rules.checks import CheckProblem, assemble_check
    from dc_rules.protocol import error_result

    content: RuleSetContent = kit.content(kit.rule())
    # The worker ran out of memory on every unit, the compile unit first.
    results = {u.id: error_result(u.id, "memory_exceeded") for u in check_units(content)}
    report = assemble_check(content, [], results)
    assert not report.ok
    assert report.rules[0].problems == [
        CheckProblem(
            rule_key="unique-names",
            code="compile_error",
            detail="the check did not finish",
            error_code="memory_exceeded",
        )
    ]  # the fixtures and the probe of a rule that never compiled are not judged


# --- cel-2 fixtures (D-152) -------------------------------------------------------------------

NO_BREAKING = "changes.all(c, c['class'] != 'breaking')"


def fake_changes_for(previous: Any, document: Any) -> list[Any]:
    """A stand-in classifier: a breaking change when a property of previous is gone."""
    old = {p["name"] for t in previous["schema"] for p in t["properties"]}
    new = {p["name"] for t in document["schema"] for p in t["properties"]}
    return [{"class": "breaking", "code": "property-removed", "path": n} for n in sorted(old - new)]


def breaking_rule(kit: Kit, condition: str = NO_BREAKING) -> dict[str, Any]:
    rule = kit.rule(condition, target="$")
    rule["fixtures"]["pass"] = [
        {"document": kit.table("id"), "previous": kit.table("id")},
    ]
    rule["fixtures"]["fail"] = [
        {"document": kit.table("id"), "previous": kit.table("id", "gone")},
    ]
    return rule


def test_a_fixture_with_previous_binds_the_injected_changes(kit: Kit) -> None:
    report = kit.check(kit.content(breaking_rule(kit)), changes_for=fake_changes_for)
    assert report.ok, report.rules[0].problems


def test_a_fixture_with_previous_and_no_changes_for_is_a_check_problem(kit: Kit) -> None:
    report = kit.check(kit.content(breaking_rule(kit)))
    problems = report.rules[0].problems
    assert [(p.code, p.fixture_kind, p.fixture_index) for p in problems] == [
        ("fixture_changes_unavailable", "pass", 0),
        ("fixture_changes_unavailable", "fail", 0),
    ]
    assert not report.ok


def test_a_changes_for_that_raises_is_unavailable_not_a_crash(kit: Kit) -> None:
    def boom(previous: Any, document: Any) -> list[Any]:
        raise ValueError("secret document text")

    report = kit.check(kit.content(breaking_rule(kit)), changes_for=boom)
    assert {p.code for p in report.rules[0].problems} == {"fixture_changes_unavailable"}
    assert "secret" not in report.model_dump_json()


def test_a_fixture_without_previous_binds_a_null_previous_and_no_changes(kit: Kit) -> None:
    rule = kit.rule("previous == null && changes.size() == 0 && refs.teams.size() == 0")
    rule["fixtures"]["fail"] = [{"document": kit.table("id"), "previous": kit.table("id")}]
    report = kit.check(kit.content(rule), changes_for=lambda previous, document: [])
    assert report.ok, report.rules[0].problems


def test_fixture_refs_are_bound_and_default_to_the_empty_maps(kit: Kit) -> None:
    guard = "!('a.b' in refs.elements) || refs.elements['a.b'] != 'not_found'"
    rule = kit.rule(guard, target="$")
    rule["fixtures"]["pass"] = [
        {"document": {}},
        {"document": {}, "refs": {"elements": {"a.b": "found"}}},
    ]
    rule["fixtures"]["fail"] = [{"document": {}, "refs": {"elements": {"a.b": "not_found"}}}]
    assert kit.check(kit.content(rule)).ok
    assert set(EMPTY_REFS) == {"elements", "servers", "contracts", "principals", "teams"}


def test_the_units_are_stamped_with_the_rule_sets_dsl_version(kit: Kit) -> None:
    content = kit.content(kit.rule())
    assert {u.dsl_version for u in check_units(content)} == {"cel-2"}
    legacy = RuleSetContent.model_validate(
        {**content.model_dump(by_alias=True), "dsl_version": "cel-1"}
    )
    assert {u.dsl_version for u in check_units(legacy)} == {"cel-1"}


def test_a_cel1_rule_set_rejects_fixtures_with_previous_or_refs(kit: Kit) -> None:
    for extra in ({"previous": {}}, {"refs": {}}):
        rule = kit.rule()
        rule["fixtures"]["pass"][0].update(extra)
        with pytest.raises(ValueError, match="cel-2"):
            RuleSetContent.model_validate({"dsl_version": "cel-1", "rules": [rule]})


def test_a_cel1_rule_over_previous_does_not_compile_in_a_cel1_set(kit: Kit) -> None:
    rule = kit.rule("previous == null")
    content = RuleSetContent.model_validate({"dsl_version": "cel-1", "rules": [rule]})
    assert kit.codes(kit.check(content))[0] == "compile_error"


def probe_outcome(kit: Kit, condition: str) -> str:
    """Run only the scale-probe unit of a one-rule cel-2 set and return its outcome."""
    content = kit.content(kit.rule(condition, target="$"))
    (probe,) = [u for u in check_units(content) if u.kind == "probe"]
    out = io.StringIO()
    job = CheckJob(units=[probe])
    serve([job.model_dump_json() + "\n"], out, engine=kit.engine, targeter=kit.targeter)
    [done] = [json.loads(line) for line in out.getvalue().splitlines() if '"done"' in line]
    return str(done["result"]["outcome"])


def test_the_probe_binds_previous_changes_and_refs_at_scale(kit: Kit) -> None:
    assert probe_outcome(kit, "previous.schema.size() == 100") == "pass"
    assert probe_outcome(kit, "changes.size() == 200") == "pass"
    assert probe_outcome(kit, "changes.filter(c, c['class'] == 'breaking').size() == 100") == "pass"
    for name in ("elements", "servers", "contracts", "principals", "teams"):
        assert probe_outcome(kit, f"refs.{name}.size() == 100") == "pass"


def test_a_cel1_probe_binds_nothing_extra(kit: Kit) -> None:
    rule = kit.rule("true", target="$")
    content = RuleSetContent.model_validate({"dsl_version": "cel-1", "rules": [rule]})
    (probe,) = [u for u in check_units(content) if u.kind == "probe"]
    assert probe.dsl_version == "cel-1"
    assert probe.previous is None


def test_a_guarded_cel2_rule_scanning_previous_passes_the_probe_but_a_cubic_one_is_refused(
    kit: Kit,
) -> None:
    guarded = "previous == null || previous.schema.size() >= 0"
    rule = kit.rule(guarded, target="$")
    rule["fixtures"]["fail"] = [{"document": {}, "previous": {}}]
    report = kit.check(kit.content(rule), changes_for=lambda previous, document: [])
    assert "scale_probe_failed" not in [p.code for p in report.rules[0].problems]
    heavy = "previous.schema.all(s, s.properties.all(p, previous.schema.all(t, true)))"
    assert "scale_probe_failed" in kit.codes(kit.check(kit.content(kit.rule(heavy, target="$"))))
