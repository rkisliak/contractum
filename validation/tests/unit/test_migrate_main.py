"""``python -m dc_validation.migrate`` runs the shared CLI with validation's own version table."""

from __future__ import annotations

import runpy
import sys
from typing import Any

import dc_core.migrations
import pytest
from dc_validation import migrate
from dc_validation.settings import ValidationSettings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("as_script", [False, True], ids=["main", "python-m"])
def test_the_entry_point_hands_validation_settings_to_the_shared_cli(
    monkeypatch: pytest.MonkeyPatch, as_script: bool
) -> None:
    calls: list[dict[str, Any]] = []
    # As a script the module is executed afresh and imports the CLI again, so patch its source.
    monkeypatch.setattr(
        dc_core.migrations if as_script else migrate,
        "migration_cli",
        lambda **kwargs: calls.append(kwargs),
    )
    if as_script:
        monkeypatch.delitem(sys.modules, "dc_validation.migrate")
        runpy.run_module("dc_validation.migrate", run_name="__main__")
    else:
        migrate.main()
    assert calls == [
        {
            "script_location": "dc_validation:migrations",
            "version_table": "alembic_version_validation",
            "settings_factory": ValidationSettings,
        }
    ]
