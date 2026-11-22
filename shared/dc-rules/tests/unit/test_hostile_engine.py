"""Engine-level hostile rules (plan 05-04, D-141, T-5-05, T-5-09).

Every case is either refused at check time or bounded at evaluation inside a real
``python -m dc_rules.worker`` subprocess that is killed when it overruns, the same way the
validation sandbox does it. Nothing here may hang: each run finishes within the per-rule deadline
plus one second.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import pytest
from dc_rules.model import RuleDef
from dc_rules.protocol import EvaluateJob, EvalUnit

if TYPE_CHECKING:
    from conftest import Kit

pytestmark = pytest.mark.unit

RULE_TIMEOUT_S = 0.5
MEMORY_GUARD_S = 60.0  # a hang guard for the memory case, never the expected duration
MEMORY_JOB = Path(__file__).parent.parent / "fixtures" / "hostile_memory_job.jsonl"
DOUBLING = "['aaaaaaaaaaaaaaaa']" + ".map(s, s+s)" * 28 + "[0].size() > 0"


def make_rule(condition: str, *, target: str = "$", key: str = "hostile") -> RuleDef:
    return RuleDef.model_validate(
        {
            "key": key,
            "target": target,
            "condition": condition,
            "severity": "error",
            "message": "Hostile rule under test.",
            "fixtures": {"pass": [], "fail": []},
        }
    )


class WorkerRun:
    """What one job did: the done result (if any) and how long the wait took."""

    def __init__(self, result: dict[str, Any] | None, elapsed_s: float) -> None:
        self.result = result
        self.elapsed_s = elapsed_s

    @property
    def timed_out(self) -> bool:
        return self.result is None

    @property
    def error_codes(self) -> set[str]:
        assert self.result is not None
        return {f["error_code"] for f in self.result["findings"] if f["outcome"] == "error"}


class _Lines:
    """Lines of a worker's stdout, read by a thread so a wait never misses buffered input."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        assert proc.stdout is not None
        stream = proc.stdout
        threading.Thread(target=self._pump, args=(stream,), daemon=True).start()

    def _pump(self, stream: IO[str]) -> None:
        for line in stream:
            self._queue.put(line)
        self._queue.put(None)  # end of file: the worker is gone

    def get(self, timeout_s: float) -> str | None:
        """The next line, ``None`` at end of file; ``queue.Empty`` when the wait runs out."""
        return self._queue.get(timeout=max(timeout_s, 0))


