"""``contractum compare``: test a dataset against two versions of a contract.

By default the versions are Latest and the Preview, so the output shows which checks the data
would newly fail once the Preview becomes Latest. The exit code follows the target version: 0
when it passes, 1 when it has a failed check (or a warning with ``--strict``), 2 on any error.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from contractum.cli import output
from contractum.cli.context import GlobalOptions, client_from_ctx, get_options, lifecycle_options
from contractum.cli.options import check_options, dataset_options, test_options_from
from contractum.cli.output import stdout_console
from contractum.cli.run import STDOUT, _check_target, _write
from contractum.comparison import (
    CheckDelta,
    CompareResult,
    compare_to_json,
    compare_to_markdown,
    diff_lines,
    status_word,
)
from contractum.comparison import compare as compare_versions

if TYPE_CHECKING:
    from rich.console import Console

    from contractum.testing import TestOptions

_STYLES = {"new": "cyan", "changed": "yellow", "removed": "dim", "same": ""}


def _change_text(delta: CheckDelta) -> str:
    return f"{delta.change}, newly failing" if delta.regression else delta.change


def headers(result: CompareResult) -> tuple[str, ...]:
    """The table header: the check, then one status column per version, then the change."""
    return (
        "Check",
        "Column",
        "Rule",
        result.from_semver or "from",
        result.to_semver or "to",
        "Change",
    )


def _row(delta: CheckDelta) -> tuple[str, ...]:
    return (
        delta.id,
        delta.column or "-",
        delta.rule,
        status_word(delta.from_status),
        status_word(delta.to_status),
        _change_text(delta),
    )


def _plain_lines(head: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(cells[index]) for cells in (head, *rows)) for index in range(len(head) - 1)]
    lines: list[str] = []
    for cells in (head, *rows):
        fixed = "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=False))
        lines.append(f"{fixed}  {cells[-1]}".rstrip())
    return lines


def _print_rich(
    console: Console, head: tuple[str, ...], rows: list[tuple[str, ...]], deltas: list[CheckDelta]
) -> None:
    from rich.table import Table
    from rich.text import Text

    table = Table(header_style="bold")
    for index, title in enumerate(head):
        table.add_column(title, overflow="fold", no_wrap=index >= 3)
    for cells, delta in zip(rows, deltas, strict=True):
        style = "bold red" if delta.regression else _STYLES[delta.change]
        table.add_row(*(Text(cell) for cell in cells[:-1]), Text(cells[-1], style=style))
    console.print(table)


def _footer_lines(result: CompareResult) -> list[str]:
    to_label = result.to_semver or "the target version"
    noun = "check" if result.newly_failing == 1 else "checks"
    lines = [f"{result.newly_failing} {noun} newly failing in {to_label}"]
    changed = len(result.shown)
    lines.append(f"{changed} differ, {result.unchanged} unchanged ({len(result.deltas)} checks)")
    lines.extend(diff_lines(result))
    return lines


def render_compare(
    result: CompareResult, options: GlobalOptions, *, console: Console | None = None
) -> None:
    """Print the delta table (rich on a terminal, aligned text otherwise) and the footer."""
    shown = result.shown
    rows = [_row(delta) for delta in shown]
    head = headers(result)
    footer = _footer_lines(result)
    if not rows:
        click.echo("\n".join([f"no check changed status between {head[3]} and {head[4]}", *footer]))
        return
    if console is None and not sys.stdout.isatty():
        click.echo("\n".join([*_plain_lines(head, rows), "", *footer]))
        return
    target = console or stdout_console(options)
    _print_rich(target, head, rows, shown)
    from rich.text import Text

    for line in footer:
        target.print(Text(line))


def _summary_line(result: CompareResult, code: int) -> str:
    return (
        f"{result.ref}: {result.from_semver or '-'} -> {result.to_semver or '-'}: "
        f"{result.newly_failing} newly failing, {len(result.shown)} differ, "
        f"{result.unchanged} unchanged -> exit {code}"
    )


def _run(
    ctx: click.Context, data: str, contract: str, from_v: str, to_v: str, **params: Any
) -> tuple[CompareResult, TestOptions]:
    options = test_options_from(params)
    with client_from_ctx(ctx) as client, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        result = compare_versions(
            client, contract, data, from_version=from_v, to_version=to_v, options=options
        )
    for item in caught:
        output.warn(str(item.message))
    return result, options


@click.command("compare")
@click.argument("data")
@click.option(
    "--contract",
    "contract",
    metavar="REF",
    required=True,
    help="The contract: [FOLDER-PATH:]ODCS-ID-OR-UUID.",
)
@click.option(
    "--from",
    "from_version",
    metavar="VERSION",
    default="latest",
    show_default=True,
    help="The version to compare from: latest, preview or an exact SemVer.",
)
@click.option(
    "--to",
    "to_version",
    metavar="VERSION",
    default="preview",
    show_default=True,
    help="The version to compare to: latest, preview or an exact SemVer.",
)
@lifecycle_options
@dataset_options
@check_options
@click.option(
    "--markdown",
    "markdown_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    metavar="PATH",
    help="Write a Markdown summary (for pull-request comments) to PATH.",
)
@click.option(
    "--json",
    "json_target",
    default=None,
    metavar="PATH|-",
    help="Write the comparison as JSON to PATH, or to stdout with - (no table is printed then).",
)
@click.pass_context
def compare(
    ctx: click.Context,
    /,
    data: str,
    contract: str,
    from_version: str,
    to_version: str,
    json_target: str | None,
    markdown_path: Path | None,
    **params: Any,
) -> int:
    """Test DATA against two versions of a contract and show which checks changed.

    Exit 0 when the target version passes, 1 when it has a failed check, 2 on any error.
    """
    _check_target(json_target, "--json")
    _check_target(markdown_path, "--markdown")
    result, options = _run(ctx, data, contract, from_version, to_version, **params)
    if json_target != STDOUT:
        render_compare(result, get_options(ctx))
    if json_target is not None:
        text = compare_to_json(result)
        if json_target == STDOUT:
            click.echo(text, nl=False)
        else:
            _write(json_target, text.encode("utf-8"), "--json")
    if markdown_path is not None:
        _write(markdown_path, compare_to_markdown(result).encode("utf-8"), "--markdown")
    code = result.exit_code(strict=options.strict, fail_on_skipped=options.fail_on_skipped)
    output.info(_summary_line(result, code), get_options(ctx))
    return code
