"""run_alembic dispatch and migration_cli argument handling (no database needed)."""

from __future__ import annotations

from typing import Any

import dc_core.migrations as migrations
import pytest
from dc_core.settings import CoreSettings

pytestmark = pytest.mark.unit


def _settings() -> CoreSettings:
    return CoreSettings(service="probe", database_url="postgresql+psycopg://u:p%40ss@h/db")


def test_cli_dispatches_upgrade_head_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(migrations, "run_alembic", lambda **kw: calls.append(kw))
    migrations.migration_cli(
        script_location="pkg:migrations",
        version_table="alembic_version_x",
        settings_factory=_settings,
        argv=["upgrade"],
    )
    assert calls == [
        {
            "script_location": "pkg:migrations",
            "version_table": "alembic_version_x",
            "url": "postgresql+psycopg://u:p%40ss@h/db",
            "command": "upgrade",
            "revision": "head",
        }
    ]


def test_cli_passes_explicit_revision_and_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(migrations, "run_alembic", lambda **kw: calls.append(kw))
    for argv in (["downgrade", "-1"], ["check"], ["current"]):
        migrations.migration_cli(
            script_location="p:m", version_table="v", settings_factory=_settings, argv=argv
        )
    assert [(c["command"], c["revision"]) for c in calls] == [
        ("downgrade", "-1"),
        ("check", "head"),
        ("current", "head"),
    ]


@pytest.mark.parametrize("argv", [[], ["explode"], ["downgrade"], ["upgrade", "a", "b"]])
def test_cli_rejects_bad_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        migrations.migration_cli(
            script_location="p:m", version_table="v", settings_factory=_settings, argv=argv
        )
    assert info.value.code == 2