def run_job_line(
    line: str, *, memory_limit_bytes: int = 1024**3, deadline_s: float = RULE_TIMEOUT_S
) -> WorkerRun:
    """Send one job to a fresh worker; kill it when ``deadline_s`` of wall time passes."""
    proc = subprocess.Popen(  # noqa: S603 (fixed interpreter and module)
        [
            sys.executable,
            "-m",
            "dc_rules.worker",
            "--memory-limit-bytes",
            str(memory_limit_bytes),
            "--cpu-limit-s",
            "20",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdin is not None
        lines = _Lines(proc)
        assert lines.get(10) is not None, "the worker never became ready"
        started = time.monotonic()
        proc.stdin.write(line if line.endswith("\n") else line + "\n")
        proc.stdin.flush()
        result: dict[str, Any] | None = None
        while True:
            try:
                raw = lines.get(deadline_s - (time.monotonic() - started))
            except queue.Empty:  # the deadline passed
                break
            if raw is None:  # the worker died
                break
            message = json.loads(raw)
            if "done" in message:
                result = message["result"]
            if message.get("end"):
                break
        return WorkerRun(result, time.monotonic() - started)
    finally:
        proc.kill()
        proc.wait()


def run_rule(rule: RuleDef, document: Any) -> WorkerRun:
    job = EvaluateJob(units=[EvalUnit(id=0, rule=rule)], document=document)
    return run_job_line(job.model_dump_json(by_alias=True))


# --- bounded at evaluation -----------------------------------------------------------------


def test_a_catastrophic_regex_returns_within_budget_because_cel_uses_re2() -> None:
    run = run_rule(make_rule('this.s.matches("(x+x+)+y")'), {"s": "x" * 1000})
    assert not run.timed_out
    assert run.result is not None and run.result["outcome"] == "fail"
    assert run.elapsed_s < RULE_TIMEOUT_S + 1


def test_a_comprehension_over_10000_elements_is_budget_exceeded() -> None:
    run = run_rule(make_rule("this.l.all(x, x >= 0)"), {"l": list(range(10_000))})
    assert run.error_codes == {"budget_exceeded"}
    assert run.elapsed_s < RULE_TIMEOUT_S + 1


def test_a_map_over_10000_elements_is_bounded_by_the_budget_or_the_deadline() -> None:
    # `map` over 10,000 elements takes about 2 s before the iteration budget trips (measured with
    # cel-expr-python 0.1.4), so the parent's 500 ms deadline is what stops it; either way it is
    # bounded and never answers pass. On a smaller machine (GitHub's runners) the worker's memory
    # cap can trip first; memory_exceeded is the same fail-closed rule error.
    run = run_rule(make_rule("this.l.map(x, x).size() > 0"), {"l": list(range(10_000))})
    assert run.elapsed_s < RULE_TIMEOUT_S + 1
    if not run.timed_out:
        assert run.error_codes in ({"budget_exceeded"}, {"memory_exceeded"})


def test_a_quadratic_comprehension_is_budget_exceeded() -> None:
    run = run_rule(make_rule("this.l.all(a, this.l.all(b, true))"), {"l": list(range(200))})
    assert run.error_codes == {"budget_exceeded"}


@pytest.mark.timing
def test_a_target_matching_100000_nodes_is_too_many_matches() -> None:
    run = run_rule(make_rule("true", target="$.items[*]"), {"items": list(range(100_000))})
    assert run.error_codes == {"too_many_matches"}
    assert run.result is not None and run.result["matched"] <= 10_000
    assert run.elapsed_s < RULE_TIMEOUT_S + 1


def test_the_worker_survives_a_hostile_unit_and_answers_the_next_job() -> None:
    # One worker, two jobs: the budget error must not poison the process.
    proc = subprocess.Popen(  # noqa: S603 (fixed interpreter and module)
        [sys.executable, "-m", "dc_rules.worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdout.readline()
        for rule, document in (
            (make_rule("this.l.map(x, x).size() > 0"), {"l": list(range(10_000))}),
            (make_rule("true"), {}),
        ):
            job = EvaluateJob(units=[EvalUnit(id=0, rule=rule)], document=document)
            proc.stdin.write(job.model_dump_json(by_alias=True) + "\n")
            proc.stdin.flush()
            outcomes = []
            while True:
                message = json.loads(proc.stdout.readline())
                if "done" in message:
                    outcomes.append(message["result"]["outcome"])
                if message.get("end"):
                    break
            assert len(outcomes) == 1
        assert outcomes == ["pass"]
    finally:
        proc.kill()
        proc.wait()


# --- the memory limit (T-5-05) --------------------------------------------------------------


def test_the_memory_job_carries_the_28_chain_the_24_chain_never_reaches_the_limit() -> None:
    line = MEMORY_JOB.read_text()
    assert line.count(".map(s, s+s)") == 28
    job = json.loads(line)
    assert job["op"] == "evaluate"
    assert job["units"][0]["rule"]["condition"] == DOUBLING
    assert len(DOUBLING.encode()) < 2048  # it passes every static limit and reaches evaluation


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced on Linux only")
def test_the_28_chain_is_memory_exceeded_under_the_default_limit_on_linux() -> None:
    # The verdict is "the allocation was refused", which is independent of host speed: the wall
    # deadline here is only a hang guard (WR-01), not a measure of how fast the worker is. The
    # sandbox judges a worker by its CPU time for the same reason.
    run = run_job_line(MEMORY_JOB.read_text(), deadline_s=MEMORY_GUARD_S)
    assert not run.timed_out
    assert run.error_codes == {"memory_exceeded"}


@pytest.mark.skipif(sys.platform == "linux", reason="Linux turns it into memory_exceeded instead")
def test_the_28_chain_outside_linux_is_stopped_by_the_deadline_not_by_a_limit() -> None:
    run = run_job_line(MEMORY_JOB.read_text())
    assert run.timed_out  # no RLIMIT_AS: only the parent's 500 ms kill stops it


# --- refused at check time ------------------------------------------------------------------


def test_an_oversized_expression_is_refused_at_check(kit: Kit) -> None:
    report = kit.check(kit.content(kit.rule("true" + " " * 2045)))
    assert kit.codes(report) == ["expression_too_large"]


def test_a_33_deep_expression_is_a_compile_error(kit: Kit) -> None:
    deep = "(" * 33 + "true" + ")" * 33
    assert len(deep) < 2048
    assert kit.codes(kit.check(kit.content(kit.rule(deep)))) == ["compile_error"]


def test_a_jsonpath_using_search_is_refused_as_target_invalid(kit: Kit) -> None:
    report = kit.check(kit.content(kit.rule(target="$.a[?search(@.n, '(a+)+$')]")))
    assert kit.codes(report) == ["target_invalid"]


def test_an_always_true_rule_cannot_pass_the_check_with_or_without_a_fail_fixture(
    kit: Kit,
) -> None:
    with_fail = kit.check(kit.content(kit.rule("true")))
    assert kit.codes(with_fail) == ["fixture_misbehaved"]
    without = kit.check(kit.content(kit.rule("true", failing=[])))
    assert kit.codes(without) == ["missing_fail_fixture"]
    assert not with_fail.ok and not without.ok
