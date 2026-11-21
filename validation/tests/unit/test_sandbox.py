"""The rule sandbox: bounded worker processes (plan 05-03, D-137, D-138, D-139).

Real worker subprocesses are used for the tracer cases. Deadline, budget and back-pressure cases
use `fake_worker.py`, a stand-alone script that speaks the same protocol with no engine import.
"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from dc_rules.checks import assemble_check, check_units, static_problems
from dc_rules.model import RuleDef, RuleSetContent
from dc_rules.ports import Limits
from dc_rules.protocol import EvalUnit
from dc_validation import sandbox as sandbox_module
from dc_validation.sandbox import Sandbox, SandboxBusy, SandboxUnavailable
from dc_validation.settings import ValidationSettings

pytestmark = pytest.mark.unit

FAKE_WORKER = [sys.executable, str(Path(__file__).with_name("fake_worker.py"))]
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


def make_settings(**overrides: Any) -> ValidationSettings:
    return ValidationSettings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
        **overrides,
    )


def table_doc(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


async def test_a_rule_is_evaluated_in_a_worker_subprocess_and_the_result_comes_back() -> None:
    sandbox = Sandbox(make_settings())
    try:
        unit = EvalUnit(id=0, rule=make_rule(UNIQUE_NAMES, target="$.schema[*]"))
        results = await sandbox.evaluate([unit], table_doc("id", "id"))
    finally:
        await sandbox.aclose()
    result = results[0]
    assert result.outcome == "fail"
    assert [f.pointer for f in result.findings] == ["/schema/0"]
    assert sandbox.engine_version is not None
    assert "cel-expr-python" in sandbox.engine_version


def test_building_the_validation_app_never_loads_the_cel_engine() -> None:
    code = (
        "import sys\n"
        "from dc_validation.main import create_validation_app\n"
        "from dc_validation.settings import ValidationSettings\n"
        "create_validation_app(ValidationSettings("
        "database_url='postgresql+psycopg://nobody:nobody@127.0.0.1:1/never'))\n"
        "loaded = sorted(m for m in sys.modules if m == 'cel_expr_python'"
        " or m.startswith(('cel_expr_python.', 'dc_rules.cel_engine', 'dc_rules.worker')))\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run(  # noqa: S603 (fixed interpreter and code)
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr


@asynccontextmanager
async def sandbox_of(*, fake: bool = True, **overrides: Any) -> AsyncIterator[Sandbox]:
    sandbox = Sandbox(make_settings(**overrides), worker_argv=FAKE_WORKER if fake else None)
    try:
        yield sandbox
    finally:
        await sandbox.aclose()


def unit(unit_id: int, key: str = "ok") -> EvalUnit:
    return EvalUnit(id=unit_id, rule=make_rule("true", key=key))


# --- deadlines, kill and respawn -----------------------------------------------------------


async def test_a_hung_unit_is_marked_timeout_and_the_next_unit_runs_in_a_replacement() -> None:
    async with sandbox_of(rule_timeout_ms=200) as sandbox:
        results = await sandbox.evaluate([unit(0), unit(1, "hang"), unit(2)], {})
    assert results[1].outcome == "error"
    assert results[1].error_code == "timeout"
    assert results[0].outcome == "pass"
    assert results[2].outcome == "pass"
    assert results[0].detail != results[2].detail  # a different process answered unit 2


async def test_a_worker_that_dies_gives_engine_error_and_is_replaced() -> None:
    async with sandbox_of(rule_timeout_ms=2000) as sandbox:
        results = await sandbox.evaluate([unit(0, "die"), unit(1)], {})
    assert results[0].error_code == "engine_error"
    assert results[1].outcome == "pass"


async def test_an_error_result_carries_the_code_and_one_root_finding() -> None:
    async with sandbox_of(rule_timeout_ms=200) as sandbox:
        results = await sandbox.evaluate([unit(0, "hang")], {})
    result = results[0]
    assert result.error_code == "timeout"
    assert [(f.outcome, f.pointer, f.error_code) for f in result.findings] == [
        ("error", "", "timeout")
    ]


async def test_an_exhausted_run_budget_marks_every_unevaluated_unit_run_budget_exceeded() -> None:
    async with sandbox_of(run_budget_s=0.5, rule_timeout_ms=400) as sandbox:
        results = await sandbox.evaluate([unit(0, "hang"), unit(1, "hang"), unit(2, "hang")], {})
    assert results[0].error_code == "timeout"
    assert results[1].error_code == "run_budget_exceeded"
    assert results[2].error_code == "run_budget_exceeded"
    assert set(results) == {0, 1, 2}  # no unit is ever skipped


LINUX_PROC = sys.platform == "linux"


@pytest.mark.skipif(not LINUX_PROC, reason="the CPU clock reads /proc/<pid>/stat")
async def test_a_slow_but_idle_worker_is_not_timed_out_it_is_waited_for_wr01() -> None:
    # The worker answers after 0.45 s of sleeping (no CPU): host load, not a runaway rule. The
    # 100 ms rule timeout must not turn that into `timeout`.
    async with sandbox_of(rule_timeout_ms=100) as sandbox:
        results = await sandbox.evaluate([unit(0, "slow"), unit(1)], {})
    assert results[0].outcome == "pass"
    assert results[1].outcome == "pass"


@pytest.mark.skipif(not LINUX_PROC, reason="the CPU clock reads /proc/<pid>/stat")
async def test_a_wedged_idle_worker_still_dies_at_the_hard_wall_cap_wr01() -> None:
    async with sandbox_of(rule_timeout_ms=50, run_budget_s=30) as sandbox:
        started = asyncio.get_running_loop().time()
        results = await sandbox.evaluate([unit(0, "stall"), unit(1)], {})
        elapsed = asyncio.get_running_loop().time() - started
    assert results[0].error_code == "timeout"
    assert results[1].outcome == "pass"
    assert 0.3 < elapsed < 5  # about 8 x 50 ms, never unbounded


async def test_a_cpu_loop_is_killed_at_about_its_cpu_allowance_wr01() -> None:
    async with sandbox_of(rule_timeout_ms=300) as sandbox:
        started = asyncio.get_running_loop().time()
        results = await sandbox.evaluate([unit(0, "hang")], {})
        elapsed = asyncio.get_running_loop().time() - started
    assert results[0].error_code == "timeout"
    assert elapsed < 3


# --- a failure before any unit started is not a verdict (WR-02) -----------------------------


async def test_a_worker_that_dies_before_any_start_marker_is_retried_once_wr02(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_WORKER_FLAG", str(tmp_path / "died-once"))
    async with sandbox_of(rule_timeout_ms=2000) as sandbox:
        results = await sandbox.evaluate([unit(0, "doa"), unit(1)], {})
    assert results[0].outcome == "pass"  # rule 1 never ran in the dead worker: no verdict on it
    assert results[1].outcome == "pass"


async def test_a_second_failure_before_any_start_marker_is_blamed_on_the_first_unit_wr02() -> None:
    async with sandbox_of(rule_timeout_ms=2000) as sandbox:
        results = await sandbox.evaluate([unit(0, "doa-always"), unit(1, "doa-always")], {})
    assert results[0].error_code == "engine_error"
    assert set(results) == {0, 1}  # nothing is skipped


# --- back-pressure --------------------------------------------------------------------------


async def test_a_saturated_sandbox_refuses_with_busy_and_recovers_when_the_slot_frees() -> None:
    async with sandbox_of(max_concurrent_runs=1, queue_wait_s=0.2) as sandbox:
        async with sandbox.slot():
            with pytest.raises(SandboxBusy):
                await sandbox.evaluate([unit(0)], {})
        results = await sandbox.evaluate([unit(0)], {})
    assert results[0].outcome == "pass"


async def test_a_cancelled_waiter_never_leaks_a_slot() -> None:
    async with sandbox_of(max_concurrent_runs=1, queue_wait_s=30) as sandbox:
        async with sandbox.slot():
            waiter = asyncio.create_task(sandbox.evaluate([unit(0)], {}))
            await asyncio.sleep(0.05)  # the waiter is now queued in slot()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        results = await asyncio.wait_for(sandbox.evaluate([unit(0)], {}), timeout=5)
    assert results[0].outcome == "pass"


async def test_a_run_cancelled_mid_evaluation_frees_its_slot_and_worker() -> None:
    async with sandbox_of(
        max_concurrent_runs=1, eval_workers=1, queue_wait_s=30, rule_timeout_ms=30_000
    ) as sandbox:
        run = asyncio.create_task(sandbox.evaluate([unit(0, "hang")], {}))
        await asyncio.sleep(0.3)  # the worker has printed its start marker and is stuck
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        results = await asyncio.wait_for(sandbox.evaluate([unit(0)], {}), timeout=10)
    assert results[0].outcome == "pass"


async def test_the_wait_for_a_worker_is_bounded_by_queue_wait_wr03() -> None:
    async with sandbox_of(
        max_concurrent_runs=2,
        eval_workers=1,
        queue_wait_s=0.3,
        rule_timeout_ms=10_000,
        run_budget_s=10,
    ) as sandbox:
        busy = asyncio.create_task(sandbox.evaluate([unit(0, "hang")], {}))
        await asyncio.sleep(0.3)  # the only worker is spinning inside a run
        started = asyncio.get_running_loop().time()
        with pytest.raises(SandboxBusy):
            await sandbox.evaluate([unit(0)], {})
        assert asyncio.get_running_loop().time() - started < 2  # not "as long as the job takes"
        busy.cancel()
        with pytest.raises(asyncio.CancelledError):
            await busy
        results = await asyncio.wait_for(sandbox.evaluate([unit(0)], {}), timeout=10)
    assert results[0].outcome == "pass"


# --- lifecycle ------------------------------------------------------------------------------


async def test_workers_spawn_lazily_and_are_recycled_after_worker_max_jobs() -> None:
    async with sandbox_of(worker_max_jobs=1, eval_workers=1) as sandbox:
        assert sandbox.engine_version is None  # nothing spawned yet
        first = await sandbox.evaluate([unit(0)], {})
        assert sandbox.engine_version == "fake-engine"
        second = await sandbox.evaluate([unit(0)], {})
    assert first[0].detail != second[0].detail


async def test_an_idle_worker_is_reused_until_it_is_recycled() -> None:
    async with sandbox_of(worker_max_jobs=100, eval_workers=1) as sandbox:
        first = await sandbox.evaluate([unit(0)], {})
        second = await sandbox.evaluate([unit(0)], {})
    assert first[0].detail == second[0].detail


async def test_a_worker_that_never_becomes_ready_is_sandbox_unavailable() -> None:
    sandbox = Sandbox(make_settings(), worker_argv=[sys.executable, "-c", "pass"])
    try:
        with pytest.raises(SandboxUnavailable):
            await sandbox.evaluate([unit(0)], {})
    finally:
        await sandbox.aclose()


async def test_the_ready_wait_is_a_setting_in04() -> None:
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    sandbox = Sandbox(make_settings(worker_ready_timeout_s=0.3), worker_argv=slow)
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(SandboxUnavailable):
            await sandbox.evaluate([unit(0)], {})
        assert asyncio.get_running_loop().time() - started < 3
    finally:
        await sandbox.aclose()


async def test_an_empty_unit_list_needs_no_worker() -> None:
    async with sandbox_of() as sandbox:
        assert await sandbox.evaluate([], {}) == {}
        assert sandbox.engine_version is None


async def test_duplicate_unit_ids_are_rejected() -> None:
    async with sandbox_of() as sandbox:
        with pytest.raises(ValueError, match="unique"):
            await sandbox.evaluate([unit(0), unit(0)], {})


# --- the real worker ------------------------------------------------------------------------


async def test_a_huge_integer_in_the_document_reaches_the_worker_and_is_normalised() -> None:
    async with sandbox_of(fake=False) as sandbox:
        rule = make_rule("this.n > 0")
        results = await sandbox.evaluate([EvalUnit(id=0, rule=rule)], {"n": 10**30})
    assert results[0].outcome == "pass"


async def test_an_iteration_budget_error_comes_back_as_a_coded_error_not_a_crash() -> None:
    async with sandbox_of(fake=False, rule_timeout_ms=5000) as sandbox:
        hostile = make_rule("this.l.all(a, this.l.all(b, true))")
        results = await sandbox.evaluate(
            [EvalUnit(id=0, rule=hostile), EvalUnit(id=1, rule=make_rule("true"))],
            {"l": list(range(200))},
        )
    assert results[0].outcome == "error"
    assert results[0].findings[0].error_code == "budget_exceeded"
    assert results[1].outcome == "pass"


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced on Linux only")
async def test_a_runaway_string_expression_is_a_memory_error_and_the_worker_survives() -> None:
    doubling = "['aaaaaaaaaaaaaaaa']" + ".map(s, s+s)" * 28 + "[0].size() > 0"
    async with sandbox_of(fake=False, rule_timeout_ms=5000, eval_workers=1) as sandbox:
        results = await sandbox.evaluate(
            [EvalUnit(id=0, rule=make_rule(doubling)), EvalUnit(id=1, rule=make_rule("true"))],
            {},
        )
    assert results[0].outcome == "error"
    assert results[0].findings[0].error_code == "memory_exceeded"
    assert results[1].outcome == "pass"


# --- the rule-set check (plan 05-04) --------------------------------------------------------


def checked_rule(condition: str, key: str = "unique-names") -> dict[str, Any]:
    return {
        "key": key,
        "target": "$.schema[*]",
        "condition": condition,
        "severity": "error",
        "message": "Rule under test.",
        "fixtures": {
            "pass": [{"document": table_doc("id", "name")}],
            "fail": [{"document": table_doc("id", "id")}],
        },
    }


async def run_set_check(sandbox: Sandbox, *rules: dict[str, Any]) -> Any:
    content = RuleSetContent.model_validate({"rules": list(rules)})
    limits = Limits()
    results = await sandbox.check(check_units(content, limits))
    return assemble_check(content, static_problems(content, limits), results, limits)


async def test_a_good_rule_set_checks_ok_through_the_sandbox() -> None:
    async with sandbox_of(fake=False) as sandbox:
        report = await run_set_check(sandbox, checked_rule(UNIQUE_NAMES))
    assert report.ok
    assert [r.ok for r in report.rules] == [True]


@pytest.mark.timing
@pytest.mark.performance  # a scale budget: only with DC_PERFORMANCE=1 (dev hardware cannot hold it)
async def test_a_full_200_rule_set_checks_ok_within_the_default_check_budget() -> None:
    # Plan 05-06 raised the scale probe to 100 tables x 100 columns: the heaviest legal rule set
    # (200 ordinary rules, each with its own probe unit) must still finish inside check_budget_s.
    rules = [checked_rule(UNIQUE_NAMES, key=f"rule-{i}") for i in range(200)]
    async with sandbox_of(fake=False, eval_workers=1) as sandbox:
        report = await run_set_check(sandbox, *rules)
    assert report.ok, [p for r in report.rules for p in r.problems][:3]
    assert len(report.rules) == 200


async def test_an_always_true_rules_fail_fixture_is_named_through_the_sandbox() -> None:
    async with sandbox_of(fake=False) as sandbox:
        report = await run_set_check(sandbox, checked_rule("true"))
    assert not report.ok
    [problem] = report.rules[0].problems
    assert problem.code == "fixture_misbehaved"
    assert (problem.fixture_kind, problem.fixture_index) == ("fail", 0)
    assert "pass" in (problem.detail or "")


DOUBLING = "['aaaaaaaaaaaaaaaa']" + ".map(s, s+s)" * 28 + "[0].size() > 0"
EXPECTED_STOP = "memory_exceeded" if sys.platform == "linux" else "timeout"


async def test_a_memory_bomb_rule_is_refused_by_the_check_and_the_worker_is_replaced() -> None:
    # 370 bytes, BOOL type: it passes every static limit and only fails when evaluated.
    async with sandbox_of(fake=False, eval_workers=1) as sandbox:
        report = await run_set_check(sandbox, checked_rule(DOUBLING, "bomb"))
        # the same sandbox still checks a good rule afterwards
        after = await run_set_check(sandbox, checked_rule(UNIQUE_NAMES))
    assert not report.ok
    assert [p.code for p in report.rules[0].problems] == [
        "fixture_misbehaved",
        "fixture_misbehaved",
        "scale_probe_failed",
    ]
    *fixtures, probe = report.rules[0].problems
    assert {p.error_code for p in fixtures} == {EXPECTED_STOP}
    # The probe runs the bomb on 100 tables in one unit, so on Linux the 500 ms deadline can
    # end it before the 50th MemoryError; either stop is a scale error.
    assert probe.error_code in {EXPECTED_STOP, "timeout"}
    assert after.ok


async def test_a_cheap_run_on_another_worker_finishes_while_a_hostile_unit_runs() -> None:
    async with sandbox_of(fake=False, eval_workers=2, max_concurrent_runs=2) as sandbox:
        hostile = asyncio.create_task(
            sandbox.evaluate([EvalUnit(id=0, rule=make_rule(DOUBLING))], {"x": 1})
        )
        await asyncio.sleep(0.1)
        started = asyncio.get_running_loop().time()
        cheap = await sandbox.evaluate([EvalUnit(id=0, rule=make_rule("true"))], {})
        elapsed = asyncio.get_running_loop().time() - started
        stopped = await hostile
    assert cheap[0].outcome == "pass"
    assert elapsed < 1.0
    assert stopped[0].outcome == "error"
    assert {f.error_code for f in stopped[0].findings} == {EXPECTED_STOP}


# --- cel-2 job bindings (plan 06-05, D-152, Pitfall 7) --------------------------------------


async def _captured_job(units: list[EvalUnit], **bindings: Any) -> Any:
    """The `EvaluateJob` the sandbox would send, without starting a worker."""
    sandbox = Sandbox(make_settings(), worker_argv=FAKE_WORKER)
    seen: list[Any] = []

    async def fake_execute(units: Any, make_job: Any, **_: Any) -> dict[int, Any]:
        seen.append(make_job(list(units)))
        return {}

    sandbox._execute = fake_execute  # type: ignore[method-assign,assignment]
    try:
        await sandbox.evaluate(units, {"k": 1}, {}, **bindings)
    finally:
        await sandbox.aclose()
    return seen[0]


async def test_a_cel1_only_job_carries_no_cel2_bindings() -> None:
    job = await _captured_job(
        [unit(0)], previous={"big": "baseline"}, changes=[{"class": "breaking"}], refs={"x": {}}
    )
    assert job.previous is None
    assert job.changes == []
    assert job.refs == {}


async def test_a_job_with_a_cel2_unit_carries_the_supplied_bindings() -> None:
    cel2 = EvalUnit(id=1, rule=make_rule("true", key="cel2"), dsl_version="cel-2")
    job = await _captured_job(
        [unit(0), cel2],
        previous={"big": "baseline"},
        changes=[{"class": "breaking"}],
        refs={"x": {}},
    )
    assert job.previous == {"big": "baseline"}
    assert job.changes == [{"class": "breaking"}]
    assert job.refs == {"x": {}}
    assert [u.dsl_version for u in job.units] == ["cel-1", "cel-2"]


# --- protocol violations and the end of a job ------------------------------------------------


@pytest.mark.parametrize("key", ["bad-start", "bad-done", "bad-marker", "garbage", "huge-line"])
async def test_a_protocol_violation_is_engine_error_and_a_replacement_runs_the_rest(
    key: str,
) -> None:
    async with sandbox_of(rule_timeout_ms=5000) as sandbox:
        results = await sandbox.evaluate([unit(0, key), unit(1)], {})
    assert results[0].error_code == "engine_error"
    assert results[1].outcome == "pass"


async def test_a_job_ended_early_gives_every_unanswered_unit_engine_error() -> None:
    async with sandbox_of(rule_timeout_ms=5000) as sandbox:
        results = await sandbox.evaluate([unit(0, "end-early"), unit(1)], {})
    assert results[0].error_code == "engine_error"
    assert results[1].error_code == "engine_error"


async def test_a_worker_that_dies_after_its_last_answer_blames_no_unit() -> None:
    async with sandbox_of(rule_timeout_ms=5000) as sandbox:
        results = await sandbox.evaluate([unit(0), unit(1, "die-after-done")], {})
    assert [results[0].outcome, results[1].outcome] == ["pass", "pass"]


async def test_a_worker_that_closed_its_stdin_is_a_broken_pipe_and_engine_error() -> None:
    sandbox = Sandbox(make_settings(), worker_argv=[*FAKE_WORKER, "--close-stdin"])
    try:
        results = await sandbox.evaluate([unit(0)], {})
    finally:
        await sandbox.aclose()
    assert results[0].error_code == "engine_error"


async def test_a_run_budget_shorter_than_the_rule_timeout_ends_a_silent_unit() -> None:
    async with sandbox_of(rule_timeout_ms=10_000, run_budget_s=0.5) as sandbox:
        started = asyncio.get_running_loop().time()
        results = await sandbox.evaluate([unit(0, "stall")], {})
        elapsed = asyncio.get_running_loop().time() - started
    assert results[0].error_code == "run_budget_exceeded"
    assert elapsed < 5  # the budget, not the 10 s rule timeout, ended the wait


async def test_a_replacement_worker_that_arrives_after_the_budget_charges_the_budget() -> None:
    async with sandbox_of(rule_timeout_ms=5000, run_budget_s=0.3) as sandbox:
        spawn = sandbox._spawn

        async def slow_spawn() -> Any:
            worker = await spawn()
            await asyncio.sleep(0.5)  # longer than the whole run budget
            return worker

        sandbox._spawn = slow_spawn  # type: ignore[method-assign]
        # The first worker exits before any marker (no verdict); its replacement is ready only
        # after the budget is spent, so nothing is evaluated and nothing is skipped.
        results = await sandbox.evaluate([unit(0, "doa-always"), unit(1)], {})
    assert results[0].error_code == "run_budget_exceeded"
    assert results[1].error_code == "run_budget_exceeded"


# --- the CPU clock (WR-01) on every platform -------------------------------------------------


def test_the_cpu_clock_reads_user_time_after_the_last_parenthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # comm "(odd) name)" holds spaces and parentheses; utime (field 14) is 250 ticks.
    stat = b"4242 ((odd) name)) S 1 1 1 0 -1 4194304 10 0 0 0 250 7 0 0 20 0 1 0"
    monkeypatch.setattr(sandbox_module, "open", lambda *_a, **_k: io.BytesIO(stat), raising=False)
    assert sandbox_module._cpu_seconds(4242) == 250 / os.sysconf("SC_CLK_TCK")


def test_a_truncated_stat_line_is_an_unknown_cpu_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_module, "open", lambda *_a, **_k: io.BytesIO(b"4242 (w) S 1 2"), raising=False
    )
    assert sandbox_module._cpu_seconds(4242) is None


def stub_cpu_clock(monkeypatch: pytest.MonkeyPatch, *readings: float | None) -> None:
    """``_cpu_seconds`` answers ``readings`` in order, then repeats the last one."""
    queue = list(readings)

    def fake(_pid: int) -> float | None:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(sandbox_module, "_cpu_seconds", fake)


async def test_a_worker_that_used_little_cpu_keeps_the_rest_of_its_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_cpu_clock(monkeypatch, 1.25)
    wall_mark = asyncio.get_running_loop().time()
    left = Sandbox._cpu_allowance_left(1, 1.0, wall_mark, 1.0)
    assert left is not None
    assert 0.7 < left <= 0.75


async def test_a_worker_that_used_its_allowance_or_hit_the_wall_cap_is_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = asyncio.get_running_loop().time()
    stub_cpu_clock(monkeypatch, 2.0)
    assert Sandbox._cpu_allowance_left(1, 1.0, now, 1.0) is None  # a full second of CPU used
    stub_cpu_clock(monkeypatch, 1.0)
    assert Sandbox._cpu_allowance_left(1, 1.0, now - 9.0, 1.0) is None  # idle past 8 x allowance


async def test_an_unknown_cpu_time_falls_back_to_the_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = asyncio.get_running_loop().time()
    stub_cpu_clock(monkeypatch, None)
    assert Sandbox._cpu_allowance_left(1, 1.0, now, 1.0) is None  # the clock vanished
    assert Sandbox._cpu_allowance_left(1, None, now, 1.0) is None  # never known


async def test_with_an_idle_cpu_clock_a_slow_unit_is_waited_for_on_any_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_cpu_clock(monkeypatch, 0.0)  # the worker never computes: host load, not a runaway
    async with sandbox_of(rule_timeout_ms=100) as sandbox:
        results = await sandbox.evaluate([unit(0, "slow"), unit(1)], {})
    assert [results[0].outcome, results[1].outcome] == ["pass", "pass"]


# --- worker lifecycle edges ------------------------------------------------------------------


async def test_a_closed_sandbox_refuses_to_evaluate() -> None:
    async with sandbox_of() as sandbox:
        await sandbox.aclose()
        with pytest.raises(SandboxUnavailable, match="closed"):
            await sandbox.evaluate([unit(0)], {})


async def test_an_idle_worker_that_exited_is_dropped_and_a_new_one_answers() -> None:
    async with sandbox_of(eval_workers=1, worker_max_jobs=100) as sandbox:
        first = await sandbox.evaluate([unit(0)], {})
        [idle] = list(sandbox._idle)
        idle.proc.kill()
        await idle.proc.wait()
        second = await sandbox.evaluate([unit(0)], {})
        assert idle not in sandbox._all
    assert second[0].outcome == "pass"
    assert first[0].detail != second[0].detail


async def test_a_worker_binary_that_cannot_start_is_unavailable_and_frees_its_permit(
    tmp_path: Path,
) -> None:
    missing = [str(tmp_path / "no-such-worker")]
    sandbox = Sandbox(make_settings(eval_workers=1, queue_wait_s=0.2), worker_argv=missing)
    try:
        for _ in range(2):  # a leaked permit would make the second call SandboxBusy
            with pytest.raises(SandboxUnavailable, match="could not be started"):
                await sandbox.evaluate([unit(0)], {})
    finally:
        await sandbox.aclose()


async def test_a_run_cancelled_while_its_worker_starts_kills_that_worker() -> None:
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    sandbox = Sandbox(make_settings(worker_ready_timeout_s=30), worker_argv=slow)
    try:
        run = asyncio.create_task(sandbox.evaluate([unit(0)], {}))
        await asyncio.sleep(0.3)  # the worker is spawned and the parent waits for its ready line
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert sandbox._all == set()
    finally:
        await sandbox.aclose()
