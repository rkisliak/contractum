"""The terminal view of a report: a rich table on a terminal, aligned plain text otherwise.

Every cell is built as rich ``Text`` so a dataset value that looks like markup is shown as it is.
A status is always written as a word (PASS, FAIL, WARN, SKIP, ERROR); colour only adds to it.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import click

from contractum.cli.context import GlobalOptions
from contractum.cli.output import stdout_console
from contractum.report.model import CheckResult, Report
from contractum.report.text import STATUS_WORDS, failing_text, sample_lines

if TYPE_CHECKING:
    from rich.console import Console

HEADERS = ("Status", "Check", "Column", "Rule", "Failing", "Details")
_STYLES = {
    "passed": "green",
    "failed": "bold red",
    "warning": "yellow",
    "skipped": "dim",
    "error": "bold red",
}


def _is_tty() -> bool:
    return sys.stdout.isatty()


def _details(check: CheckResult, max_failures: int) -> str:
    reason = check.message or check.skipped_reason or ""
    shown = sample_lines(check, max_failures)
    hidden = len(check.samples) - len(shown)
    lines = [reason, *shown] if reason else shown
    if hidden > 0:
        lines.append(f"+{hidden} more samples")
    return "\n".join(lines)


def _row(check: CheckResult, max_failures: int) -> tuple[str, ...]:
    return (
        STATUS_WORDS[check.status],
        check.id,
        check.column or "-",
        check.rule,
        failing_text(check),
        _details(check, max_failures),
    )


def footer_lines(report: Report) -> list[str]:
    """Two lines: the contract version that was tested and the counts."""
    contract = report.contract
    label = f"{contract.odcs_id or contract.name or 'contract'}@{contract.semver or '-'}"
    status = f" ({contract.status})" if contract.status else ""
    summary = report.summary
    counts = (
        f"{summary.passed} passed, {summary.failed} failed, {summary.warning} warning, "
        f"{summary.skipped} skipped, {summary.error} error"
    )
    lines = [f"contract {label}{status}, object {report.object}", counts]
    if summary.total - summary.skipped == 0:
        lines.append("no checks were evaluated")
    return lines


def _plain_lines(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(cells[i].split("\n")[0]) for cells in (HEADERS, *rows)) for i in range(5)]
    lines: list[str] = []
    for cells in (HEADERS, *rows):
        head, _, rest = cells[5].partition("\n")
        fixed = "  ".join(cell.ljust(width) for cell, width in zip(cells[:5], widths, strict=True))
        lines.append(f"{fixed}  {head}".rstrip())
        indent = " " * (sum(widths) + 2 * 5)
        lines.extend(f"{indent}{extra}" for extra in rest.split("\n") if rest)
    return lines


def _rich_table(rows: list[tuple[str, ...]], statuses: list[str]) -> object:
    from rich.table import Table
    from rich.text import Text

    table = Table(header_style="bold")
    for index, header in enumerate(HEADERS):
        table.add_column(header, overflow="fold", no_wrap=index == 0)
    for cells, status in zip(rows, statuses, strict=True):
        table.add_row(Text(cells[0], style=_STYLES[status]), *(Text(cell) for cell in cells[1:]))
    return table


def render_report(
    report: Report,
    options: GlobalOptions,
    *,
    max_failures: int,
    console: Console | None = None,
) -> None:
    """Print every check and the footer to stdout.

    A terminal gets a rich table; anything else gets aligned plain text, so logs stay readable.
    """
    rows = [_row(check, max_failures) for check in report.checks]
    if not rows:
        click.echo("\n".join(footer_lines(report)))
        return
    if console is None and not _is_tty():
        click.echo("\n".join([*_plain_lines(rows), "", *footer_lines(report)]))
        return
    target = console or stdout_console(options)
    target.print(_rich_table(rows, [check.status for check in report.checks]))
    from rich.text import Text

    for line in footer_lines(report):
        target.print(Text(line))
