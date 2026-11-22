"""The worker protocol and loop (plan 05-03, D-118, D-137, D-139).

`test_the_worker_process_*` drives the real `python -m dc_rules.worker` over pipes; the rest drive
`serve` in-process with string streams so engine behaviour (compile counts, MemoryError) is
deterministic.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

import pytest
from dc_rules import worker as worker_module
from dc_rules.cel_engine import CelEngine, engine_version
from dc_rules.evaluator import evaluate_rule
from dc_rules.model import RuleDef
from dc_rules.ports import ExpressionEngine, Limits, Targeter
from dc_rules.protocol import (
    CheckJob,
    CheckUnit,
    EvaluateJob,
    EvalUnit,
    LimitsWire,
    UnitResult,
    WorkerReady,
)
from dc_rules.targets import JsonPathTargeter
from dc_rules.worker import CACHE_MAX_ENTRIES, CompileCache, serve

pytestmark = pytest.mark.unit

UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"


def make_rule(condition: str, key: str = "rule-under-test", target: str = "$") -> RuleDef:
    return RuleDef.model_validate(
        {
            "key": key,
            "target": target,
            "condition": condition,
            "severity": "error",
            "message": "Rule under test.",
            "fixtures": {"pass": [{"document": {}}], "fail": [{"document": {}}]},
        }
    )


def doc(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


class CountingEngine:
    """Wraps a real engine and counts compiles; optional hook to raise from `compile`."""

    def __init__(self, inner: ExpressionEngine, boom: BaseException | None = None) -> None:
        self._inner = inner
        self._boom = boom
        self.compiles = 0
        self.name = inner.name
        self.version = inner.version

    def compile(self, expression: str, dsl_version: str = "cel-1") -> Any:
        self.compiles += 1
        if self._boom is not None:
            raise self._boom
        return self._inner.compile(expression, dsl_version)

    def evaluate(self, compiled: Any, bindings: Mapping[str, Any]) -> Any:
        return self._inner.evaluate(compiled, bindings)

    def compiled_size(self, compiled: Any) -> int:
        return self._inner.compiled_size(compiled)


def run_serve(
    job_lines: list[str], engine: ExpressionEngine, targeter: Targeter, **kwargs: Any
) -> list[dict[str, Any]]:
    out = io.StringIO()
    serve(job_lines, out, engine=engine, targeter=targeter, **kwargs)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def check_line(units: list[CheckUnit]) -> str:
    return json.dumps(CheckJob(units=units).model_dump(mode="json")) + "\n"


def evaluate_line(units: list[EvalUnit], document: Any) -> str:
    return json.dumps(EvaluateJob(units=units, document=document).model_dump(mode="json")) + "\n"


# --- the worker process over pipes ----------------------------------------------------------


def test_the_worker_process_announces_itself_and_answers_a_job_with_markers() -> None:
    proc = subprocess.Popen(  # noqa: S603 (fixed interpreter and module)
        [sys.executable, "-m", "dc_rules.worker", "--memory-limit-bytes", str(2**30)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        ready = WorkerReady.model_validate_json(proc.stdout.readline())
        assert ready.engine_version == engine_version()
        assert ready.dsl_version == "cel-2"

        units = [
            EvalUnit(id=0, rule=make_rule(UNIQUE_NAMES, target="$.schema[*]")),
            EvalUnit(id=1, rule=make_rule("this.schema.size() == 1")),
        ]
        proc.stdin.write(evaluate_line(units, doc("id", "id")))
        proc.stdin.flush()
        lines = [json.loads(proc.stdout.readline()) for _ in range(5)]
    finally:
        proc.kill()
        proc.wait()

    assert [list(m) for m in lines] == [
        ["start"],
        ["done", "result"],
        ["start"],
        ["done", "result"],
        ["end"],
    ]
    assert [lines[0]["start"], lines[1]["done"], lines[2]["start"], lines[3]["done"]] == [
        0,
        0,
        1,
        1,
    ]
    assert lines[4] == {"end": True}

    engine, targeter = CelEngine(), JsonPathTargeter()
    for unit, message in ((units[0], lines[1]), (units[1], lines[3])):
        expected = evaluate_rule(
            unit.rule,
            engine.compile(unit.rule.condition),
            targeter.compile(unit.rule.target),
            doc("id", "id"),
            {},
            engine=engine,
            targeter=targeter,
            limits=Limits(),
        )
        got = UnitResult.model_validate(message["result"])
        assert got.outcome == expected.outcome
        assert got.matched == expected.matched
        assert [(f.pointer, f.path, f.error_code) for f in got.findings] == [
            (f.location.pointer, f.location.path, f.error_code) for f in expected.findings
        ]


def test_the_worker_exits_cleanly_when_its_input_ends() -> None:
    done = subprocess.run(  # noqa: S603 (fixed interpreter and module)
        [sys.executable, "-m", "dc_rules.worker"],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0
    assert json.loads(done.stdout.splitlines()[0])["ready"] is True


# --- serve, in process ----------------------------------------------------------------------


def test_serve_emits_start_done_markers_in_order_and_ends_the_job() -> None:
    units = [EvalUnit(id=7, rule=make_rule("true")), EvalUnit(id=9, rule=make_rule("false"))]
    messages = run_serve([evaluate_line(units, {})], CelEngine(), JsonPathTargeter())
    assert [list(m) for m in messages] == [
        ["start"],
        ["done", "result"],
        ["start"],
        ["done", "result"],
        ["end"],
    ]
    assert [m["result"]["outcome"] for m in messages if "done" in m] == ["pass", "fail"]


def test_a_published_rule_compiles_once_per_cache_key() -> None:
    engine = CountingEngine(CelEngine())
    unit = EvalUnit(id=0, cache_key="version-1:rule-a", rule=make_rule("true", key="rule-a"))
    run_serve(
        [evaluate_line([unit], {}), evaluate_line([unit, unit.model_copy(update={"id": 1})], {})],
        engine,
        JsonPathTargeter(),
    )
    assert engine.compiles == 1


def test_a_draft_unit_without_a_cache_key_is_compiled_every_time() -> None:
    engine = CountingEngine(CelEngine())
    unit = EvalUnit(id=0, rule=make_rule("true"))
    run_serve([evaluate_line([unit], {}), evaluate_line([unit], {})], engine, JsonPathTargeter())
    assert engine.compiles == 2


def test_the_compile_cache_evicts_the_oldest_entry_and_a_miss_compiles_again() -> None:
    assert CACHE_MAX_ENTRIES == 1024
    cache = CompileCache(max_entries=2)
    engine = CountingEngine(CelEngine())

    def unit(n: int) -> EvalUnit:
        return EvalUnit(id=0, cache_key=f"v:{n}", rule=make_rule("true", key=f"rule-{n}"))

    lines = [evaluate_line([unit(n)], {}) for n in (1, 2, 3, 1)]
    run_serve(lines, engine, JsonPathTargeter(), cache=cache)
    assert len(cache) == 2
    assert engine.compiles == 4  # entry 1 was evicted by 3, so the fourth job compiled it again


def test_a_memory_error_becomes_memory_exceeded_and_the_next_unit_still_runs() -> None:
    engine = CountingEngine(CelEngine(), boom=MemoryError("std::bad_alloc secret"))
    first = EvalUnit(id=0, rule=make_rule("true"))
    messages = run_serve([evaluate_line([first], {})], engine, JsonPathTargeter())
    result = messages[1]["result"]
    assert result["outcome"] == "error"
    assert result["error_code"] == "memory_exceeded"
    assert "secret" not in json.dumps(messages)
    assert messages[-1] == {"end": True}


def test_any_other_exception_becomes_engine_error_without_its_text() -> None:
    engine = CountingEngine(CelEngine(), boom=RuntimeError("leaks xq-leak-probe from the document"))
    messages = run_serve(
        [evaluate_line([EvalUnit(id=0, rule=make_rule("true"))], {})], engine, JsonPathTargeter()
    )
    result = messages[1]["result"]
    assert result["outcome"] == "error"
    assert result["error_code"] == "engine_error"
    assert "xq-leak-probe" not in json.dumps(messages)


def test_a_published_rule_that_no_longer_compiles_is_an_engine_error_not_an_author_error() -> None:
    unit = EvalUnit(id=0, cache_key="v:1", rule=make_rule("this.a +"))
    messages = run_serve([evaluate_line([unit], {})], CelEngine(), JsonPathTargeter())
    result = messages[1]["result"]
    assert result["outcome"] == "error"
    assert result["error_code"] == "engine_error"
    assert result["problem_code"] == "compile_error"
    assert result["detail"] is None


def test_the_document_is_normalised_once_before_any_unit_runs() -> None:
    units = [EvalUnit(id=i, rule=make_rule("this.n > 0")) for i in range(2)]
    messages = run_serve([evaluate_line(units, {"n": 10**30})], CelEngine(), JsonPathTargeter())
    assert [m["result"]["outcome"] for m in messages if "done" in m] == ["pass", "pass"]


def test_the_check_op_answers_a_compile_unit_with_the_compiled_size() -> None:
    unit = CheckUnit(id=3, rule=make_rule("true"), kind="compile")
    messages = run_serve([check_line([unit])], CelEngine(), JsonPathTargeter())
    result = messages[1]["result"]
    assert (result["id"], result["outcome"]) == (3, "pass")
    assert result["compiled_bytes"] > 0


def test_the_check_op_reports_a_compile_failure_as_an_invalid_unit() -> None:
    unit = CheckUnit(id=0, rule=make_rule("this.a +"), kind="compile")
    result = run_serve([check_line([unit])], CelEngine(), JsonPathTargeter())[1]["result"]
    assert (result["outcome"], result["problem_code"]) == ("invalid", "compile_error")


def test_a_malformed_job_line_ends_the_job_and_the_loop_keeps_serving() -> None:
    good = evaluate_line([EvalUnit(id=0, rule=make_rule("true"))], {})
    messages = run_serve(["not json\n", '{"op": "nope"}\n', good], CelEngine(), JsonPathTargeter())
    assert messages[:2] == [{"end": True}, {"end": True}]
    assert messages[-1] == {"end": True}
    assert messages[3]["result"]["outcome"] == "pass"


def test_before_job_runs_once_per_job() -> None:
    calls: list[int] = []
    unit = EvalUnit(id=0, rule=make_rule("true"))
    run_serve(
        [evaluate_line([unit], {}), evaluate_line([unit], {})],
        CelEngine(),
        JsonPathTargeter(),
        before_job=lambda: calls.append(1),
    )
    assert len(calls) == 2


def test_limits_wire_mirrors_limits_field_for_field() -> None:
    assert set(LimitsWire.model_fields) == set(Limits.__dataclass_fields__)
    assert LimitsWire().to_limits() == Limits()
    custom = Limits(max_matches=5, max_fixture_bytes=7)
    assert LimitsWire.from_limits(custom).to_limits() == custom


# --- ctx.operation (quick 261119-rpi): cel-2 only -------------------------------------------


def ctx_line(units: list[EvalUnit], ctx: dict[str, Any]) -> str:
    job = EvaluateJob(units=units, document={}, ctx=ctx)
    return json.dumps(job.model_dump(mode="json")) + "\n"


def done_outcomes(messages: list[dict[str, Any]]) -> list[str]:
    return [m["result"]["outcome"] for m in messages if "done" in m]


def test_an_evaluate_job_binds_ctx_operation_for_cel2_and_strips_it_for_cel1() -> None:
    cel2 = EvalUnit(id=0, rule=make_rule("ctx.operation == 'publish'"), dsl_version="cel-2")
    cel1 = EvalUnit(id=1, rule=make_rule("has(ctx.operation)"), dsl_version="cel-1")
    cel1_folder = EvalUnit(id=2, rule=make_rule("ctx.folder_kind == 'team'"), dsl_version="cel-1")
    ctx = {"folder_kind": "team", "operation": "publish"}

    messages = run_serve(
        [ctx_line([cel2, cel1, cel1_folder], ctx)], CelEngine(), JsonPathTargeter()
    )

    assert done_outcomes(messages) == ["pass", "fail", "pass"]


def check_fixture_outcome(
    rule: RuleDef, kind: str, fixture_kind: str = "pass", dsl_version: str = "cel-2"
) -> str:
    unit = CheckUnit.model_validate(
        {
            "id": 0,
            "rule": rule,
            "kind": kind,
            "fixture_kind": fixture_kind,
            "fixture_index": 0,
            "dsl_version": dsl_version,
        }
    )
    return done_outcomes(run_serve([check_line([unit])], CelEngine(), JsonPathTargeter()))[0]


def rule_with_fixture_ctx(condition: str, ctx: dict[str, Any] | None) -> RuleDef:
    fixture: dict[str, Any] = {"document": {}}
    if ctx is not None:
        fixture["ctx"] = ctx
    return RuleDef.model_validate(
        {
            "key": "r",
            "condition": condition,
            "severity": "warning",
            "message": "m",
            "fixtures": {"pass": [fixture], "fail": [fixture]},
        }
    )


def test_a_cel2_fixture_without_ctx_sees_operation_save() -> None:
    rule = rule_with_fixture_ctx("ctx.operation == 'save'", None)
    assert check_fixture_outcome(rule, "fixture") == "pass"


def test_a_cel2_fixture_binds_its_own_ctx_operation() -> None:
    rule = rule_with_fixture_ctx("ctx.operation == 'publish'", {"operation": "publish"})
    assert check_fixture_outcome(rule, "fixture") == "pass"


def test_the_cel2_scale_probe_sees_operation_publish() -> None:
    rule = rule_with_fixture_ctx("ctx.operation == 'publish'", None)
    assert check_fixture_outcome(rule, "probe") == "pass"


@pytest.mark.parametrize("kind", ["fixture", "probe"])
def test_a_cel1_fixture_or_probe_never_sees_ctx_operation(kind: str) -> None:
    rule = rule_with_fixture_ctx("!has(ctx.operation)", None)
    assert check_fixture_outcome(rule, kind, dsl_version="cel-1") == "pass"
    assert check_fixture_outcome(rule, kind) == "fail"  # cel-2 binds save or publish


# --- serve: fail-closed edges ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("boom", "code"),
    [
        (MemoryError("std::bad_alloc secret"), "memory_exceeded"),
        (RuntimeError("leaks secret from the draft"), "engine_error"),
    ],
)
def test_a_check_unit_that_raises_is_a_fixed_error_without_its_text(
    boom: BaseException, code: str
) -> None:
    engine = CountingEngine(CelEngine(), boom=boom)
    unit = CheckUnit(id=4, rule=make_rule("true"), kind="compile")
    messages = run_serve([check_line([unit])], engine, JsonPathTargeter())
    result = messages[1]["result"]
    assert (result["id"], result["outcome"], result["error_code"]) == (4, "error", code)
    assert "secret" not in json.dumps(messages)
    assert messages[-1] == {"end": True}


def test_blank_input_lines_are_skipped_without_any_answer() -> None:
    good = evaluate_line([EvalUnit(id=0, rule=make_rule("true"))], {})
    messages = run_serve(["\n", "   \n", good], CelEngine(), JsonPathTargeter())
    assert [list(m) for m in messages] == [["start"], ["done", "result"], ["end"]]


def test_a_job_whose_bindings_cannot_be_normalised_errors_every_unit_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(document: Any) -> Any:
        raise TypeError("unexpected xq-leak-probe in the document")

    monkeypatch.setattr(worker_module, "normalize_document", refuse)
    engine = CountingEngine(CelEngine())
    units = [EvalUnit(id=5, rule=make_rule("true")), EvalUnit(id=6, rule=make_rule("true"))]
    messages = run_serve([evaluate_line(units, {})], engine, JsonPathTargeter())

    assert [list(m) for m in messages] == [
        ["start"],
        ["done", "result"],
        ["start"],
        ["done", "result"],
        ["end"],
    ]
    results = [m["result"] for m in messages if "done" in m]
    assert [(r["id"], r["outcome"], r["error_code"]) for r in results] == [
        (5, "error", "engine_error"),
        (6, "error", "engine_error"),
    ]
    assert engine.compiles == 0  # no unit ran against an unnormalised document
    assert "xq-leak-probe" not in json.dumps(messages)
