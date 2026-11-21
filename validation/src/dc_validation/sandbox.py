"""The rule sandbox: a pool of worker processes that evaluate rules out of process (D-137, D-138).

The web process never imports the CEL engine (an import-linter contract and a subprocess test
enforce it). Every evaluation runs in a long-lived ``python -m dc_rules.worker`` subprocess that
speaks JSON lines over stdin and stdout (``dc_rules.protocol``). The engine has no way to
interrupt a running evaluation, so the parent owns the clock:

* a per-evaluation deadline (``rule_timeout_ms``) measured in the worker's user CPU time where the
  platform reports it (``/proc/<pid>/stat``, Linux): when the worker does not answer in time the
  parent checks how much CPU it used since its last line. A worker that used its allowance is a
  runaway rule, so it is killed and reaped, the unit named by the last ``start`` marker is marked
  ``timeout`` and a replacement resends the units that are left. A worker that used less was
  starved or stalled (host load), so the parent keeps waiting, up to a hard wall-clock cap of
  ``_WALL_CAP_FACTOR`` times the rule timeout; the verdict does not depend on how busy the host is
  (WR-01). Without ``/proc`` the deadline is plain wall-clock;
* a worker that fails before it marks any unit as started is an infrastructure failure, not a
  verdict on a rule: the job is resent once to a fresh worker (WR-02);
* a per-run budget (``run_budget_s``): once it is spent every unevaluated unit is
  ``run_budget_exceeded``; a rule is never silently skipped;
* bounded concurrency: at most ``max_concurrent_runs`` runs hold the sandbox at once, over at
  most ``eval_workers`` processes. ``slot()`` waits ``queue_wait_s`` and then raises
  `SandboxBusy`, which the HTTP layer maps to 503 with ``Retry-After``.
* a separate, smaller batch pool (``batch_slot()``, D-161) for rule impact pages: a page holds a
  batch slot and takes a run slot and a worker only while one of its documents is evaluated. The
  pool is below ``eval_workers`` (and the run slots), so batches can never hold every worker and
  at least one is always left for save and publish runs (WR-03; with one worker it is shared).

Workers spawn lazily on first use and are recycled after ``worker_max_jobs`` jobs. Nothing in a
worker touches the network or a database.

``evaluate`` and ``check`` each take their own slot. A caller that enters ``slot()`` itself (the
tests do, to saturate the sandbox deterministically) must not call them from inside it unless it
leaves room for the nested slot.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from dc_core.app import LifespanHook
from dc_rules.model import RuleFixtures
from dc_rules.ports import ErrorCode
from dc_rules.protocol import (
    CheckJob,
    CheckUnit,
    DoneMarker,
    EvaluateJob,
    EvalUnit,
    LimitsWire,
    StartMarker,
    UnitResult,
    WorkerReady,
    error_result,
)
from fastapi import FastAPI
from pydantic import BaseModel

from dc_validation.settings import ValidationSettings

_log = structlog.get_logger("dc_validation.sandbox")

_READ_LIMIT = 16 * 1024 * 1024
# A cold worker parses the whole job line before its first marker; that is not billed to rule 1.
_FIRST_MARKER_ALLOWANCE_S = 2.0
# Hard wall-clock cap, in rule timeouts, for a worker that is waiting but not computing.
_WALL_CAP_FACTOR = 8
_NO_FIXTURES = RuleFixtures(pass_=[], fail=[])


class SandboxBusy(Exception):
    """No run slot became free within ``queue_wait_s`` (HTTP 503 with Retry-After)."""


class SandboxUnavailable(Exception):
    """A worker process could not be started (HTTP 503)."""


@dataclass(eq=False)
class _Worker:
    proc: asyncio.subprocess.Process
    jobs: int = 0


def _cpu_seconds(pid: int) -> float | None:
    """User-mode CPU seconds (all threads) a process has used; ``None`` when unknown.

    Reads ``/proc/<pid>/stat`` (Linux): field 14, ``utime``, in clock ticks. Only user time counts:
    a runaway rule is a compute loop (user time), while the system time of a memory-hungry rule is
    page-fault work whose cost depends on the host (a cold or contended VM can spend a second of
    system time on the first touch of a gigabyte), so charging it would make the verdict depend on
    host load again. System-heavy work stays bounded by ``RLIMIT_AS``, ``RLIMIT_CPU`` and the hard
    wall cap. The command name (field 2) may contain spaces and parentheses, so parse after its
    last ``)``.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("ascii", "replace")
        fields = raw[raw.rindex(")") + 2 :].split()
        ticks = int(fields[11])
        return ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _target_of(unit: EvalUnit | CheckUnit) -> str:
    return unit.rule.target


