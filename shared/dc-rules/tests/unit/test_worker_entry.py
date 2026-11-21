"""The worker entry point in process: ``main`` and ``_apply_limits``.

The real ``python -m dc_rules.worker`` subprocess is driven in ``test_worker_protocol.py``; the
coverage of the full gate does not trace subprocesses (they would blow the CPU budgets), so the
entry point is exercised here with patched standard streams and a recording ``resource`` stub.
``_apply_limits`` must never touch the limits of the pytest process itself.
"""

from __future__ import annotations

import io
import json
import resource
import sys
from typing import Any

import pytest
from dc_rules import worker
from dc_rules.cel_engine import engine_version
from dc_rules.model import RuleDef
from dc_rules.protocol import EvaluateJob, EvalUnit, LimitsWire

pytestmark = pytest.mark.unit


class RecordingResource:
    """Stands in for ``resource`` inside ``_apply_limits``: records, never applies."""

    RLIMIT_AS = resource.RLIMIT_AS
    RLIMIT_CORE = resource.RLIMIT_CORE
    RLIMIT_CPU = resource.RLIMIT_CPU
    RLIM_INFINITY = resource.RLIM_INFINITY

    def __init__(self, hard: int, fail: set[int] | None = None) -> None:
        self.hard = hard
        self.fail = fail or set()
        self.calls: list[tuple[int, tuple[int, int]]] = []

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        if which in self.fail:
            raise ValueError("refused")
        self.calls.append((which, limits))

    def getrlimit(self, which: int) -> tuple[int, int]:
        if which in self.fail:
            raise OSError("refused")
        return (self.hard, self.hard)


def _install(monkeypatch: pytest.MonkeyPatch, stub: RecordingResource) -> None:
    monkeypatch.setitem(sys.modules, "resource", stub)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_limits_are_set_and_the_cpu_allowance_is_re_armed_per_job(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    stub = RecordingResource(hard=resource.RLIM_INFINITY)
    _install(monkeypatch, stub)
    monkeypatch.setattr(sys, "platform", platform)
    rearm = worker._apply_limits(memory_limit_bytes=2048, cpu_limit_s=7)
    applied = {which for which, _ in stub.calls}
    assert (resource.RLIMIT_AS in applied) == (platform == "linux")
    assert resource.RLIMIT_CORE in applied
    assert (resource.RLIMIT_CPU, (7, resource.RLIM_INFINITY)) in stub.calls
    before = len(stub.calls)
    rearm()
    which, (soft, hard) = stub.calls[before]
    assert which == resource.RLIMIT_CPU and hard == resource.RLIM_INFINITY
    assert soft >= 7  # process time so far plus a fresh allowance


def test_a_finite_hard_cpu_limit_caps_the_re_armed_soft_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = RecordingResource(hard=3)
    _install(monkeypatch, stub)
    rearm = worker._apply_limits(memory_limit_bytes=2048, cpu_limit_s=60)
    rearm()
    assert stub.calls[-1] == (resource.RLIMIT_CPU, (3, 3))


def test_refused_limits_are_ignored_and_the_rearm_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = RecordingResource(
        hard=resource.RLIM_INFINITY,
        fail={resource.RLIMIT_AS, resource.RLIMIT_CORE, resource.RLIMIT_CPU},
    )
    _install(monkeypatch, stub)
    monkeypatch.setattr(sys, "platform", "linux")
    rearm = worker._apply_limits(memory_limit_bytes=2048, cpu_limit_s=7)
    rearm()
    assert stub.calls == []


def test_a_refused_re_arm_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = RecordingResource(hard=resource.RLIM_INFINITY)
    _install(monkeypatch, stub)
    rearm = worker._apply_limits(memory_limit_bytes=2048, cpu_limit_s=7)
    stub.fail = {resource.RLIMIT_CPU}
    rearm()  # must not raise


class StringStreams:
    """``io.StringIO`` streams: no ``reconfigure``, so ``main`` leaves them as they are."""

    def __init__(self, stdin_text: str) -> None:
        self.stdin: Any = io.StringIO(stdin_text)
        self.stdout: Any = io.StringIO()

    def output(self) -> str:
        return str(self.stdout.getvalue())


class ByteStreams:
    """Real text wrappers over bytes in a non-UTF-8 encoding, as a mis-set locale would give."""

    def __init__(self, stdin_text: str) -> None:
        self.stdin: Any = io.TextIOWrapper(io.BytesIO(stdin_text.encode()), encoding="latin-1")
        self.stdout: Any = io.TextIOWrapper(io.BytesIO(), encoding="latin-1")

    def output(self) -> str:
        self.stdout.flush()
        return str(self.stdout.buffer.getvalue().decode("utf-8"))


@pytest.mark.parametrize("streams_kind", [StringStreams, ByteStreams])
def test_main_announces_itself_on_stdout_and_answers_a_job(
    monkeypatch: pytest.MonkeyPatch, streams_kind: type[StringStreams | ByteStreams]
) -> None:
    rearms: list[int] = []
    seen: dict[str, Any] = {}

    def fake_limits(memory_limit_bytes: int, cpu_limit_s: int) -> Any:
        seen["limits"] = (memory_limit_bytes, cpu_limit_s)
        return lambda: rearms.append(1)

    monkeypatch.setattr(worker, "_apply_limits", fake_limits)
    rule = RuleDef.model_validate(
        {
            "key": "has-schema",
            "target": "$",
            "condition": "has(this.schema)",
            "severity": "error",
            "message": "Needs a schema.",
            "fixtures": {"pass": [{"document": {}}], "fail": [{"document": {}}]},
        }
    )
    job = EvaluateJob(
        units=[EvalUnit(id=1, rule=rule)],
        document={"schema": []},
        limits=LimitsWire(),
    )
    streams = streams_kind(json.dumps(job.model_dump(mode="json")) + "\n")
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdin", streams.stdin)
    monkeypatch.setattr(sys, "stdout", streams.stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    assert worker.main(["--memory-limit-bytes", "4096", "--cpu-limit-s", "3"]) == 0

    assert seen["limits"] == (4096, 3)
    assert rearms == [1]  # once per job
    messages = [json.loads(line) for line in streams.output().splitlines()]
    assert messages[0]["engine_version"] == engine_version()
    assert {"start": 1} in messages
    done = next(m for m in messages if m.get("done") == 1)
    assert done["result"]["outcome"] == "pass"
    assert messages[-1] == {"end": True}
    # Library prints go to stderr, never to the protocol channel.
    assert sys.stdout is stderr
    if streams_kind is ByteStreams:
        # The protocol is UTF-8 whatever the locale says.
        assert streams.stdin.encoding == "utf-8"
        assert streams.stdout.encoding == "utf-8"
