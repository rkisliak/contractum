"""No credential reaches stdout, stderr, debug logs, exception messages or the profiles file."""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
import respx
from support.auth import Outcome, run_main
from support.payloads import load_payload

BASE = "https://contractum.example"
ME = "/api/v1/identity/me"
ENTRA_ACCESS = "canary-entra-access-1e0c"
PROMPTED = "canary-prompted-token-3a7e"
LEGACY_REFRESH = "canary-legacy-refresh-2b8e"


@pytest.fixture
def outputs() -> list[str]:
    return []


@pytest.fixture
def run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outputs: list[str],
) -> Callable[..., Outcome]:
    caplog.set_level(logging.DEBUG)

    def invoke(*args: str, stdin: str = "") -> Outcome:
        outcome = run_main(
            ["--url", BASE, *args], capsys=capsys, monkeypatch=monkeypatch, stdin=stdin
        )
        outputs.append(outcome.stdout)
        outputs.append(outcome.stderr)
        return outcome

    return invoke


def _everything(outputs: list[str], caplog: pytest.LogCaptureFixture) -> str:
    logged = [record.getMessage() for record in caplog.records]
    config_file = Path(os.environ["CONTRACTUM_CONFIG"])
    profile_text = config_file.read_text() if config_file.exists() else ""
    return "\n".join([*outputs, *logged, profile_text])


def _data_file() -> Path:
    return Path(os.environ["CONTRACTUM_DATA_DIR"]) / "credentials.json"


def test_no_sign_in_path_leaks_a_credential(
    run: Callable[..., Outcome],
    outputs: list[str],
    caplog: pytest.LogCaptureFixture,
    canary_token: str,
    assert_no_secret: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = (canary_token, ENTRA_ACCESS, PROMPTED, LEGACY_REFRESH)
    me = load_payload("me")

    with respx.mock(base_url=BASE) as router:
        me_route = router.get(ME).respond(200, json={**me, "via": "pat", "read_only": True})
        # a pasted token: stored with mode 0600, shown by nobody
        assert run("login", "--token", "-", stdin=canary_token).code == 0
        stored = _data_file()
        assert canary_token in stored.read_text()
        if os.name == "posix":
            assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        assert run("whoami").code == 0
        assert run("whoami", "--json").code == 0
        assert run("logout").code == 0
        assert canary_token not in stored.read_text()
        # a token on the command line is refused without echoing it
        assert run("login", "--token", canary_token).code == 2

        # a token typed at the hidden terminal prompt
        monkeypatch.setattr("contractum.cli.auth._stdin_is_tty", lambda: True)
        monkeypatch.setattr("click.prompt", lambda *_a, **_k: PROMPTED)
        assert run("login").code == 0
        assert PROMPTED in stored.read_text()
        assert run("logout").code == 0

        # an Entra access token passed through CONTRACTUM_TOKEN is never stored
        me_route.respond(200, json={**me, "via": "jwt", "idp": "entra"})
        monkeypatch.setenv("CONTRACTUM_TOKEN", ENTRA_ACCESS)
        assert run("whoami").code == 0
        assert run("logout").code == 0
        assert ENTRA_ACCESS not in stored.read_text()
        monkeypatch.delenv("CONTRACTUM_TOKEN")

        # an entry an earlier pre-release stored is unusable and removed without being shown
        legacy = {"kind": "local_session", "server": BASE, "access_token": canary_token}
        stored.write_text(json.dumps({"default": {**legacy, "refresh_token": LEGACY_REFRESH}}))
        assert run("whoami").code == 2
        assert run("logout").code == 0
        assert LEGACY_REFRESH not in stored.read_text()

        # profile commands refuse credential-like keys without echoing the value
        assert run("config", "set", "url", BASE).code == 0
        assert run("config", "set", "token", canary_token).code == 2
        assert run("config", "list").code == 0

    assert_no_secret(_everything(outputs, caplog), *secrets)
