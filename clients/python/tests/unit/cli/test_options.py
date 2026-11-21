"""The dataset and check option decorators and the TestOptions they build."""

from __future__ import annotations

from typing import Any

import click
import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from contractum.cli import options as cli_options
from contractum.cli.context import lifecycle_options
from contractum.cli.options import SampleSize, check_options, dataset_options
from contractum.testing import TestOptions


def _run(*args: str) -> tuple[int, TestOptions | None, str]:
    captured: dict[str, TestOptions] = {}

    @click.command()
    @dataset_options
    @check_options
    @lifecycle_options
    def command(**params: Any) -> None:
        captured["options"] = cli_options.test_options_from(params)

    result = CliRunner().invoke(command, list(args))
    return result.exit_code, captured.get("options"), result.output


def _options(*args: str) -> TestOptions:
    code, built, output = _run(*args)
    assert code == 0, output
    assert built is not None
    return built


def test_defaults_equal_the_test_options_defaults() -> None:
    assert _options() == TestOptions()


def test_a_sample_row_count_is_an_int() -> None:
    built = _options("--sample", "100")

    assert built.sample == 100
    assert isinstance(built.sample, int)


def test_a_sample_fraction_is_a_float() -> None:
    built = _options("--sample", "0.25")

    assert built.sample == 0.25
    assert isinstance(built.sample, float)


def test_one_is_a_row_and_one_point_zero_is_everything() -> None:
    assert isinstance(_options("--sample", "1").sample, int)
    assert isinstance(_options("--sample", "1.0").sample, float)
    assert _options("--sample", "1e-1").sample == 0.1


@pytest.mark.parametrize(
    "args",
    [
        ("--sample", "0"),
        ("--sample", "1.5"),
        ("--sample", "0.0"),
        ("--sample", "-1"),
        ("--sample", "abc"),
        ("--sample", "nan"),
        ("--sample", "inf"),
        ("--sample", "1e"),
        ("--sample", "1_0"),
        ("--sample", ""),
        ("--sample", "١٢"),
        ("--max-failures", "-1"),
        ("--max-failures", "x"),
        ("--limit", "0"),
        ("--limit", "-5"),
        ("--seed", "-1"),
        ("--mask", "bogus"),
        ("--extra", "bogus"),
        ("--format", "xlsx"),
        ("--sunset-within", "-1"),
    ],
)
def test_invalid_values_exit_two(args: tuple[str, ...]) -> None:
    code, built, output = _run(*args)

    assert code == 2
    assert built is None
    assert "Usage" in output or "Error" in output


def test_every_option_lands_in_the_test_options() -> None:
    built = _options(
        "--format",
        "csv",
        "--object",
        "orders",
        "--schema-only",
        "--dialect",
        "postgres",
        "--extra",
        "fail",
        "--ignore-case",
        "--strict-types",
        "--sample",
        "50",
        "--seed",
        "7",
        "--limit",
        "1000",
        "--max-failures",
        "0",
        "--mask",
        "all",
        "--no-samples",
        "--strict",
        "--fail-on-skipped",
    )

    assert built == TestOptions(
        format="csv",
        object_name="orders",
        schema_only=True,
        dialect="postgres",
        extra="fail",
        ignore_case=True,
        strict_types=True,
        sample=50,
        seed=7,
        limit=1000,
        max_failures=0,
        mask="all",
        samples=False,
        strict=True,
        fail_on_skipped=True,
    )


def test_lifecycle_flags_land_in_the_test_options() -> None:
    built = _options("--fail-on-deprecated", "--sunset-within", "30")

    assert built.fail_on_deprecated is True
    assert built.sunset_within_days == 30


def test_ddl_is_an_accepted_format_for_later_plans() -> None:
    assert _options("--format", "DDL").format == "ddl"


def test_test_options_from_ignores_unknown_keys() -> None:
    built = cli_options.test_options_from({"limit": 5, "profile": "x", "verbose": True})

    assert built.limit == 5


def test_sample_size_converts_numbers_given_by_python_callers() -> None:
    size = SampleSize()

    assert size.convert(5, None, None) == 5
    assert size.convert(0.5, None, None) == 0.5
    with pytest.raises(click.BadParameter):
        size.convert(0, None, None)
    with pytest.raises(click.BadParameter):
        size.convert(True, None, None)


def test_the_helper_is_not_collected_as_a_test() -> None:
    assert cli_options.test_options_from.__test__ is False  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample": 0},
        {"sample": -3},
        {"sample": 1.5},
        {"sample": 0.0},
        {"sample": float("nan")},
        {"sample": True},
        {"limit": 0},
        {"max_failures": -1},
        {"seed": -1},
        {"sunset_within_days": -1},
        {"extra": "bogus"},
        {"mask": "bogus"},
        {"unknown": 1},
    ],
)
def test_test_options_refuse_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TestOptions(**kwargs)


def test_test_options_are_frozen_and_never_collected_by_pytest() -> None:
    built = TestOptions(sample=0.5)

    with pytest.raises(ValidationError):
        built.limit = 3  # type: ignore[misc]
    assert TestOptions.__test__ is False
    assert TestOptions(sample=1).sample == 1
    assert TestOptions(sample=1.0).sample == 1.0
