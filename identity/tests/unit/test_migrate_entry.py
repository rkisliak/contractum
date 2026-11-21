"""``python -m dc_identity.migrate`` hands the identity script location and table to the CLI."""

from __future__ import annotations

import runpy
import sys
from typing import Any

import dc_core.migrations
import pytest
from dc_identity.settings import IdentitySettings

pytestmark = pytest.mark.unit


def test_running_the_module_as_a_script_calls_the_shared_cli_with_identity_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dc_core.migrations, "migration_cli", lambda **kw: calls.append(kw))
    # A fresh execution of the module, as ``python -m`` does (no stale-module warning).
    monkeypatch.delitem(sys.modules, "dc_identity.migrate", raising=False)
    runpy.run_module("dc_identity.migrate", run_name="__main__")
    assert calls == [
        {
            "script_location": "dc_identity:migrations",
            "version_table": "alembic_version_identity",
            "settings_factory": IdentitySettings,
        }
    ]
