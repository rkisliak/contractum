"""The contracts migration entry point runs the shared CLI with contracts' own version table."""

from __future__ import annotations

import runpy
import sys
from typing import Any

import dc_core.migrations
import pytest
from dc_contracts import migrate
from dc_contracts.settings import ContractsSettings

pytestmark = pytest.mark.unit

EXPECTED = {
    "script_location": "dc_contracts:migrations",
    "version_table": "alembic_version_contracts",
    "settings_factory": ContractsSettings,
}


def recorder(calls: list[dict[str, Any]]) -> Any:
    def migration_cli(**kwargs: Any) -> None:
        calls.append(kwargs)

    return migration_cli


def test_main_passes_contracts_settings_and_version_table(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(migrate, "migration_cli", recorder(calls))

    migrate.main()

    assert calls == [EXPECTED]


def test_python_dash_m_runs_main(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    # Run as a script the module is executed again and re-imports the CLI from its source module.
    monkeypatch.setattr(dc_core.migrations, "migration_cli", recorder(calls))
    monkeypatch.delitem(sys.modules, "dc_contracts.migrate")

    runpy.run_module("dc_contracts.migrate", run_name="__main__")

    assert calls == [EXPECTED]
