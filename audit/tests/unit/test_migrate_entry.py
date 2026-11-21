"""``python -m dc_audit.migrate`` hands the audit script location and version table to the CLI."""

from __future__ import annotations

import runpy
import sys
from typing import Any

import dc_core.migrations
import pytest
from dc_audit import migrate
from dc_audit.settings import AuditSettings

pytestmark = pytest.mark.unit


def _recorder(monkeypatch: pytest.MonkeyPatch, target: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(target, "migration_cli", lambda **kwargs: calls.append(kwargs))
    return calls


EXPECTED = {
    "script_location": "dc_audit:migrations",
    "version_table": "alembic_version_audit",
    "settings_factory": AuditSettings,
}


def test_main_runs_the_shared_migration_cli_with_the_audit_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _recorder(monkeypatch, migrate)
    migrate.main()
    assert calls == [EXPECTED]


def test_running_the_module_as_a_script_calls_main(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch, dc_core.migrations)
    # A fresh execution of the module, as ``python -m`` does (no stale-module warning).
    monkeypatch.delitem(sys.modules, "dc_audit.migrate")
    runpy.run_module("dc_audit.migrate", run_name="__main__")
    assert calls == [EXPECTED]
