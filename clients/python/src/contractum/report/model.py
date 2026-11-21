"""The report of one contract test run: a typed model, its builder and its verdict.

``Report`` is the public contract for CI consumers (``schema_version`` "1"). It never holds a
token, a URL with credentials or an unmasked value of a sensitive column.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from contractum.engine.checks import CheckOutcome, Plan, Sample, Skipped, Status

_NO_OUTCOME = "check produced no outcome"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolInfo(_Model):
    name: str = "contractum"
    version: str


class ContractInfo(_Model):
    """Which contract version was tested and where it came from."""

    source: Literal["server", "lock", "file"]
    contract_id: str | None = None
    odcs_id: str | None = None
    name: str | None = None
    semver: str | None = None
    status: str | None = None
    content_sha256: str | None = None
    deprecated: bool = False
    sunset_date: str | None = None


class DatasetInfo(_Model):
    """What was tested; row counts are None when unknown."""

    kind: str
    location: str | None = None
    rows_total: int | None = None
    rows_tested: int | None = None
    sampled: bool = False
    limit: int | None = None
    schema_only: bool = False


class SampleOut(_Model):
    row: int
    value: Any = None


class CheckResult(_Model):
    id: str
    category: str
    column: str | None = None
    rule: str
    status: Status
    severity: str = "error"
    message: str | None = None
    failed_rows: int | None = None
    total_rows: int | None = None
    failed_percent: float | None = None
    observed: Any = None
    threshold: Any = None
    samples: tuple[SampleOut, ...] = ()
    skipped_reason: str | None = None
    sampled: bool = False


class Summary(_Model):
    passed: int = 0
    failed: int = 0
    warning: int = 0
    skipped: int = 0
    error: int = 0
    total: int = 0
    ok: bool = True


class Report(_Model):
    """The typed result of a test run."""

    schema_version: Literal["1"] = "1"
    tool: ToolInfo
    contract: ContractInfo
    dataset: DatasetInfo
    object: str
    summary: Summary
    checks: tuple[CheckResult, ...]
    started_at: datetime
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """No check failed and none could not be evaluated; warnings and skips are allowed."""
        return self.summary.ok

    def exit_code(self, *, strict: bool = False, fail_on_skipped: bool = False) -> int:
        """The process verdict: 2 if any check errored, else 1 on a failure, else 0.

        ``strict`` also turns warnings into 1 and ``fail_on_skipped`` does the same for skipped
        checks. Zero evaluated checks exit 0 unless ``fail_on_skipped`` rejects the skips.
        """
        summary = self.summary
        if summary.error:
            return 2
        if summary.failed:
            return 1
        if strict and summary.warning:
            return 1
        if fail_on_skipped and summary.skipped:
            return 1
        return 0

    def summary_text(self, *, max_checks: int = 20) -> str:
        """A short text for terminals and comments: counts, then one line per non-passed check."""
        summary = self.summary
        contract = f"{self.contract.odcs_id or '-'}@{self.contract.semver or '-'}"
        lines = [
            f"contract {contract} object {self.object}: {summary.passed} passed, "
            f"{summary.failed} failed, {summary.warning} warning, {summary.skipped} skipped, "
            f"{summary.error} error"
        ]
        if summary.total - summary.skipped == 0:
            lines.append("no checks were evaluated")
        shown = [check for check in self.checks if check.status != "passed"]
        for check in shown[:max_checks]:
            lines.append(f"{check.status} {check.id}: {check.message or check.skipped_reason}")
        if len(shown) > max_checks:
            lines.append(f"+{len(shown) - max_checks} more")
        return "\n".join(lines)


def _percent(failed: int | None, total: int | None) -> float | None:
    if failed is None or not total:
        return None
    return round(failed * 100 / total, 4)


def _from_outcome(outcome: CheckOutcome) -> CheckResult:
    return CheckResult(
        id=outcome.check_id,
        category=outcome.category,
        column=outcome.column,
        rule=outcome.rule,
        status=outcome.status,
        severity=outcome.severity,
        message=outcome.message,
        failed_rows=outcome.failed_rows,
        total_rows=outcome.total_rows,
        failed_percent=_percent(outcome.failed_rows, outcome.total_rows),
        observed=outcome.observed,
        threshold=outcome.threshold,
        samples=tuple(_sample_out(sample) for sample in outcome.samples),
        skipped_reason=outcome.skipped_reason,
        sampled=outcome.sampled,
    )


def _sample_out(sample: Sample) -> SampleOut:
    return SampleOut(row=sample.row, value=sample.value)


def _from_skipped(entry: Skipped) -> CheckResult:
    return CheckResult(
        id=entry.check_id,
        category=entry.category,
        column=entry.column,
        rule=entry.rule,
        status="skipped",
        severity=entry.severity,
        skipped_reason=entry.reason,
    )


def _not_evaluated(check_id: str) -> CheckResult:
    """A planned check without an outcome is an error, never a pass."""
    return CheckResult(
        id=check_id, category="schema", rule="unknown", status="error", message=_NO_OUTCOME
    )


def summarize(checks: Sequence[CheckResult]) -> Summary:
    """Count checks by status; ``ok`` is false when any failed or errored."""
    counts = {status: 0 for status in ("passed", "failed", "warning", "skipped", "error")}
    for check in checks:
        counts[check.status] += 1
    return Summary(
        **counts,
        total=len(checks),
        ok=counts["failed"] == 0 and counts["error"] == 0,
    )


def build_report(
    *,
    tool: ToolInfo,
    contract: ContractInfo,
    dataset: DatasetInfo,
    plan: Plan,
    outcomes: Sequence[CheckOutcome],
    lifecycle: CheckOutcome | None = None,
    started_at: datetime | None = None,
    duration_ms: int = 0,
) -> Report:
    """Merge outcomes and skipped entries in ``plan.order``, then the lifecycle check last.

    A planned id without an outcome becomes an ``error`` result; outcomes the plan does not know
    follow the planned ones in the order given.
    """
    results: dict[str, CheckResult] = {
        entry.check_id: _from_skipped(entry) for entry in plan.skipped
    }
    for outcome in outcomes:
        results[outcome.check_id] = _from_outcome(outcome)
    ordered = [
        results.pop(check_id) if check_id in results else _not_evaluated(check_id)
        for check_id in plan.order
    ]
    ordered.extend(results.values())
    if lifecycle is not None:
        ordered.append(_from_outcome(lifecycle))
    return Report(
        tool=tool,
        contract=contract,
        dataset=dataset,
        object=plan.object_name,
        summary=summarize(ordered),
        checks=tuple(ordered),
        started_at=started_at or datetime.now(UTC),
        duration_ms=duration_ms,
    )
