"""The rule worker: ``python -m dc_rules.worker --memory-limit-bytes N --cpu-limit-s M``.

One long-lived process, one CEL ``Env``, one job at a time, JSON lines over stdin and stdout
(see `dc_rules.protocol`). The parent enforces the wall-clock deadline and kills the process; the
worker only bounds what it can bound itself: address space (``RLIMIT_AS``, Linux only), core dumps
(``RLIMIT_CORE``) and CPU time (``RLIMIT_CPU``, re-armed before every job so a long-lived worker
does not run out of its lifetime budget). The limits are applied before the engine is imported.

``serve`` is the loop. It takes the engine and targeter as arguments so tests can drive it
in-process with any engine (or a counting wrapper) and plain string streams.

The result of a unit never contains exception or engine text: a ``MemoryError`` becomes
``memory_exceeded`` and any other exception becomes ``engine_error`` (D-139).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from dc_rules.checks import (
    EMPTY_REFS,
    default_fixture_ctx,
    probe_bindings,
    scale_probe_document,
)
from dc_rules.evaluator import evaluate_rule
from dc_rules.model import CEL2_CTX_KEYS, DSL_VERSION, FixtureCtx
from dc_rules.normalize import normalize_document
from dc_rules.ports import ExpressionEngine, Limits, RuleCompileError, RuleResult, Targeter
from dc_rules.protocol import (
    CheckJob,
    CheckUnit,
    DoneMarker,
    EndMarker,
    EvaluateJob,
    EvalUnit,
    FindingWire,
    Job,
    StartMarker,
    UnitResult,
    WorkerReady,
    error_result,
)

CACHE_MAX_ENTRIES = 1024
CHECK_DETAIL_CHARS = 300

_JOB_ADAPTER: TypeAdapter[EvaluateJob | CheckJob] = TypeAdapter(Job)


class _Out(Protocol):
    def write(self, text: str, /) -> Any: ...

    def flush(self) -> Any: ...


class CompileCache:
    """Bounded cache of compiled (condition, target) pairs; the oldest entry is evicted first.

    Never a source of truth: a miss compiles again (D-118).
    """

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._max = max_entries
        self._entries: OrderedDict[str, tuple[Any, Any]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> tuple[Any, Any] | None:
        return self._entries.get(key)

    def put(self, key: str, value: tuple[Any, Any]) -> None:
        self._entries[key] = value
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)


def _emit(out: _Out, payload: Any) -> None:
    out.write(payload.model_dump_json() + "\n")
    out.flush()


def _unit_result(unit_id: int, result: RuleResult) -> UnitResult:
    """The wire answer of one evaluated rule: its outcome, findings and matched count."""
    return UnitResult(
        id=unit_id,
        outcome=result.outcome,
        findings=[
            FindingWire(
                outcome=f.outcome,
                pointer=f.location.pointer,
                path=f.location.path,
                error_code=f.error_code,
            )
            for f in result.findings
        ],
        matched=result.matched,
    )


def _evaluate_unit(
    unit: EvalUnit,
    document: Any,
    ctx: dict[str, Any],
    limits: Limits,
    *,
    engine: ExpressionEngine,
    targeter: Targeter,
    cache: CompileCache,
    cel2_bindings: Mapping[str, Any] | None = None,
) -> UnitResult:
    rule = unit.rule
    try:
        compiled = cache.get(unit.cache_key) if unit.cache_key is not None else None
        if compiled is None:
            compiled = (
                engine.compile(rule.condition, unit.dsl_version),
                targeter.compile(rule.target),
            )
            if unit.cache_key is not None:
                cache.put(unit.cache_key, compiled)
        result = evaluate_rule(
            rule,
            compiled[0],
            compiled[1],
            document,
            ctx,
            engine=engine,
            targeter=targeter,
            limits=limits,
            extra_bindings=cel2_bindings if unit.dsl_version == "cel-2" else None,
        )
    except RuleCompileError as exc:
        # A published rule that no longer compiles is an engine problem, not an author one.
        out = error_result(unit.id, "engine_error", target=rule.target)
        out.problem_code = exc.code
        return out
    except MemoryError:
        return error_result(unit.id, "memory_exceeded", target=rule.target)
    except Exception:
        return error_result(unit.id, "engine_error", target=rule.target)
    return _unit_result(unit.id, result)


def _check_unit(
    unit: CheckUnit,
    limits: Limits,
    *,
    engine: ExpressionEngine,
    targeter: Targeter,
) -> UnitResult:
    """Answer one authoring check. Never cached: a draft is never a source of truth (D-118).

    The ``detail`` of an ``invalid`` answer is the author's own compile feedback (research
    Pitfall 11), truncated; it never carries a document value.
    """
    rule = unit.rule
    try:
        compiled = (
            engine.compile(rule.condition, unit.dsl_version),
            targeter.compile(rule.target),
        )
        size = engine.compiled_size(compiled[0])
        if size > limits.max_compiled_bytes:
            # Refused for every unit kind: an over-size rule is never evaluated.
            return UnitResult(
                id=unit.id,
                outcome="invalid",
                problem_code="ast_too_large",
                detail=f"compiled size {size} bytes, the limit is {limits.max_compiled_bytes}",
                compiled_bytes=size,
            )
        if unit.kind == "compile":
            return UnitResult(id=unit.id, outcome="pass", compiled_bytes=size)
        extra: dict[str, Any] | None = None
        if unit.kind == "fixture":
            if unit.changes_unavailable:
                return UnitResult(
                    id=unit.id,
                    outcome="invalid",
                    problem_code="fixture_changes_unavailable",
                    detail="the fixture has a previous document but no change classifier was given",
                )
            fixtures = rule.fixtures.pass_ if unit.fixture_kind == "pass" else rule.fixtures.fail
            fixture = fixtures[unit.fixture_index or 0]
            document = normalize_document(fixture.document)
            ctx = (fixture.ctx or FixtureCtx()).model_dump(mode="json")
            if unit.dsl_version == "cel-2":
                # A fixture binds its own ctx.operation, or "save" when it sets none.
                ctx = {"operation": "save", **ctx}
                extra = {
                    "previous": normalize_document(unit.previous),
                    "changes": normalize_document(unit.changes),
                    "refs": normalize_document(unit.refs or EMPTY_REFS),
                }
        else:
            document = normalize_document(scale_probe_document())
            ctx = default_fixture_ctx()
            if unit.dsl_version == "cel-2":
                # The probe binds publish so the branch that does the work is the one probed.
                ctx = {**ctx, "operation": "publish"}
                extra = normalize_document(probe_bindings(scale_probe_document()))
        result = evaluate_rule(
            rule,
            compiled[0],
            compiled[1],
            document,
            ctx,
            engine=engine,
            targeter=targeter,
            limits=limits,
            extra_bindings=extra,
        )
    except RuleCompileError as exc:
        return UnitResult(
            id=unit.id,
            outcome="invalid",
            problem_code=exc.code,
            detail=exc.detail[:CHECK_DETAIL_CHARS],
        )
    except MemoryError:
        return error_result(unit.id, "memory_exceeded", target=rule.target)
    except Exception:
        return error_result(unit.id, "engine_error", target=rule.target)
    return _unit_result(unit.id, result)


def serve(
    lines_in: Iterable[str],
    lines_out: _Out,
    *,
    engine: ExpressionEngine,
    targeter: Targeter,
    before_job: Callable[[], None] | None = None,
    cache: CompileCache | None = None,
) -> None:
    """Answer one job per input line until the input ends."""
    cache = cache if cache is not None else CompileCache()
    for line in lines_in:
        if not line.strip():
            continue
        try:
            job = _JOB_ADAPTER.validate_python(json.loads(line))
        except ValueError, ValidationError:
            # Nothing to attribute the answer to: end the job, the parent marks every unit.
            _emit(lines_out, EndMarker())
            continue
        if before_job is not None:
            before_job()
        if isinstance(job, EvaluateJob):
            try:
                document = normalize_document(job.document)
                cel2_bindings = {
                    "previous": normalize_document(job.previous),
                    "changes": normalize_document(job.changes),
                    "refs": normalize_document(job.refs or EMPTY_REFS),
                }
            except Exception:
                for unit in job.units:
                    _emit(lines_out, StartMarker(start=unit.id))
                    _emit(
                        lines_out,
                        DoneMarker(
                            done=unit.id,
                            result=error_result(unit.id, "engine_error", target=unit.rule.target),
                        ),
                    )
                _emit(lines_out, EndMarker())
                continue
            limits = job.limits.to_limits()
            # cel-1 never sees the cel-2-only ctx keys, so legacy versions evaluate as before.
            cel1_ctx = {k: v for k, v in job.ctx.items() if k not in CEL2_CTX_KEYS}
            for unit in job.units:
                _emit(lines_out, StartMarker(start=unit.id))
                result = _evaluate_unit(
                    unit,
                    document,
                    job.ctx if unit.dsl_version == "cel-2" else cel1_ctx,
                    limits,
                    engine=engine,
                    targeter=targeter,
                    cache=cache,
                    cel2_bindings=cel2_bindings,
                )
                _emit(lines_out, DoneMarker(done=unit.id, result=result))
        else:
            for check_unit in job.units:
                _emit(lines_out, StartMarker(start=check_unit.id))
                _emit(
                    lines_out,
                    DoneMarker(
                        done=check_unit.id,
                        result=_check_unit(
                            check_unit, job.limits.to_limits(), engine=engine, targeter=targeter
                        ),
                    ),
                )
        _emit(lines_out, EndMarker())


def _apply_limits(memory_limit_bytes: int, cpu_limit_s: int) -> Callable[[], None]:
    """Apply the resource limits; returns the per-job CPU re-arm callback."""
    import resource
    import time

    if sys.platform == "linux":
        # RLIMIT_AS is enforced on Linux only (macOS accepts the call and ignores it).
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
        except ValueError, OSError:
            pass
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except ValueError, OSError:
        pass

    try:
        _, hard = resource.getrlimit(resource.RLIMIT_CPU)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit_s, hard))
    except ValueError, OSError:
        return lambda: None

    def rearm() -> None:
        # RLIMIT_CPU counts the process lifetime; give every job a fresh allowance.
        soft = int(time.process_time()) + cpu_limit_s
        if hard != resource.RLIM_INFINITY:
            soft = min(soft, hard)
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))
        except ValueError, OSError:
            pass

    return rearm


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dc_rules.worker")
    parser.add_argument("--memory-limit-bytes", type=int, default=1024**3)
    parser.add_argument("--cpu-limit-s", type=int, default=10)
    args = parser.parse_args(argv)

    rearm = _apply_limits(args.memory_limit_bytes, args.cpu_limit_s)

    # Imported only now, after the limits are in place.
    from dc_rules.cel_engine import CelEngine, engine_version
    from dc_rules.targets import JsonPathTargeter

    protocol_out = sys.stdout
    # Anything a library prints must not corrupt the protocol channel.
    sys.stdout = sys.stderr
    for stream in (sys.stdin, protocol_out):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    engine = CelEngine()
    targeter = JsonPathTargeter()
    _emit(
        protocol_out,
        WorkerReady(engine_version=engine_version(), dsl_version=DSL_VERSION),
    )
    serve(sys.stdin, protocol_out, engine=engine, targeter=targeter, before_job=rearm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
