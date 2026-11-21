"""Shared fixtures: path-based markers, a canary token and an isolated environment."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

# The pytester plugin runs a real pytest session inside the plugin tests (test_pytest_plugin.py).
pytest_plugins = ["pytester"]

_TESTS = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark tests under tests/unit as unit and under tests/integration as integration."""
    for item in items:
        parents = Path(str(item.path)).parents
        if _TESTS / "unit" in parents:
            item.add_marker(pytest.mark.unit)
        elif _TESTS / "integration" in parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def canary_token() -> str:
    """A token-shaped secret that tests plant and then prove never appears in any output."""
    return "dcp_" + "0123456789abcdef" + "_" + "C" * 43


@pytest.fixture
def assert_no_secret() -> Callable[..., None]:
    """Return a checker that fails with the offset of a leaked secret, never the secret."""

    def check(text: str, *secrets: str) -> None:
        for index, secret in enumerate(secrets):
            position = text.find(secret)
            if position >= 0:
                pytest.fail(f"secret #{index} leaked at offset {position}")

    return check


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config, data and cache at temp directories and drop every ambient setting."""
    monkeypatch.setenv("CONTRACTUM_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("CONTRACTUM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONTRACTUM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CONTRACTUM_KEYRING", "0")
    for name in (
        "CONTRACTUM_TOKEN",
        "CONTRACTUM_URL",
        "CONTRACTUM_PROFILE",
        "CONTRACTUM_FROZEN",
        "CONTRACTUM_DEBUG",
    ):
        monkeypatch.delenv(name, raising=False)
