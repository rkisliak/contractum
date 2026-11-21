"""``contractum test``: test a dataset against a contract and report the result.

The exit code is the verdict: 0 when every evaluated check passed, 1 when the data does not
match (a failed check, or a warning with ``--strict``, a skip with ``--fail-on-skipped``), and 2
for any usage, auth, network, server, integrity, lock, data-load or internal error.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import click

from contractum._atomic import write_atomic
from contractum.cli import output
from contractum.cli.context import get_options, lifecycle_options
from contractum.cli.options import check_options, dataset_options, test_options_from
from contractum.cli.render import render_report
from contractum.cli.source import contract_source_options, resolve_contract
from contractum.errors import UsageError

if TYPE_CHECKING:
    from contractum.report.model import Report

F = TypeVar("F", bound=Callable[..., Any])

_SOURCE_KEYS = ("contract", "contract_file", "version", "lock_path", "frozen", "offline")
STDOUT = "-"


def report_options(f: F) -> F:
    """Add ``--json PATH|-``, ``--junit PATH`` and ``--markdown PATH``."""
    path = click.Path(dir_okay=False, path_type=Path)
    f = click.option(
        "--markdown",
        "markdown_path",
        type=path,
        default=None,
        metavar="PATH",
        help="Write a Markdown summary (for pull-request comments) to PATH.",
    )(f)
    f = click.option(
        "--junit",
        "junit_path",
        type=path,
        default=None,
        metavar="PATH",
        help="Write the report as JUnit XML to PATH.",
    )(f)
    f = click.option(
        "--json",
        "json_target",
        default=None,
        metavar="PATH|-",
        help="Write the report as JSON to PATH, or to stdout with - (no table is printed then).",
    )(f)
    return f


def _check_target(target: Path | str | None, flag: str) -> None:
    """Refuse an output path whose folder is missing before any work is done."""
    if target is None or str(target) == STDOUT:
        return
    folder = Path(target).parent
    if not folder.is_dir():
        raise click.UsageError(f"the folder for {flag} does not exist: {folder}")


def _write(path: Path | str, data: bytes, flag: str) -> None:
    try:
        write_atomic(path, data)
    except OSError as exc:
        raise UsageError(
            f"cannot write {flag} file {Path(path).name}: {type(exc).__name__}"
        ) from exc


def _write_reports(
    report: Report, json_target: str | None, junit_path: Path | None, markdown_path: Path | None
) -> None:
    if json_target is not None:
        from contractum.report.json_out import STDOUT as JSON_STDOUT
        from contractum.report.json_out import report_to_json, write_json

        if json_target == JSON_STDOUT:
            write_json(report, json_target)
        else:
            _write(json_target, report_to_json(report).encode("utf-8"), "--json")
    if junit_path is not None:
        from contractum.report.junit import report_to_junit

        _write(junit_path, report_to_junit(report), "--junit")
    if markdown_path is not None:
        from contractum.report.markdown import report_to_markdown

        _write(markdown_path, report_to_markdown(report).encode("utf-8"), "--markdown")


def _summary_line(report: Report, code: int) -> str:
    summary = report.summary
    return (
        f"{report.object}: {summary.passed} passed, {summary.failed} failed, "
        f"{summary.warning} warning, {summary.skipped} skipped, {summary.error} error "
        f"-> exit {code}"
    )


@click.command("test")
@click.argument("data")
@contract_source_options
@lifecycle_options
@dataset_options
@check_options
@report_options
@click.pass_context
def test_cmd(
    ctx: click.Context,
    /,
    data: str,
    json_target: str | None,
    junit_path: Path | None,
    markdown_path: Path | None,
    **params: Any,
) -> int:
    """Test DATA (a file, a Delta table or a SQL DDL file) against a published contract.

    Exit 0 when every check passed, 1 when the data does not match the contract, 2 on any error.
    """
    for flag, target in (
        ("--json", json_target),
        ("--junit", junit_path),
        ("--markdown", markdown_path),
    ):
        _check_target(target, flag)
    options = test_options_from(params)
    version = resolve_contract(ctx, **{key: params[key] for key in _SOURCE_KEYS})
    report = version.test(data, options=options)
    if json_target != STDOUT:
        render_report(report, get_options(ctx), max_failures=options.max_failures)
    _write_reports(report, json_target, junit_path, markdown_path)
    code = report.exit_code(strict=options.strict, fail_on_skipped=options.fail_on_skipped)
    output.info(_summary_line(report, code), get_options(ctx))
    return code


# The name starts with ``test_``; keep pytest from collecting it when a test module imports it.
test_cmd.__test__ = False  # type: ignore[attr-defined]
