"""The ui container entrypoint (``ui/nginx/40-ui-config.sh``), run with ``sh`` and no Docker.

It renders ``config.json`` and the two nginx header files from ``DC_UI_*`` values and must fail the
container start on any value that could weaken the CSP or the nginx configuration (D-UI-08-05,
D-UI-08-07; T-8-12, T-8-15, T-8-23).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "ui" / "nginx" / "40-ui-config.sh"
TENANT_AUTHORITY = "https://login.microsoftonline.com/11111111-2222-4333-8444-555555555555"
ENTRA = {
    "DC_UI_ENTRA_ENABLED": "true",
    "DC_UI_ENTRA_CLIENT_ID": "ui-client-1",
    "DC_UI_ENTRA_AUTHORITY": TENANT_AUTHORITY,
    "DC_UI_ENTRA_PROTOCOL": "aad",
    "DC_UI_ENTRA_SCOPES": "openid profile offline_access",
}


def _run(runtime_dir: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("sh is not installed")
    return subprocess.run(  # noqa: S603 - fixed argv, the script under test
        [sh, str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "DC_UI_RUNTIME_DIR": str(runtime_dir), **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def _csp(headers_file: Path) -> str:
    match = re.search(
        r'add_header Content-Security-Policy "([^"]*)" always;', headers_file.read_text()
    )
    assert match, headers_file.read_text()
    return match.group(1)


def _directives(csp: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in csp.split("; "):
        name, _, value = part.partition(" ")
        parsed[name] = value
    return parsed


def test_defaults_write_a_config_with_entra_off_and_a_csp_with_no_foreign_origin(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    config = json.loads((tmp_path / "config.json").read_text())
    assert config["entraEnabled"] is False
    assert config["localLoginEnabled"] is True
    assert config["apiBase"] == "/api/v1"
    assert set(config) == {
        "entraEnabled",
        "entraClientId",
        "entraAuthority",
        "entraScopes",
        "entraProtocol",
        "localLoginEnabled",
        "apiBase",
        "version",
    }
    assert config["version"] == "dev"
    csp = _csp(tmp_path / "headers.conf")
    assert "http" not in csp
    directives = _directives(csp)
    assert directives["connect-src"] == "'self'"
    assert directives["frame-src"] == "'self'"
    assert directives["form-action"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"


def test_entra_enabled_adds_only_the_authority_origin_to_connect_frame_and_form(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, ENTRA)
    assert result.returncode == 0, result.stderr
    config = json.loads((tmp_path / "config.json").read_text())
    assert config["entraEnabled"] is True
    assert config["entraClientId"] == "ui-client-1"
    assert config["entraAuthority"] == TENANT_AUTHORITY
    assert config["entraProtocol"] == "aad"
    # The UI's runtime-config parser reads the scopes as one space-separated string.
    assert config["entraScopes"] == "openid profile offline_access"
    directives = _directives(_csp(tmp_path / "headers.conf"))
    origin = "https://login.microsoftonline.com"
    for name in ("connect-src", "frame-src", "form-action"):
        assert directives[name] == f"'self' {origin}", name
    assert directives["script-src"] == "'self'"
    assert origin not in directives["default-src"]


def test_an_authority_with_a_port_keeps_the_port_in_the_origin(tmp_path: Path) -> None:
    env = {**ENTRA, "DC_UI_ENTRA_AUTHORITY": "https://localhost:8443/entra"}
    assert _run(tmp_path, env).returncode == 0
    assert _directives(_csp(tmp_path / "headers.conf"))["connect-src"] == (
        "'self' https://localhost:8443"
    )


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("DC_UI_ENTRA_AUTHORITY", "http://login.example.com/tenant"),
        ("DC_UI_ENTRA_AUTHORITY", "login.example.com/tenant"),
        ("DC_UI_ENTRA_AUTHORITY", "https://login.example.com/te'nant"),
        ("DC_UI_ENTRA_AUTHORITY", 'https://login.example.com/te"nant'),
        ("DC_UI_ENTRA_AUTHORITY", "https://login.example.com/a;b"),
        ("DC_UI_ENTRA_AUTHORITY", "https://login.example.com/a b"),
        ("DC_UI_ENTRA_AUTHORITY", "https://login.example.com/a\\b"),
        ("DC_UI_ENTRA_AUTHORITY", "https://login.example.com/a\nadd_header X-Evil 1;"),
        ("DC_UI_ENTRA_AUTHORITY", "https://login.example.com/$host"),
        ("DC_UI_ENTRA_AUTHORITY", "https://user@login.example.com/tenant"),
        ("DC_UI_ENTRA_AUTHORITY", ""),
        ("DC_UI_ENTRA_PROTOCOL", "saml"),
        ("DC_UI_ENTRA_CLIENT_ID", "a/b"),
        ("DC_UI_ENTRA_CLIENT_ID", ""),
        ("DC_UI_ENTRA_CLIENT_ID", "x" * 129),
        ("DC_UI_ENTRA_SCOPES", "openid;profile"),
        ("DC_UI_ENTRA_SCOPES", 'openid"'),
        ("DC_UI_ENTRA_ENABLED", "yes"),
        ("DC_UI_LOCAL_LOGIN_ENABLED", "1"),
        ("DC_UI_API_BASE", "api/v1"),
        ("DC_UI_API_BASE", "//evil.example.com"),
        ("DC_UI_API_BASE", "/api v1"),
        ("DC_UI_API_BASE", "/api/v1;"),
        ("DC_UI_API_BASE", "https://api.example.com/v1"),
        ("DC_UI_VERSION", "1.2.3 beta"),
        ("DC_UI_VERSION", 'v1"'),
        ("DC_UI_VERSION", "v1'"),
        ("DC_UI_VERSION", "1.2.3;x"),
        ("DC_UI_VERSION", "1.2.3$host"),
        ("DC_UI_VERSION", "1.2.3\nadd_header X-Evil 1;"),
        ("DC_UI_VERSION", "1.2.3\\"),
        ("DC_UI_VERSION", "-1.2.3"),
        ("DC_UI_VERSION", ".1"),
        ("DC_UI_VERSION", "1" * 129),
    ],
)
def test_an_invalid_value_fails_the_start_and_writes_nothing(
    tmp_path: Path, variable: str, value: str
) -> None:
    result = _run(tmp_path, {**ENTRA, variable: value})
    assert result.returncode != 0, (variable, value)
    assert list(tmp_path.iterdir()) == [], (variable, value)
    # One line naming the variable, never echoing its value (it may be a secret by mistake).
    assert variable in result.stderr
    if len(value) > 3:
        assert value not in result.stderr


@pytest.mark.parametrize("version", ["dev", "1.4.0", "1.4.0-rc.1", "9.9.9-rc.12", "1.0.0+build.5"])
def test_the_platform_version_lands_in_the_config(tmp_path: Path, version: str) -> None:
    result = _run(tmp_path, {"DC_UI_VERSION": version})
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "config.json").read_text())["version"] == version


def test_an_empty_platform_version_reads_dev(tmp_path: Path) -> None:
    assert _run(tmp_path, {"DC_UI_VERSION": ""}).returncode == 0
    assert json.loads((tmp_path / "config.json").read_text())["version"] == "dev"


def test_the_platform_version_is_the_only_thing_that_changes_the_config(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _run(tmp_path / "a", ENTRA).returncode == 0
    assert _run(tmp_path / "b", {**ENTRA, "DC_UI_VERSION": "2.0.0"}).returncode == 0
    first = json.loads((tmp_path / "a" / "config.json").read_text())
    second = json.loads((tmp_path / "b" / "config.json").read_text())
    assert {k for k in first if first[k] != second[k]} == {"version"}
    for name in ("headers.conf", "headers-bridge.conf"):
        assert (tmp_path / "a" / name).read_text() == (tmp_path / "b" / name).read_text()


def test_a_bad_value_is_refused_even_while_entra_is_disabled(tmp_path: Path) -> None:
    env = {"DC_UI_ENTRA_ENABLED": "false", "DC_UI_API_BASE": "api"}
    assert _run(tmp_path, env).returncode != 0
    assert list(tmp_path.iterdir()) == []


def test_no_written_config_key_names_a_secret(tmp_path: Path) -> None:
    assert _run(tmp_path, ENTRA).returncode == 0
    for key in json.loads((tmp_path / "config.json").read_text()):
        assert "secret" not in key.lower()
    assert not [p for p in tmp_path.iterdir() if "secret" in p.name.lower()]


def test_the_redirect_bridge_header_set_differs_only_by_frame_ancestors_and_coop(
    tmp_path: Path,
) -> None:
    assert _run(tmp_path, ENTRA).returncode == 0
    app_csp = _csp(tmp_path / "headers.conf")
    bridge_csp = _csp(tmp_path / "headers-bridge.conf")
    assert app_csp.endswith("frame-ancestors 'none'")
    assert bridge_csp.endswith("frame-ancestors 'self'")
    assert app_csp.removesuffix("'none'") == bridge_csp.removesuffix("'self'")
    app_lines = (tmp_path / "headers.conf").read_text().splitlines()
    bridge_lines = (tmp_path / "headers-bridge.conf").read_text().splitlines()
    assert any("Cross-Origin-Opener-Policy" in line for line in app_lines)
    assert not any("Cross-Origin-Opener-Policy" in line for line in bridge_lines)
    other = [line for line in app_lines if "Content-Security-Policy" not in line]
    bridge_other = [line for line in bridge_lines if "Content-Security-Policy" not in line]
    assert [line for line in other if "Cross-Origin-Opener-Policy" not in line] == bridge_other


def test_every_header_line_in_both_files_carries_always(tmp_path: Path) -> None:
    assert _run(tmp_path, ENTRA).returncode == 0
    for name in ("headers.conf", "headers-bridge.conf"):
        for line in (tmp_path / name).read_text().splitlines():
            assert line.startswith("add_header ") and line.endswith(" always;"), (name, line)
