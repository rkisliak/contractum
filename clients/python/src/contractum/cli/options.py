"""Option decorators shared by ``test`` and ``compare``, and the ``TestOptions`` they build.

The decorators add click options with ranges enforced by click, so a bad value exits with code 2
before any data is read. ``test_options_from`` turns the parsed keyword arguments into a
``TestOptions``; the lifecycle flags come from ``lifecycle_options`` in ``cli.context``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

import click

from contractum.testing import TestOptions

F = TypeVar("F", bound=Callable[..., Any])

_DIGITS = frozenset("0123456789")
_NUMBER_CHARS = frozenset("0123456789.eE+-")
_FORMATS = ("parquet", "csv", "tsv", "json", "ndjson", "delta", "ddl")


class SampleSize(click.ParamType):  # type: ignore[type-arg]
    """``--sample``: a whole number of rows (at least 1) or a fraction above 0 up to 1."""

    name = "N|FRACTION"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return self._checked(value, param, ctx)
        text = str(value).strip()
        if text and set(text) <= _DIGITS:
            return self._checked(int(text), param, ctx)
        if text and set(text) <= _NUMBER_CHARS:
            try:
                return self._checked(float(text), param, ctx)
            except ValueError:
                pass
        return self.fail(f"{value!r} is not a row count or a fraction", param, ctx)

    def _checked(
        self, number: int | float, param: click.Parameter | None, ctx: click.Context | None
    ) -> int | float:
        if isinstance(number, int) and number >= 1:
            return number
        if isinstance(number, float) and 0 < number <= 1:
            return number
        return self.fail(
            "use a row count of at least 1 or a fraction above 0 and at most 1", param, ctx
        )


def dataset_options(f: F) -> F:
    """Add ``--format``, ``--object``, ``--schema-only`` and ``--dialect``."""
    options = [
        click.option(
            "--format",
            "format",
            type=click.Choice(_FORMATS, case_sensitive=False),
            default=None,
            help="Dataset format when the file suffix does not say.",
        ),
        click.option(
            "--object",
            "object_name",
            metavar="NAME",
            default=None,
            help="Schema object to test; needed when the contract has several.",
        ),
        click.option(
            "--schema-only",
            "schema_only",
            is_flag=True,
            default=False,
            help="Check column names and types only, without reading rows.",
        ),
        click.option(
            "--dialect",
            "dialect",
            metavar="NAME",
            default=None,
            help="SQL dialect of a DDL file.",
        ),
    ]
    for option in reversed(options):
        f = option(f)
    return f


def check_options(f: F) -> F:
    """Add the options that shape how checks run and how strictly the result is judged."""
    options = [
        click.option(
            "--extra",
            type=click.Choice(("allow", "warn", "fail")),
            default="warn",
            show_default=True,
            help="What to do with columns the contract does not list.",
        ),
        click.option(
            "--ignore-case",
            "ignore_case",
            is_flag=True,
            default=False,
            help="Match column names without regard to case.",
        ),
        click.option(
            "--strict-types",
            "strict_types",
            is_flag=True,
            default=False,
            help=(
                "Require float or decimal columns for number, and fail (not warn) on values "
                "outside a format width."
            ),
        ),
        click.option(
            "--sample",
            type=SampleSize(),
            default=None,
            help="Test N random rows, or a FRACTION of the rows.",
        ),
        click.option(
            "--seed",
            type=click.IntRange(min=0),
            default=0,
            show_default=True,
            help="Seed of the random sample.",
        ),
        click.option(
            "--limit",
            type=click.IntRange(min=1),
            default=None,
            metavar="N",
            help="Test only the first N rows.",
        ),
        click.option(
            "--max-failures",
            "max_failures",
            type=click.IntRange(min=0),
            default=5,
            show_default=True,
            help="Failing values shown per check.",
        ),
        click.option(
            "--mask",
            type=click.Choice(("auto", "all", "none")),
            default="auto",
            show_default=True,
            help="Hide failing values: auto by classification, all, or none.",
        ),
        click.option(
            "--samples/--no-samples",
            "samples",
            default=True,
            help="Show failing values (--no-samples shows none).",
        ),
        click.option(
            "--strict",
            is_flag=True,
            default=False,
            help="Exit 1 on warnings too.",
        ),
        click.option(
            "--fail-on-skipped",
            "fail_on_skipped",
            is_flag=True,
            default=False,
            help="Exit 1 when a check was skipped.",
        ),
    ]
    for option in reversed(options):
        f = option(f)
    return f


def test_options_from(params: Mapping[str, Any]) -> TestOptions:
    """Build ``TestOptions`` from parsed option values; unknown keys are ignored.

    ``fail_on_deprecated`` and ``sunset_within_days`` come from ``lifecycle_options``.
    """
    names = TestOptions.model_fields
    given = {name: value for name, value in params.items() if name in names}
    return TestOptions(**given)


# The name starts with ``test_``; keep pytest from collecting it when a test module imports it.
test_options_from.__test__ = False  # type: ignore[attr-defined]
