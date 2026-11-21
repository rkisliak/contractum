"""login --token and whoami through the CLI: stored securely, never printed."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import keyring.core
import pytest
import respx
from support.auth import MemoryKeyring, Outcome, run_main
from support.payloads import load_payload

from contractum.auth.store import SecretStore

BASE = "https://contractum.example"
ME = "/api/v1/identity/me"


def _me(**changes: Any) -> dict[str, Any]:
    return {**load_payload("me"), "via": "pat", "read_only": True, **changes}


@pytest.fixture
def run(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Outcome]:
    def invoke(*args: str, input_text: str = "", url: str = BASE) -> Outcome:
        return run_main(
            ["--url", url, *args], capsys=capsys, monkeypatch=monkeypatch, stdin=input_text
        )

    return invoke


def _data_file() -> Path:
    return Path(os.environ["CONTRACTUM_DATA_DIR"]) / "credentials.json"


def test_login_with_a_piped_token_then_whoami_works_without_showing_the_token(
    canary_token: str, assert_no_secret: Callable[..., None], run: Callable[..., Outcome]
) -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.get(ME).respond(200, json=_me())
        login = run("login", "--token", "-", input_text=canary_token + "\n")
        assert login.code == 0, login.output
        who = run("whoami")
        assert who.code == 0, who.output
    assert_no_secret(login.output + who.output, canary_token)
    assert f"signed in to {BASE} as Dana Engineer (user via pat)" in login.stderr
    assert route.call_count == 2
    assert route.calls[0].request.headers["authorization"] == f"Bearer {canary_token}"
    assert route.calls[1].request.headers["authorization"] == f"Bearer {canary_token}"
    assert "display name: Dana Engineer" in who.stdout
    assert "credential source: stored PAT dcp_0123456789abcdef" in who.stdout
    assert f"server: {BASE}" in who.stdout
    assert "global permissions: contract:read" in who.stdout
    path = _data_file()
    assert json.loads(path.read_text())["default"]["access_token"] == canary_token
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_whoami_json_has_the_documented_fields_and_no_secret(
    canary_token: str, assert_no_secret: Callable[..., None], run: Callable[..., Outcome]
) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me())
        run("login", "--token", "-", input_text=canary_token)
        who = run("whoami", "--json")
    assert who.code == 0, who.output
    assert_no_secret(who.output, canary_token)
    data = json.loads(who.stdout)
    assert data["principal_type"] == "user"
    assert data["via"] == "pat"
    assert data["read_only"] is True
    assert data["profile"] == "default"
    assert data["credential_source"].startswith("stored PAT")
    assert data["global_permissions"] == ["contract:read"]


def test_login_stores_in_the_os_keyring_when_one_is_available(
    canary_token: str, monkeypatch: pytest.MonkeyPatch, run: Callable[..., Outcome]
) -> None:
    memory = MemoryKeyring()
    monkeypatch.setattr(keyring.core, "_keyring_backend", memory)
    monkeypatch.delenv("CONTRACTUM_KEYRING")
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me())
        login = run("login", "--token", "-", input_text=canary_token)
        who = run("whoami")
    assert login.code == 0, login.output
    assert who.code == 0, who.output
    assert ("contractum", "default") in memory.entries
    assert not _data_file().exists()


def test_a_token_on_the_command_line_is_refused_with_exit_2(
    canary_token: str, run: Callable[..., Outcome]
) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        route = router.get(ME).respond(200, json=_me())
        result = run("login", "--token", canary_token)
    assert result.code == 2
    assert "pass the token on stdin (--token -)" in result.output
    assert canary_token not in result.output
    assert route.call_count == 0
    assert not _data_file().exists()


def test_login_without_a_token_and_without_a_terminal_is_a_usage_error(
    run: Callable[..., Outcome],
) -> None:
    result = run("login")
    assert result.code == 2
    assert "no terminal to ask for the token" in result.output
    assert "--token -" in result.output
    assert "CONTRACTUM_TOKEN" in result.output
    assert not _data_file().exists()


def test_login_on_a_terminal_asks_for_the_token_without_echo(
    canary_token: str,
    assert_no_secret: Callable[..., None],
    run: Callable[..., Outcome],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contractum.cli import auth as auth_cli

    monkeypatch.setattr(auth_cli, "_stdin_is_tty", lambda: True)
    prompts: list[tuple[str, bool]] = []

    def fake_prompt(text: str, hide_input: bool = False, **_: Any) -> str:
        prompts.append((text, hide_input))
        return canary_token

    monkeypatch.setattr(auth_cli.click, "prompt", fake_prompt)
    with respx.mock(base_url=BASE) as router:
        route = router.get(ME).respond(200, json=_me())
        result = run("login")
    assert result.code == 0, result.output
    assert prompts == [("Token", True)]
    assert route.calls.last.request.headers["authorization"] == f"Bearer {canary_token}"
    assert json.loads(_data_file().read_text())["default"]["kind"] == "pat"
    assert_no_secret(result.output, canary_token)


@pytest.mark.parametrize(
    "argv",
    [
        ["--username", "alice"],
        ["--password-stdin"],
        ["--entra"],
        ["--device-code"],
        ["--client-credentials"],
    ],
)
def test_the_removed_sign_in_options_are_unknown(
    argv: list[str], run: Callable[..., Outcome]
) -> None:
    result = run("login", *argv)
    assert result.code == 2
    assert "No such option" in result.output
    assert not _data_file().exists()


@pytest.mark.parametrize("text", ["", "\n", "two words\n", "tab\there"])
def test_an_unusable_pasted_token_is_refused(text: str, run: Callable[..., Outcome]) -> None:
    result = run("login", "--token", "-", input_text=text)
    assert result.code == 2
    assert "error:" not in result.stdout


def test_a_rejected_token_is_not_stored(canary_token: str, run: Callable[..., Outcome]) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(
            401,
            json={"type": "urn:problem:unauthorized", "title": "Unauthorized", "status": 401},
            headers={"content-type": "application/problem+json"},
        )
        result = run("login", "--token", "-", input_text=canary_token)
    assert result.code == 2
    assert canary_token not in result.output
    assert not _data_file().exists()


def test_whoami_with_the_environment_token_names_the_source(
    canary_token: str, monkeypatch: pytest.MonkeyPatch, run: Callable[..., Outcome]
) -> None:
    monkeypatch.setenv("CONTRACTUM_TOKEN", canary_token)
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me(via="jwt"))
        who = run("whoami")
    assert who.code == 0, who.output
    assert "credential source: CONTRACTUM_TOKEN (environment)" in who.stdout
    assert canary_token not in who.output
    assert not _data_file().exists()


def test_whoami_without_credentials_says_how_to_sign_in(run: Callable[..., Outcome]) -> None:
    result = run("whoami")
    assert result.code == 2
    assert "no token is configured" in result.output
    assert "contractum login --token -" in result.output


def test_a_stored_credential_is_never_sent_to_another_server(
    canary_token: str, run: Callable[..., Outcome]
) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me())
        run("login", "--token", "-", input_text=canary_token)
    with respx.mock(base_url="https://other.example", assert_all_called=False) as other:
        route = other.get(ME).respond(200, json=_me())
        result = run("whoami", url="https://other.example")
    assert result.code == 2
    assert f"belongs to {BASE}" in result.output
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("via", "idp", "label"),
    [
        ("key", "local", "stored service-account key dcp_0123456789abcdef"),
        ("jwt", "entra", "stored Entra token"),
        ("jwt", "local", "stored token"),
    ],
)
def test_the_credential_kind_follows_how_the_server_sees_the_token(
    canary_token: str, via: str, idp: str, label: str, run: Callable[..., Outcome]
) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me(via=via, idp=idp))
        run("login", "--token", "-", input_text=canary_token)
        who = run("whoami")
    assert f"credential source: {label}" in who.stdout


def test_a_pat_that_can_write_triggers_a_warning(
    canary_token: str, run: Callable[..., Outcome]
) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me(read_only=False))
        login = run("login", "--token", "-", input_text=canary_token)
    assert login.code == 0, login.output
    assert "warning: this token can write" in login.stderr


def test_a_token_without_a_label_falls_back_to_the_display_name(
    run: Callable[..., Outcome],
) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me())
        run("login", "--token", "-", input_text="dcp_notashape")
        who = run("whoami")
    assert "stored PAT Dana Engineer" in who.stdout


def test_the_store_default_location_is_the_isolated_data_dir(run: Callable[..., Outcome]) -> None:
    assert SecretStore().data_dir == Path(os.environ["CONTRACTUM_DATA_DIR"])


def test_logout_removes_a_stored_pat_and_says_how_to_revoke_it(
    canary_token: str, run: Callable[..., Outcome]
) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me())
        run("login", "--token", "-", input_text=canary_token)
    result = run("logout")
    assert result.code == 0, result.output
    assert "removed the stored credential" in result.stderr
    assert "revoke it in the web interface" in result.stderr
    assert canary_token not in result.output
    assert run("whoami").code == 2


def test_logout_with_nothing_stored_exits_0(run: Callable[..., Outcome]) -> None:
    result = run("logout")
    assert result.code == 0
    assert "not signed in" in result.stderr


def test_logout_with_the_environment_token_says_to_unset_it(
    canary_token: str, monkeypatch: pytest.MonkeyPatch, run: Callable[..., Outcome]
) -> None:
    monkeypatch.setenv("CONTRACTUM_TOKEN", canary_token)
    result = run("logout")
    assert result.code == 0
    assert "unset it to sign out" in result.stderr
    assert canary_token not in result.output


def test_logout_of_an_entra_token_says_it_stays_valid_until_it_expires(
    canary_token: str, run: Callable[..., Outcome]
) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me(via="jwt", idp="entra"))
        run("login", "--token", "-", input_text=canary_token)
    result = run("logout")
    assert result.code == 0, result.output
    assert "the token itself stays valid until it expires" in result.stderr
    assert not json.loads(_data_file().read_text())


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "local_session", "server": BASE, "access_token": "x", "refresh_token": "y"},
        {"kind": "entra_user", "server": BASE, "access_token": "x"},
        {"server": BASE},
        "not an object",
    ],
)
def test_an_unusable_stored_entry_is_refused_and_logout_removes_it(
    entry: object, run: Callable[..., Outcome]
) -> None:
    path = _data_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"default": entry}))
    path.chmod(0o600)
    who = run("whoami")
    assert who.code == 2
    assert "cannot be used" in who.output
    assert "contractum login --token -" in who.output
    result = run("logout")
    assert result.code == 0, result.output
    assert "removed the stored credential" in result.stderr
    assert "stays valid until it expires or is revoked" in result.stderr
    assert not json.loads(path.read_text())


def test_logout_needs_no_server(canary_token: str, run: Callable[..., Outcome]) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me())
        run("login", "--token", "-", input_text=canary_token)
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        result = run("logout")
        assert router.calls.call_count == 0
    assert result.code == 0, result.output


@pytest.mark.parametrize("ending", ["\r\n", "\n", ""])
def test_one_trailing_line_break_is_dropped_from_a_piped_token(
    canary_token: str, ending: str, run: Callable[..., Outcome]
) -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.get(ME).respond(200, json=_me())
        result = run("login", "--token", "-", input_text=canary_token + ending)
    assert result.code == 0, result.output
    assert route.calls.last.request.headers["authorization"] == f"Bearer {canary_token}"
