"""A Markdown summary of a report for pull-request comments.

It lists the failed, error and warning checks, keeps failing values in a collapsed block and
never exceeds ``max_chars`` (a comment limit); when the text would be longer it first drops the
sample block and then rows, and says so.
"""

from __future__ import annotations

import re
from collections import defaultdict

from contractum.report.model import CheckResult, Report
from contractum.report.text import STATUS_WORDS, failing_text, sample_lines

MAX_CELL = 200
MAX_SAMPLES = 5
SKIPPED_IDS_SHOWN = 5
_ORDER = {"failed": 0, "error": 1, "warning": 2}
_BACKTICKS = re.compile("`+")
_NOTE = "\n_Output shortened to fit the size limit._\n"


def _cell(value: object) -> str:
    """Text for one table cell: one line, bounded, with markup and table characters escaped."""
    text = " ".join(str(value).split())
    if len(text) > MAX_CELL:
        text = text[: MAX_CELL - 1] + "..."
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("`", "\\`")
    )


def _verdict(report: Report) -> str:
    return "PASSED" if report.ok else "FAILED"


def _header(report: Report) -> list[str]:
    contract = report.contract
    label = f"{contract.odcs_id or contract.name or 'contract'}@{contract.semver or '-'}"
    status = f" ({_cell(contract.status)})" if contract.status else ""
    dataset = report.dataset
    if dataset.schema_only:
        rows = "schema only, no rows read"
    else:
        tested = "?" if dataset.rows_tested is None else str(dataset.rows_tested)
        total = "?" if dataset.rows_total is None else str(dataset.rows_total)
        rows = f"{tested} of {total} rows tested"
        if dataset.sampled:
            rows += " (a sample, not every row)"
    summary = report.summary
    return [
        f"## Contractum: {_verdict(report)}",
        "",
        f"- **Contract:** `{_cell(label)}`{status}",
        f"- **Object:** `{_cell(report.object)}`",
        f"- **Dataset:** {_cell(dataset.kind)}, {rows}",
        f"- **Checks:** {summary.passed} passed, {summary.failed} failed, "
        f"{summary.warning} warning, {summary.skipped} skipped, {summary.error} error",
        "",
    ]


def _table(checks: list[CheckResult], max_rows: int) -> list[str]:
    lines = ["| Status | Check | Column | Rule | Failing | Details |", "|---|---|---|---|---|---|"]
    for check in checks[:max_rows]:
        lines.append(
            f"| {STATUS_WORDS[check.status]} | `{_cell(check.id)}` | {_cell(check.column or '-')} "
            f"| {_cell(check.rule)} | {_cell(failing_text(check))} "
            f"| {_cell(check.message or check.skipped_reason or '')} |"
        )
    if len(checks) > max_rows:
        lines.append("")
        lines.append(f"+{len(checks) - max_rows} more")
    lines.append("")
    return lines


def _fence(body: str) -> str:
    longest = max((len(run) for run in _BACKTICKS.findall(body)), default=0)
    return "`" * max(3, longest + 1)


def _samples(checks: list[CheckResult], max_rows: int) -> list[str]:
    blocks: list[str] = []
    for check in checks[:max_rows]:
        lines = [" ".join(line.split())[:MAX_CELL] for line in sample_lines(check, MAX_SAMPLES)]
        if lines:
            blocks.append(f"{check.id}\n" + "\n".join(f"  {line}" for line in lines))
    if not blocks:
        return []
    body = "\n".join(blocks)
    fence = _fence(body)
    return [
        "<details><summary>Failing values</summary>",
        "",
        f"{fence}text",
        body,
        fence,
        "",
        "</details>",
        "",
    ]


def _skipped(report: Report) -> list[str]:
    groups: dict[str, list[str]] = defaultdict(list)
    for check in report.checks:
        if check.status == "skipped":
            groups[check.skipped_reason or "no reason given"].append(check.id)
    if not groups:
        return []
    total = sum(len(ids) for ids in groups.values())
    lines = [f"<details><summary>{total} skipped checks</summary>", ""]
    for reason, ids in groups.items():
        shown = ", ".join(f"`{_cell(item)}`" for item in ids[:SKIPPED_IDS_SHOWN])
        more = f" and {len(ids) - SKIPPED_IDS_SHOWN} more" if len(ids) > SKIPPED_IDS_SHOWN else ""
        lines.append(f"- {_cell(reason)}: {len(ids)} checks ({shown}{more})")
    lines.extend(["", "</details>", ""])
    return lines


def _render(report: Report, listed: list[CheckResult], rows: int, *, samples: bool) -> str:
    lines = _header(report)
    if listed:
        lines.extend(_table(listed, rows))
        if samples:
            lines.extend(_samples(listed, rows))
    else:
        lines.extend(["Every evaluated check passed.", ""])
    lines.extend(_skipped(report))
    return "\n".join(lines).rstrip() + "\n"


def report_to_markdown(report: Report, *, max_rows: int = 50, max_chars: int = 65000) -> str:
    """The Markdown summary: non-passed checks (``max_rows`` of them, then ``+N more``).

    Failing values sit in a collapsed block. The text stays under ``max_chars``: the sample block
    goes first, then rows are halved, and a note says the output was shortened.
    """
    listed = sorted(
        (check for check in report.checks if check.status in _ORDER),
        key=lambda check: _ORDER[check.status],
    )
    text = _render(report, listed, max_rows, samples=True)
    if len(text) <= max_chars:
        return text
    rows = max_rows
    samples = False
    while True:
        text = _render(report, listed, rows, samples=samples).rstrip("\n") + "\n" + _NOTE
        if len(text) <= max_chars or rows == 0:
            return text[:max_chars]
        rows //= 2
