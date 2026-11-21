"""The public surface: every exported name imports, the CLI lists its ten commands, types ship."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import click
import pytest

import contractum
from contractum import cli
from contractum.cli import COMMANDS

PROJECT = Path(__file__).resolve().parents[2]
COMMAND_NAMES = [
    "compare",
    "config",
    "list",
    "login",
    "logout",
    "pull",
    "show",
    "test",
    "versions",
    "whoami",
]


@pytest.mark.parametrize("name", contractum.__all__)
def test_every_exported_name_resolves(name: str) -> None:
    assert getattr(contractum, name) is not None


def test_all_equals_the_sorted_export_table() -> None:
    assert contractum.__all__ == sorted(["__version__", *contractum._EXPORTS])
    assert len(contractum.__all__) == len(set(contractum.__all__))


def test_documented_imports_work() -> None:
    from contractum import (
        Client,
        ContractVersion,
        Report,
        assert_conforms,
        compare,
        run_test,
    )

    assert callable(compare)
    assert callable(run_test)
    assert callable(assert_conforms)
    assert {Client.__name__, ContractVersion.__name__, Report.__name__} == {
        "Client",
        "ContractVersion",
        "Report",
    }


def test_compare_is_the_function_even_after_its_module_was_imported() -> None:
    code = (
        "import contractum.comparison; from contractum import compare; "
        "print(type(compare).__name__)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "function"


def test_the_cli_lists_exactly_the_ten_commands() -> None:
    assert sorted(COMMANDS) == COMMAND_NAMES
    assert len(COMMAND_NAMES) == 10
    assert cli.cli.list_commands(click.Context(cli.cli)) == COMMAND_NAMES


@pytest.mark.parametrize("name", COMMAND_NAMES)
def test_each_command_target_is_a_click_command(name: str) -> None:
    command = cli.cli.get_command(click.Context(cli.cli), name)
    assert isinstance(command, click.Command)
    assert command.name == name


def test_the_package_marks_itself_typed() -> None:
    marker = Path(contractum.__file__).parent / "py.typed"
    assert marker.is_file()
    assert marker.read_bytes() == b""


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is needed to build the wheel")
def test_the_built_wheel_contains_the_typed_marker(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=True,
    )
    (wheel,) = tmp_path.glob("contractum-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    assert "contractum/py.typed" in names
    assert "contractum/__init__.py" in names


def test_mypy_sees_the_typed_public_exports(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import contractum\n"
        "from contractum import Client, compare\n"
        "reveal_type(Client)\n"
        "reveal_type(compare)\n"
        "reveal_type(contractum.NotFoundError)\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--cache-dir", str(tmp_path / "cache")]
        + [str(probe)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    revealed = [line for line in done.stdout.splitlines() if "Revealed type" in line]
    assert len(revealed) == 3, done.stdout
    assert revealed[0].endswith('-> contractum.client.Client"')
    assert revealed[1].endswith('-> contractum.comparison.CompareResult"')
    assert revealed[2].endswith('-> contractum.errors.NotFoundError"')
    assert "Any" not in "".join(revealed)
    assert "missing library stubs" not in done.stdout
