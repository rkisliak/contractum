"""The import fence bites: a planted forbidden import fails lint-imports."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
CONTRACT_NAME = "contractum imports no server code"


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


def _synthetic_tree(root: Path, forbidden: list[str], body: str) -> Path:
    package = root / "contractum"
    package.mkdir()
    (package / "__init__.py").write_text(body)
    lines = [
        "[tool.importlinter]",
        'root_packages = ["contractum"]',
        "include_external_packages = true",
        "",
        "[[tool.importlinter.contracts]]",
        f'name = "{CONTRACT_NAME}"',
        'type = "forbidden"',
        'source_modules = ["contractum"]',
        "forbidden_modules = [" + ", ".join(f'"{m}"' for m in forbidden) + "]",
    ]
    config = root / "pyproject.toml"
    config.write_text("\n".join(lines) + "\n")
    return config


def _real_forbidden() -> list[str]:
    config = tomllib.loads((PROJECT / "pyproject.toml").read_text())
    contract = config["tool"]["importlinter"]["contracts"][0]
    assert contract["name"] == CONTRACT_NAME
    return list(contract["forbidden_modules"])


def test_the_real_tree_passes() -> None:
    done = _lint(PROJECT, str(PROJECT / "src"))
    assert done.returncode == 0, done.stdout + done.stderr
    assert CONTRACT_NAME in done.stdout


def test_a_planted_server_import_fails(tmp_path: Path) -> None:
    _synthetic_tree(tmp_path, _real_forbidden(), "import fastapi\n")
    done = _lint(tmp_path, str(tmp_path))
    assert done.returncode != 0
    assert CONTRACT_NAME in done.stdout


def test_the_planted_import_passes_when_the_forbidden_list_is_empty(tmp_path: Path) -> None:
    _synthetic_tree(tmp_path, [], "import fastapi\n")
    done = _lint(tmp_path, str(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
