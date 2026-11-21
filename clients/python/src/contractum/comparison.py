"""Compare one dataset against two versions of a contract, usually Latest and the Preview.

``compare`` tests the same data against both versions with the same options, lines the two reports
up check by check and adds the server's classified diff of the contract as context. It answers the
question "will my data still pass when the Preview becomes Latest?" before that happens.

The result is a frozen model that serialises to JSON. It holds the two engine-rendered reports,
so failing values are already masked and bounded; nothing here reads the dataset again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from contractum.errors import AccessDeniedError, NotFoundError, UsageError
from contractum.report.markdown import _cell
from contractum.report.model import CheckResult, Report
from contractum.report.text import STATUS_WORDS
from contractum.testing import TestOptions

if TYPE_CHECKING:
    from contractum.client import Client
    from contractum.models import ContractDiff

Change = Literal["new", "removed", "changed", "same"]

DIFF_EXAMPLES = 10
MAX_MARKDOWN_ROWS = 100
_FAILING = frozenset({"failed", "error"})
_ORDER = ("new", "changed", "removed")
_DIFF_UNAVAILABLE = "the classified diff is not available to this caller"
_VALUE_LIMIT = 60


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckDelta(_Model):
    """How one check differs between the two versions.

    ``change`` is ``new`` (only in the target version), ``removed`` (only in the source version),
    ``changed`` (the status differs) or ``same``. ``regression`` marks a check that fails or
    errors in the target version and did not before.
    """

    id: str
    column: str | None = None
    rule: str
    change: Change
    from_status: str | None = None
    to_status: str | None = None
    regression: bool = False
    message: str | None = None


class DiffSummary(_Model):
    """The server's classified diff of the two versions, reduced to what a reader scans."""

    counts: dict[str, int]
    changes_total: int
    required_bump: str
    truncated: bool = False
    examples: tuple[str, ...] = ()


class CompareResult(_Model):
    """Both reports, the per-check deltas and the server's diff of the contract."""

    schema_version: Literal["1"] = "1"
    ref: str
    contract_id: str | None = None
    from_semver: str | None = None
    to_semver: str | None = None
    from_report: Report
    to_report: Report
    deltas: tuple[CheckDelta, ...]
    unchanged: int = 0
    newly_failing: int = 0
    server_diff: DiffSummary | None = None
    server_diff_note: str | None = None

    def exit_code(self, *, strict: bool = False, fail_on_skipped: bool = False) -> int:
        """2 when either version could not be evaluated, 1 when the target version fails.

        The target version is judged on its own, so a check that already failed in the source
        version still makes the exit code 1; the ``changed`` and ``regression`` fields tell
        existing failures from new ones.
        """
        if self.from_report.summary.error or self.to_report.summary.error:
            return 2
        return self.to_report.exit_code(strict=strict, fail_on_skipped=fail_on_skipped)

    @property
    def shown(self) -> list[CheckDelta]:
        """The deltas that are not ``same``, new first, then changed, then removed."""
        order = {name: index for index, name in enumerate(_ORDER)}
        kept = [delta for delta in self.deltas if delta.change != "same"]
        return sorted(kept, key=lambda delta: order[delta.change])


def _present(old: CheckResult | None, new: CheckResult) -> CheckDelta:
    """The delta of a check the target version has: new, changed or the same."""
    if old is None:
        change: Change = "new"
    else:
        change = "changed" if old.status != new.status else "same"
    return CheckDelta(
        id=new.id,
        column=new.column,
        rule=new.rule,
        change=change,
        from_status=old.status if old is not None else None,
        to_status=new.status,
        regression=new.status in _FAILING and (old is None or old.status not in _FAILING),
        message=new.message or new.skipped_reason,
    )


def _removed(old: CheckResult) -> CheckDelta:
    """The delta of a check only the source version has."""
    return CheckDelta(
        id=old.id,
        column=old.column,
        rule=old.rule,
        change="removed",
        from_status=old.status,
        message=old.message or old.skipped_reason,
    )


def compare_reports(a: Report, b: Report) -> list[CheckDelta]:
    """Line two reports up by check id: ``b``'s checks in order, then those only ``a`` has."""
    before = {check.id: check for check in a.checks}
    deltas = [_present(before.get(check.id), check) for check in b.checks]
    present = {check.id for check in b.checks}
    deltas.extend(_removed(check) for check in a.checks if check.id not in present)
    return deltas


def _short(value: Any) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= _VALUE_LIMIT else text[: _VALUE_LIMIT - 3] + "..."


def _describe(change: dict[str, Any]) -> str:
    """One line for a classified change: ``[class] code path (attribute: before -> after)``."""
    label = change.get("class") or change.get("classification") or "change"
    code = change.get("code") or change.get("kind") or "change"
    text = f"[{_short(label)}] {_short(code)} {_short(change.get('path', ''))}".rstrip()
    attribute = change.get("attribute")
    if attribute:
        before, after = _short(change.get("before")), _short(change.get("after"))
        text += f" ({_short(attribute)}: {before} -> {after})"
    return text


def summarize_diff(diff: ContractDiff) -> DiffSummary:
    """The counts per class, the bump the server requires and the first few changes."""
    return DiffSummary(
        counts=dict(diff.counts),
        changes_total=diff.changes_total,
        required_bump=diff.required_bump,
        truncated=diff.truncated or diff.changes_total > len(diff.changes),
        examples=tuple(_describe(change) for change in diff.changes[:DIFF_EXAMPLES]),
    )


