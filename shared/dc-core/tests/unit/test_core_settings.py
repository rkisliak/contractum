"""``CoreSettings``: Docker secret files and the SQLAlchemy URL built from its parts."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from dc_core.settings import CoreSettings
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.unit


def test_a_secret_file_supplies_the_password_and_the_url_is_built_from_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = uuid.uuid4().hex + "%@/"  # characters that need escaping in a URL
    (tmp_path / "db_password").write_text(generated)
    monkeypatch.setattr(CoreSettings, "secrets_dir", str(tmp_path))
    settings = CoreSettings(service="core", db_host="db", db_name="core", db_user="app")
    assert settings.db_password is not None
    assert settings.db_password.get_secret_value() == generated
    url = make_url(settings.sqlalchemy_url())
    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.password, url.host, url.port, url.database) == (
        "app",
        generated,
        "db",
        5432,
        "core",
    )


def test_without_a_secrets_directory_the_url_has_no_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(CoreSettings, "secrets_dir", str(tmp_path / "missing"))
    settings = CoreSettings(service="core", db_name="core", db_user="app")
    url = make_url(settings.sqlalchemy_url())
    assert url.password is None
    assert (url.host, url.database) == ("postgres", "core")