class Sandbox:
    def __init__(
        self, settings: ValidationSettings, *, worker_argv: Sequence[str] | None = None
    ) -> None:
        self._settings = settings
        self._argv = (
            list(worker_argv)
            if worker_argv is not None
            else [sys.executable, "-m", "dc_rules.worker"]
        )
        self._run_slots = asyncio.Semaphore(settings.max_concurrent_runs)
        # D-161: rule impact pages wait in their own, smaller pool before taking any run slot.
        self._batch_slots = asyncio.Semaphore(settings.batch_slot_count())
        self._worker_permits = asyncio.Semaphore(settings.eval_workers)
        self._idle: deque[_Worker] = deque()
        self._all: set[_Worker] = set()
        self._engine_version: str | None = None
        self._closed = False

    @property
    def engine_version(self) -> str | None:
        """The engine version a worker reported at start (``None`` before the first spawn)."""
        return self._engine_version

    # -- concurrency ---------------------------------------------------------------------------

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one run slot; ``SandboxBusy`` when none frees up within ``queue_wait_s``."""
        try:
            await asyncio.wait_for(self._run_slots.acquire(), self._settings.queue_wait_s)
        except TimeoutError:
            raise SandboxBusy("the rule sandbox is saturated") from None
        try:
            yield
        finally:
            self._run_slots.release()

    @asynccontextmanager
    async def batch_slot(self) -> AsyncIterator[None]:
        """Hold one batch slot for a dry-run or drift page (D-161).

        ``SandboxBusy`` when none frees up within ``queue_wait_s``. A page holds its batch slot for
        its whole evaluation and takes a normal run slot (``evaluate``) only per document, so at
        most ``batch_slot_count()`` run slots and workers ever serve batches; that count is below
        ``eval_workers``, so a worker stays free for runs (WR-03).
        """
        try:
            await asyncio.wait_for(self._batch_slots.acquire(), self._settings.queue_wait_s)
        except TimeoutError:
            raise SandboxBusy("the batch pool is saturated") from None
        try:
            yield
        finally:
            self._batch_slots.release()

    # -- public API ----------------------------------------------------------------------------

    async def evaluate(
        self,
        units: list[EvalUnit],
        document: Any,
        ctx: Mapping[str, Any] | None = None,
        *,
        previous: Any = None,
        changes: Sequence[Any] = (),
        refs: Mapping[str, Any] | None = None,
    ) -> dict[int, UnitResult]:
        """Evaluate each unit against ``document``; every unit gets exactly one result.

        ``previous``, ``changes`` and ``refs`` are the cel-2 bindings. They go on the job only
        when at least one unit is cel-2; a cel-1-only job carries the defaults so it never ships a
        document-sized baseline it cannot read (D-152, Pitfall 7).
        """
        # Fixtures are authoring data; the worker has no use for them, so they stay off the wire.
        stripped = {
            u.id: u.model_copy(
                update={"rule": u.rule.model_copy(update={"fixtures": _NO_FIXTURES})}
            )
            for u in units
        }
        limits = LimitsWire.from_limits(self._settings.limits())
        context = dict(ctx or {})
        bindings: dict[str, Any] = {}
        if any(u.dsl_version == "cel-2" for u in units):
            bindings = {
                "previous": previous,
                "changes": list(changes),
                "refs": dict(refs) if refs is not None else {},
            }

        def make_job(remaining: list[Any]) -> BaseModel:
            return EvaluateJob(
                units=[stripped[u.id] for u in remaining],
                document=document,
                ctx=context,
                limits=limits,
                **bindings,
            )

        return await self._execute(units, make_job)

    async def check(self, units: list[CheckUnit]) -> dict[int, UnitResult]:
        """Run authoring checks (compile, fixtures, scale probe) with ``check_budget_s``."""
        limits = LimitsWire.from_limits(self._settings.limits())

        def make_job(remaining: list[Any]) -> BaseModel:
            return CheckJob(units=list(remaining), limits=limits)

        return await self._execute(units, make_job, budget_s=self._settings.check_budget_s)

    async def aclose(self) -> None:
        """Kill every worker process."""
        self._closed = True
        for worker in list(self._all):
            await self._discard(worker)

    # -- one run -------------------------------------------------------------------------------

    async def _execute(
        self,
        units: Sequence[EvalUnit] | Sequence[CheckUnit],
        make_job: Callable[[list[Any]], BaseModel],
        *,
        budget_s: float | None = None,
    ) -> dict[int, UnitResult]:
        if len({u.id for u in units}) != len(units):
            raise ValueError("unit ids must be unique within one call")
        results: dict[int, UnitResult] = {}
        remaining: list[Any] = list(units)
        if not remaining:
            return results
        loop = asyncio.get_running_loop()
        queue_deadline = loop.time() + self._settings.queue_wait_s
        async with self.slot():
            if self._closed:
                raise SandboxUnavailable("the rule sandbox is closed")
            worker: _Worker | None = await self._acquire_worker(
                max(queue_deadline - loop.time(), 0.0)
            )
            clean = False
            deadline = loop.time() + (self._settings.run_budget_s if budget_s is None else budget_s)
            retries_left = 1  # one resend to a fresh worker when nothing had started (WR-02)
            try:
                while remaining:
                    if loop.time() >= deadline:
                        for unit in remaining:
                            results[unit.id] = error_result(
                                unit.id, "run_budget_exceeded", target=_target_of(unit)
                            )
                        break
                    if worker is None:
                        worker = await self._spawn()
                    clean = False
                    status = await self._run_job(
                        worker,
                        make_job(remaining),
                        remaining,
                        results,
                        deadline,
                        blame_unstarted=retries_left == 0,
                    )
                    worker.jobs += 1
                    if status == "unstarted":
                        # `_run_job` killed and reaped the worker before any unit started: an
                        # infrastructure failure, so the whole job goes to a fresh worker once.
                        worker = None
                        retries_left -= 1
                        continue
                    if status == "dead":
                        worker = None  # `_run_job` killed and reaped it
                    else:
                        clean = True
                        for unit in remaining:
                            if unit.id not in results:  # the worker ended the job early
                                results[unit.id] = error_result(
                                    unit.id, "engine_error", target=_target_of(unit)
                                )
                    remaining = [u for u in remaining if u.id not in results]
            finally:
                await self._release_worker(worker, reusable=clean)
        return results

    async def _run_job(
        self,
        worker: _Worker,
        job: BaseModel,
        remaining: list[Any],
        results: dict[int, UnitResult],
        deadline: float,
        *,
        blame_unstarted: bool = True,
    ) -> Literal["ok", "dead", "unstarted"]:
        """Send one job and read its markers.

        ``unstarted``: the worker failed before it printed any marker for this job and
        ``blame_unstarted`` is false, so no unit is charged and the caller may resend the job.
        """
        loop = asyncio.get_running_loop()
        proc = worker.proc
        assert proc.stdin is not None and proc.stdout is not None  # noqa: S101 (PIPE spawn)
        rule_timeout = self._settings.rule_timeout_ms / 1000
        ids = {u.id for u in remaining}
        current: int | None = None
        seen_marker = False

        async def fail(code: ErrorCode) -> Literal["dead", "unstarted"]:
            if not seen_marker and not blame_unstarted and code != "run_budget_exceeded":
                await self._discard(worker)
                return "unstarted"
            culprit = current if current is not None and current not in results else None
            if culprit is None:
                culprit = next((u.id for u in remaining if u.id not in results), None)
            if culprit is not None:
                unit = next(u for u in remaining if u.id == culprit)
                results[culprit] = error_result(culprit, code, target=_target_of(unit))
            await self._discard(worker)
            return "dead"

        try:
            proc.stdin.write(json.dumps(job.model_dump(mode="json")).encode() + b"\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return await fail("engine_error")

        # The clock of the line being waited for: wall time and the worker's CPU time since the
        # previous line (or the job write). Reset every time a line arrives.
        wall_mark = loop.time()
        cpu_mark = _cpu_seconds(proc.pid)
        extra_wait: float | None = None  # the CPU allowance a starved worker still has
        while True:
            left = deadline - loop.time()
            if left <= 0:
                return await fail("run_budget_exceeded")
            allowance = (
                rule_timeout if seen_marker else max(rule_timeout, _FIRST_MARKER_ALLOWANCE_S)
            )
            wait = allowance if extra_wait is None else max(extra_wait, 0.02)
            budget_limited = left <= wait
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), min(wait, left))
            except TimeoutError:
                if budget_limited:
                    return await fail("run_budget_exceeded")
                extra_wait = self._cpu_allowance_left(proc.pid, cpu_mark, wall_mark, allowance)
                if extra_wait is not None:
                    continue  # waiting, not computing: the host is loaded, not the rule runaway
                return await fail("timeout")
            except ValueError:  # a line longer than the stream limit
                return await fail("engine_error")
            if not raw:  # EOF: the worker died (killed, crashed, CPU limit)
                with suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), 0.2)
                # A negative code is the signal that ended it (-9: killed, -24: CPU limit). Never
                # rule or document text.
                _log.warning("rule_worker_exited", returncode=proc.returncode)
                return await fail("engine_error")
            wall_mark = loop.time()
            cpu_mark = _cpu_seconds(proc.pid)
            extra_wait = None
            try:
                message = json.loads(raw)
                if "start" in message:
                    marker = StartMarker.model_validate(message)
                    if marker.start not in ids:
                        raise ValueError("unknown unit")
                    current = marker.start
                    seen_marker = True
                elif "done" in message:
                    done = DoneMarker.model_validate(message)
                    if done.done not in ids or done.result.id != done.done:
                        raise ValueError("unknown unit")
                    results.setdefault(done.done, done.result)
                    current = None
                    seen_marker = True
                elif message.get("end") is True:
                    return "ok"
                else:
                    raise ValueError("unknown marker")
            except (ValueError, TypeError, AttributeError):
                return await fail("engine_error")

    @staticmethod
    def _cpu_allowance_left(
        pid: int, cpu_mark: float | None, wall_mark: float, allowance: float
    ) -> float | None:
        """The CPU seconds a silent worker may still use, or ``None`` when it must be killed.

        ``None`` when the worker used its allowance since its last line, when the hard wall cap
        is hit, and when its CPU time is unknown (no ``/proc``): the wall-clock deadline stands.
        """
        if cpu_mark is None:
            return None
        cpu_now = _cpu_seconds(pid)
        if cpu_now is None:
            return None
        wall = asyncio.get_running_loop().time() - wall_mark
        used = cpu_now - cpu_mark
        if used >= allowance or wall >= allowance * _WALL_CAP_FACTOR:
            return None
        return min(allowance - used, allowance * _WALL_CAP_FACTOR - wall)

    # -- worker lifecycle ----------------------------------------------------------------------

    async def _acquire_worker(self, wait_s: float) -> _Worker:
        """Take a worker permit (``SandboxBusy`` after ``wait_s``), then an idle or a new worker."""
        try:
            await asyncio.wait_for(self._worker_permits.acquire(), wait_s)
        except TimeoutError:
            raise SandboxBusy("the rule sandbox is saturated") from None
        while self._idle:
            worker = self._idle.popleft()
            if worker.proc.returncode is None:
                return worker
            self._all.discard(worker)
        try:
            return await self._spawn()
        except BaseException:
            self._worker_permits.release()
            raise

    async def _release_worker(self, worker: _Worker | None, *, reusable: bool) -> None:
        try:
            if worker is not None:
                recycle = worker.jobs >= self._settings.worker_max_jobs
                if reusable and not recycle and not self._closed and worker.proc.returncode is None:
                    self._idle.append(worker)
                else:
                    await self._discard(worker)
        finally:
            self._worker_permits.release()

    async def _spawn(self) -> _Worker:
        s = self._settings
        argv = [
            *self._argv,
            "--memory-limit-bytes",
            str(s.worker_memory_bytes),
            "--cpu-limit-s",
            str(math.ceil(max(s.run_budget_s, s.check_budget_s)) + 5),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=_READ_LIMIT,
            )
        except OSError as exc:
            raise SandboxUnavailable("a rule worker could not be started") from exc
        worker = _Worker(proc)
        self._all.add(worker)
        try:
            assert proc.stdout is not None  # noqa: S101 (PIPE spawn)
            line = await asyncio.wait_for(
                proc.stdout.readline(), self._settings.worker_ready_timeout_s
            )
            ready = WorkerReady.model_validate_json(line)
        except (TimeoutError, ValueError):
            await self._discard(worker)
            _log.warning("rule_worker_not_ready", returncode=proc.returncode)
            raise SandboxUnavailable("a rule worker did not become ready") from None
        except BaseException:
            await self._discard(worker)
            raise
        self._engine_version = ready.engine_version
        return worker

    async def _discard(self, worker: _Worker) -> None:
        self._all.discard(worker)
        with suppress(ValueError):
            self._idle.remove(worker)
        with suppress(ProcessLookupError):
            worker.proc.kill()
        await worker.proc.wait()


def sandbox_hook(settings: ValidationSettings) -> LifespanHook:
    """Lifespan hook: ``app.state.sandbox`` is the pool; its workers are killed on shutdown."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        sandbox = Sandbox(settings)
        app.state.sandbox = sandbox
        try:
            yield
        finally:
            await sandbox.aclose()

    return _hook