def _server_diff(
    client: Client, contract_id: str, from_semver: str, to_semver: str
) -> tuple[DiffSummary | None, str | None]:
    if from_semver == to_semver:
        return None, f"both sides are version {from_semver}, there is nothing to diff"
    try:
        diff = client.diff(contract_id, from_semver, to_semver)
    except (AccessDeniedError, NotFoundError):
        return None, _DIFF_UNAVAILABLE
    return summarize_diff(diff), None


def compare(
    client: Client,
    ref: str,
    data: object,
    *,
    from_version: str = "latest",
    to_version: str = "preview",
    options: TestOptions | None = None,
) -> CompareResult:
    """Test ``data`` against two versions of the contract ``ref`` and show what changed.

    ``from_version`` and ``to_version`` are ``latest``, ``preview`` or an exact SemVer. A missing
    Preview raises ``NotFoundError``. Pass a path, a table or a frame as ``data``, not a one-shot
    stream: the data is read once per version. The server's classified diff is added when the
    caller may read it; a 403 or 404 on the diff only leaves ``server_diff`` empty with a note.
    """
    before = client.get(ref, version=from_version)
    after = client.get(ref, version=to_version)
    if before.contract_id != after.contract_id:
        raise UsageError(f"{ref!r} resolved to two different contracts")
    opts = options or TestOptions()
    from_report = before.test(data, options=opts)
    to_report = after.test(data, options=opts)
    deltas = compare_reports(from_report, to_report)
    summary, note = (
        _server_diff(client, before.contract_id, before.semver, after.semver)
        if before.contract_id and before.semver and after.semver
        else (None, "the server sent no contract id or version")
    )
    return CompareResult(
        ref=ref,
        contract_id=before.contract_id,
        from_semver=before.semver,
        to_semver=after.semver,
        from_report=from_report,
        to_report=to_report,
        deltas=tuple(deltas),
        unchanged=sum(1 for delta in deltas if delta.change == "same"),
        newly_failing=sum(1 for delta in deltas if delta.regression),
        server_diff=summary,
        server_diff_note=note,
    )


def compare_to_json(result: CompareResult) -> str:
    """The result as indented JSON with a trailing newline."""
    return result.model_dump_json(indent=2) + "\n"


def status_word(value: str | None) -> str:
    return STATUS_WORDS.get(value, "-") if value else "-"


def _markdown_header(result: CompareResult) -> list[str]:
    to_label = result.to_semver or "-"
    verdict = "REGRESSION" if result.newly_failing else "OK"
    counts = {name: sum(1 for d in result.deltas if d.change == name) for name in _ORDER}
    return [
        f"## Contractum compare: {verdict}",
        "",
        f"- **Contract:** `{_cell(result.ref)}`, `{_cell(result.from_semver or '-')}` "
        f"to `{_cell(to_label)}`",
        f"- **Newly failing in {_cell(to_label)}:** {result.newly_failing}",
        f"- **Checks:** {counts['changed']} changed, {counts['new']} new, "
        f"{counts['removed']} removed, {result.unchanged} unchanged",
        "",
    ]


def _markdown_table(result: CompareResult) -> list[str]:
    from_label, to_label = _cell(result.from_semver or "from"), _cell(result.to_semver or "to")
    lines = [
        f"| Check | Column | Rule | {from_label} | {to_label} | Change |",
        "|---|---|---|---|---|---|",
    ]
    rows = result.shown
    for delta in rows[:MAX_MARKDOWN_ROWS]:
        change = f"{delta.change}, newly failing" if delta.regression else delta.change
        lines.append(
            f"| `{_cell(delta.id)}` | {_cell(delta.column or '-')} | {_cell(delta.rule)} "
            f"| {status_word(delta.from_status)} | {status_word(delta.to_status)} | {change} |"
        )
    if len(rows) > MAX_MARKDOWN_ROWS:
        lines.extend(["", f"+{len(rows) - MAX_MARKDOWN_ROWS} more"])
    lines.append("")
    return lines


def diff_lines(result: CompareResult) -> list[str]:
    """The server diff as plain lines: a summary line, then the first few changes (or the note)."""
    diff = result.server_diff
    if diff is None:
        return [result.server_diff_note] if result.server_diff_note else []
    counts = ", ".join(f"{count} {name}" for name, count in sorted(diff.counts.items()))
    summary = f"contract diff: {diff.changes_total} changes"
    summary += f" ({counts})" if counts else ""
    lines = [f"{summary}, required version bump: {diff.required_bump}"]
    lines.extend(f"  {example}" for example in diff.examples)
    if diff.truncated or diff.changes_total > len(diff.examples):
        lines.append(f"  +{max(diff.changes_total - len(diff.examples), 0)} more")
    return lines


def compare_to_markdown(result: CompareResult) -> str:
    """A Markdown comment for a pull request: the verdict, the changed checks and the diff."""
    lines = _markdown_header(result)
    if result.shown:
        lines.extend(_markdown_table(result))
    else:
        lines.extend(["No check changed status.", ""])
    extra = diff_lines(result)
    if extra:
        lines.append("**Contract diff**")
        lines.append("")
        lines.extend(f"- {_cell(line.strip())}" for line in extra)
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "CheckDelta",
    "CompareResult",
    "DiffSummary",
    "compare",
    "compare_reports",
    "compare_to_json",
    "compare_to_markdown",
    "status_word",
    "summarize_diff",
]
