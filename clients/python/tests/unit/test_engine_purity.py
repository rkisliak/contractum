"""The purity contract bites: engine, odcs and report import no presentation or network code."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
CONTRACT_NAME = "engine, odcs and report stay pure"


def _contract() -> dict[str, object]:
    config = tomllib.loads((PROJECT / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    return next(item for item in contracts if item["name"] == CONTRACT_NAME)


def _lint(cwd: Path, pythonpath: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": pythonpath}
    return subprocess.run(
        # No config argument: lint-imports reads pyproject.toml from the working directory.
        [str(Path(sys.executable).parent / "lint-imports")],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _synthetic_tree(root: Path, module: str, body: str) -> Path:
    package = root / "contractum"
    package.mkdir()
    (package / "__init__.py").write_text("")
    contract = _contract()
    # Every internal module the contract names must exist for import-linter to resolve it.
    internal = [*contract["source_modules"], *contract["forbidden_modules"]]  # type: ignore[misc]
    for dotted in internal:
        if dotted.startswith("contractum."):
            directory = package / dotted.split(".")[1]
            directory.mkdir(exist_ok=True)
            (directory / "__init__.py").write_text("")
    (package / "engine" / f"{module}.py").write_text(body)
    forbidden = ", ".join(f'"{name}"' for name in contract["forbidden_modules"])  # type: ignore[attr-defined]
    sources = ", ".join(f'"{name}"' for name in contract["source_modules"])  # type: ignore[attr-defined]
    config = root / "pyproject.toml"
    config.write_text(
        "\n".join(
            [
                "[tool.importlinter]",
                'root_packages = ["contractum"]',
                "include_external_packages = true",
                "",
                "[[tool.importlinter.contracts]]",
                f'name = "{CONTRACT_NAME}"',
                'type = "forbidden"',
                f"source_modules = [{sources}]",
                f"forbidden_modules = [{forbidden}]",
                "",
            ]
        )
    )
    return config


def test_the_contract_names_the_presentation_and_network_modules() -> None:
    forbidden = set(_contract()["forbidden_modules"])  # type: ignore[call-overload]

    assert {"click", "rich", "httpx", "keyring"} <= forbidden
    assert {"contractum.cli", "contractum.auth", "contractum.client"} <= forbidden
    assert set(_contract()["source_modules"]) == {  # type: ignore[call-overload]
        "contractum.engine",
        "contractum.odcs",
        "contractum.report",
    }


def test_the_real_tree_keeps_the_contract() -> None:
    done = _lint(PROJECT, str(PROJECT / "src"))

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"{CONTRACT_NAME} KEPT" in done.stdout


@pytest.mark.parametrize("forbidden", ["rich", "click", "httpx"])
def test_a_planted_presentation_import_in_the_engine_breaks_the_contract(
    tmp_path: Path, forbidden: str
) -> None:
    _synthetic_tree(tmp_path, "planted", f"import {forbidden}\n")

    done = _lint(tmp_path, str(tmp_path))

    assert done.returncode != 0
    assert f"{CONTRACT_NAME} BROKEN" in done.stdout


def test_a_clean_engine_module_keeps_the_contract(tmp_path: Path) -> None:
    _synthetic_tree(tmp_path, "clean", "import math\n")

    done = _lint(tmp_path, str(tmp_path))

    assert done.returncode == 0, done.stdout + done.stderr
