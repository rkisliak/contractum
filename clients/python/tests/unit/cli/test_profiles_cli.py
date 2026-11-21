"""The config command group: named profiles, never credentials."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from support.auth import Outcome, run_main


@pytest.fixture
def run(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Outcome]:
    def invoke(*args: str) -> Outcome:
        return run_main(list(args), capsys=capsys, monkeypatch=monkeypatch)

    return invoke


def test_set_get_and_list_a_value_of_the_default_profile(run: Callable[..., Outcome]) -> None:
    assert run("config", "set", "url", "https://contractum.example").code == 0
    got = run("config", "get", "url")
    assert got.code == 0
    assert got.stdout == "https://contractum.example\n"
    listing = run("config", "list")
    assert "  default\n" in listing.stdout
    assert "    url = https://contractum.example" in listing.stdout


def test_a_named_profile_is_created_with_the_global_flag(run: Callable[..., Outcome]) -> None:
    assert run("--profile", "prod", "config", "set", "url", "https://prod.example").code == 0
    assert run("--profile", "prod", "config", "get", "url").stdout == "https://prod.example\n"
    assert run("config", "get", "url").code == 2


def test_use_marks_the_default_profile_and_get_follows_it(run: Callable[..., Outcome]) -> None:
    run("--profile", "prod", "config", "set", "url", "https://prod.example")
    run("--profile", "dev", "config", "set", "allow_insecure_http", "true")
    assert run("config", "use", "prod").code == 0
    listing = run("config", "list").stdout
    assert "* prod" in listing
    assert "  dev" in listing
    assert "allow_insecure_http = true" in listing
    assert run("config", "get", "url").stdout == "https://prod.example\n"


def test_the_environment_profile_is_used_when_no_flag_is_given(
    run: Callable[..., Outcome], monkeypatch: pytest.MonkeyPatch
) -> None:
    run("--profile", "ci", "config", "set", "url", "https://ci.example")
    monkeypatch.setenv("CONTRACTUM_PROFILE", "ci")
    assert run("config", "get", "url").stdout == "https://ci.example\n"


def test_use_of_an_unknown_profile_is_refused_with_the_known_ones(
    run: Callable[..., Outcome],
) -> None:
    run("--profile", "prod", "config", "set", "url", "https://prod.example")
    result = run("config", "use", "nope")
    assert result.code == 2
    assert "profile 'nope' is not defined" in result.stderr
    assert "known: prod" in result.stderr
    assert run("config", "use", "default").code == 0


def test_unset_removes_a_value(run: Callable[..., Outcome]) -> None:
    run("config", "set", "url", "https://contractum.example")
    run("config", "set", "ca_bundle", "/etc/ca.pem")
    assert run("config", "unset", "url").code == 0
    assert run("config", "get", "url").code == 2
    assert run("config", "get", "ca_bundle").stdout == "/etc/ca.pem\n"


@pytest.mark.parametrize("key", ["token", "client_secret", "password", "Access_Token"])
def test_secret_like_keys_are_refused(run: Callable[..., Outcome], key: str) -> None:
    result = run("config", "set", key, "x")
    assert result.code == 2
    assert "looks like a credential" in result.stderr
    assert not Path(os.environ["CONTRACTUM_CONFIG"]).exists()


def test_an_unknown_key_is_refused_by_set_get_and_unset(run: Callable[..., Outcome]) -> None:
    for verb in (("set", "colour", "red"), ("get", "colour"), ("unset", "colour")):
        result = run("config", *verb)
        assert result.code == 2
        assert "unknown profile key" in result.stderr
        assert "valid keys:" in result.stderr


def test_a_boolean_value_is_validated(run: Callable[..., Outcome]) -> None:
    assert run("config", "set", "allow_insecure_http", "maybe").code == 2
    run("config", "set", "allow_insecure_http", "yes")
    assert run("config", "get", "allow_insecure_http").stdout == "true\n"


def test_list_without_profiles_says_so(run: Callable[..., Outcome]) -> None:
    result = run("config", "list")
    assert result.code == 0
    assert "no profiles are defined" in result.stderr


def test_path_prints_the_profiles_file(run: Callable[..., Outcome]) -> None:
    assert run("config", "path").stdout == os.environ["CONTRACTUM_CONFIG"] + "\n"


def test_the_profiles_file_never_holds_a_secret(
    run: Callable[..., Outcome], canary_token: str
) -> None:
    run("config", "set", "url", "https://contractum.example")
    text = Path(os.environ["CONTRACTUM_CONFIG"]).read_text()
    assert canary_token not in text
    assert "token" not in text.lower()
