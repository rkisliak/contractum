"""run_env, the shared body of every service's ``migrations/env.py`` (SQLite, no Postgres needed).

A throwaway Alembic script directory holds an ``env.py`` that calls ``run_env`` and one revision
that creates a table, so each branch runs through real Alembic: offline SQL, an injected
connection and a short-lived engine built from the URL.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from dc_core.migrations import run_alembic

pytestmark = pytest.mark.unit

_ENV = """
import os

import sqlalchemy as sa
from dc_core.migrations import run_env


def _url() -> str:
    url = os.environ.get("DC_PROBE_URL")
    if url is None:
        raise AssertionError("url_factory must not be called")
    return url


run_env(metadata=sa.MetaData(), default_version_table="alembic_version_probe", url_factory=_url)
"""

_REVISION = """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("probe", sa.Column("id", sa.Integer, primary_key=True))


def downgrade() -> None:
    op.drop_table("probe")
"""


@pytest.fixture
def scripts(tmp_path: Path) -> Path:
    root = tmp_path / "migrations"
    (root / "versions").mkdir(parents=True)
    (root / "env.py").write_text(_ENV, encoding="utf-8")
    (root / "versions" / "0001_probe.py").write_text(_REVISION, encoding="utf-8")
    return root


def _config(scripts: Path, *, url: str | None = None) -> tuple[Config, io.StringIO]:
    out = io.StringIO()
    config = Config(output_buffer=out)
    config.set_main_option("script_location", str(scripts))
    if url is not None:
        config.set_main_option("sqlalchemy.url", url)
    return config, out


def _tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_offline_sql_uses_the_configured_url_and_the_default_version_table(
    scripts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DC_PROBE_URL", raising=False)
    config, out = _config(scripts, url="sqlite://")
    command.upgrade(config, "head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE probe" in sql
    assert "CREATE TABLE alembic_version_probe" in sql
    assert "INSERT INTO alembic_version_probe (version_num) VALUES ('0001')" in sql


def test_offline_sql_reads_the_url_factory_and_the_version_table_attribute(
    scripts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DC_PROBE_URL", "sqlite://")
    config, out = _config(scripts)
    config.attributes["version_table"] = "alembic_version_custom"
    command.upgrade(config, "head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE alembic_version_custom" in sql
    assert "alembic_version_probe" not in sql


def test_online_upgrade_runs_on_an_injected_connection(
    scripts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DC_PROBE_URL", raising=False)
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        config, _ = _config(scripts)
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
        assert {"probe", "alembic_version_probe"} <= set(sa.inspect(connection).get_table_names())
        version = connection.execute(sa.text("SELECT version_num FROM alembic_version_probe"))
        assert version.scalar_one() == "0001"
    engine.dispose()


def test_online_upgrade_and_downgrade_build_their_own_engine_from_the_url(
    scripts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DC_PROBE_URL", raising=False)
    url = f"sqlite:///{tmp_path / 'probe.db'}"
    run_alembic(
        script_location=str(scripts), version_table="alembic_version_x", url=url, command="upgrade"
    )
    engine = sa.create_engine(url)
    assert {"probe", "alembic_version_x"} <= _tables(engine)
    run_alembic(
        script_location=str(scripts),
        version_table="alembic_version_x",
        url=url,
        command="downgrade",
        revision="base",
    )
    assert "probe" not in _tables(engine)
    engine.dispose()
