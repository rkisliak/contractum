"""Token sign-in against the real stack: login --token -, whoami, logout, and an Entra token."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support.integration import World, cli_env, run_cli

from contractum.auth.store import SecretStore
from contractum.config import DEFAULT_PROFILE


@pytest.fixture
def isolated_env(it_world: World, tmp_path: Path) -> dict[str, str]:
    """A CLI environment with an isolated profile, credential store and no ambient token."""
    return cli_env(
        CONTRACTUM_URL=it_world.url,
        CONTRACTUM_CA_BUNDLE=str(it_world.ca),
        CONTRACTUM_CONFIG=str(tmp_path / "config"),
        CONTRACTUM_DATA_DIR=str(tmp_path / "data"),
        CONTRACTUM_KEYRING="0",
        CONTRACTUM_TOKEN=None,
    )


def _store(data_dir: Path) -> SecretStore:
    return SecretStore(env={"CONTRACTUM_KEYRING": "0", "CONTRACTUM_DATA_DIR": str(data_dir)})


def test_login_with_a_pat_then_whoami_then_logout(
    reader_pat: str, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    login = run_cli("login", "--token", "-", env=isolated_env, stdin=reader_pat + "\n")
    assert login.returncode == 0, login.stderr
    assert "via pat" in login.stderr

    whoami = run_cli("whoami", "--json", env=isolated_env)
    assert whoami.returncode == 0, whoami.stderr
    details = json.loads(whoami.stdout)
    assert details["via"] == "pat"
    assert details["read_only"] is True
    assert details["credential_source"].startswith("stored PAT dcp_")

    stored = _store(tmp_path / "data").get(DEFAULT_PROFILE)
    assert stored is not None
    assert stored.kind == "pat"

    logout = run_cli("logout", env=isolated_env)
    assert logout.returncode == 0, logout.stderr
    assert "removed the stored credential" in logout.stderr
    assert "stays valid until you revoke it" in logout.stderr
    assert _store(tmp_path / "data").get(DEFAULT_PROFILE) is None

    after = run_cli("whoami", env=isolated_env)
    assert after.returncode == 2
    assert "contractum login --token -" in after.stderr

    for result in (login, whoami, logout, after):
        assert reader_pat not in result.stdout + result.stderr


def test_login_with_an_entra_token_stores_the_entra_kind(
    entra_token: str, isolated_env: dict[str, str], tmp_path: Path
) -> None:
    login = run_cli("login", "--token", "-", env=isolated_env, stdin=entra_token)
    assert login.returncode == 0, login.stderr

    whoami = run_cli("whoami", "--json", env=isolated_env)
    assert whoami.returncode == 0, whoami.stderr
    details = json.loads(whoami.stdout)
    assert details["idp"] == "entra"
    assert details["credential_source"] == "stored Entra token"

    stored = _store(tmp_path / "data").get(DEFAULT_PROFILE)
    assert stored is not None
    assert stored.kind == "entra_token"

    logout = run_cli("logout", env=isolated_env)
    assert logout.returncode == 0, logout.stderr
    assert "the token itself stays valid until it expires" in logout.stderr
    for result in (login, whoami, logout):
        assert entra_token not in result.stdout + result.stderr
