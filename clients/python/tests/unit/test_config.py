"""Settings precedence, the profiles file, write_atomic and the CLI client factory."""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any

import click
import filelock
import pytest
from hypothesis import given
from hypothesis import strategies as st

from contractum import _atomic, config
from contractum._atomic import write_atomic
from contractum.cli.context import GlobalOptions, client_from_ctx, settings_from_ctx
from contractum.config import (
    ConfigFile,
    Overrides,
    Profile,
    Settings,
    _dump,
    _parse_config,
    config_path,
    load_config,
    resolve_settings,
    set_default_profile,
    set_profile_value,
    unset_profile_value,
)
from contractum.errors import ConfigError

ON_WINDOWS = sys.platform == "win32"
URL_FLAG = "https://flag.example"
URL_ENV = "https://env.example"
URL_PROFILE = "https://profile.example"


@pytest.fixture
def file(tmp_path: Path) -> Path:
    return tmp_path / "profiles.toml"


def _profile_file(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# --- precedence -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "env_url", "profile_url", "expected"),
    [
        (URL_FLAG, URL_ENV, URL_PROFILE, URL_FLAG),
        (None, URL_ENV, URL_PROFILE, URL_ENV),
        (None, None, URL_PROFILE, URL_PROFILE),
        (URL_FLAG, None, URL_PROFILE, URL_FLAG),
        (URL_FLAG, URL_ENV, None, URL_FLAG),
        (None, URL_ENV, None, URL_ENV),
        (URL_FLAG, None, None, URL_FLAG),
        (None, None, None, None),
    ],
)
def test_url_precedence_flag_env_profile_default(
    file: Path, flag: str | None, env_url: str | None, profile_url: str | None, expected: str | None
) -> None:
    if profile_url is not None:
        _profile_file(file, f'[profiles.default]\nurl = "{profile_url}"\n')
    environ = {"CONTRACTUM_URL": env_url} if env_url is not None else {}
    if expected is None:
        with pytest.raises(ConfigError):
            resolve_settings(overrides=Overrides(url=flag), env=environ, path=file)
        return
    settings = resolve_settings(overrides=Overrides(url=flag), env=environ, path=file)
    assert settings.url == expected


@pytest.mark.parametrize(
    ("flag", "env_value", "profile_value", "expected"),
    [
        ("/flag.pem", "/env.pem", "/profile.pem", Path("/flag.pem")),
        (None, "/env.pem", "/profile.pem", Path("/env.pem")),
        (None, None, "/profile.pem", Path("/profile.pem")),
        (None, None, None, None),
    ],
)
def test_ca_bundle_precedence(
    file: Path,
    flag: str | None,
    env_value: str | None,
    profile_value: str | None,
    expected: Path | None,
) -> None:
    body = f'[profiles.default]\nurl = "{URL_PROFILE}"\n'
    if profile_value is not None:
        body += f'ca_bundle = "{profile_value}"\n'
    _profile_file(file, body)
    environ = {"CONTRACTUM_CA_BUNDLE": env_value} if env_value is not None else {}
    settings = resolve_settings(overrides=Overrides(ca_bundle=flag), env=environ, path=file)
    assert settings.ca_bundle == expected


@pytest.mark.parametrize(
    ("flag", "env_value", "profile_value", "expected"),
    [
        (False, "true", "true", False),
        (None, "false", "true", False),
        (None, None, "true", True),
        (None, None, None, False),
        (True, "0", "false", True),
        (None, "YES", None, True),
    ],
)
def test_allow_insecure_http_precedence(
    file: Path,
    flag: bool | None,
    env_value: str | None,
    profile_value: str | None,
    expected: bool,
) -> None:
    body = f'[profiles.default]\nurl = "{URL_PROFILE}"\n'
    if profile_value is not None:
        body += f"allow_insecure_http = {profile_value}\n"
    _profile_file(file, body)
    environ = {"CONTRACTUM_ALLOW_INSECURE_HTTP": env_value} if env_value is not None else {}
    settings = resolve_settings(
        overrides=Overrides(allow_insecure_http=flag), env=environ, path=file
    )
    assert settings.allow_insecure_http is expected


def test_an_empty_environment_variable_counts_as_unset(file: Path) -> None:
    _profile_file(file, f'[profiles.default]\nurl = "{URL_PROFILE}"\n')
    settings = resolve_settings(env={"CONTRACTUM_URL": "  ", "CONTRACTUM_TOKEN": ""}, path=file)
    assert settings.url == URL_PROFILE
    assert settings.env_token is None


def test_an_invalid_boolean_variable_is_a_config_error(file: Path) -> None:
    with pytest.raises(ConfigError, match="CONTRACTUM_ALLOW_INSECURE_HTTP"):
        resolve_settings(
            env={"CONTRACTUM_URL": URL_ENV, "CONTRACTUM_ALLOW_INSECURE_HTTP": "maybe"}, path=file
        )


def test_a_missing_url_names_the_three_ways_to_set_it(file: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        resolve_settings(env={}, path=file)
    hint = raised.value.hint or ""
    assert "--url" in hint
    assert "CONTRACTUM_URL" in hint
    assert "profile" in hint


def test_entra_keys_an_earlier_pre_release_wrote_are_ignored_and_dropped_on_write(
    file: Path,
) -> None:
    _profile_file(
        file,
        f'[profiles.default]\nurl = "{URL_PROFILE}"\nentra_tenant_id = "tenant-p"\n'
        'entra_client_id = "client-p"\nentra_scope = "scope-p"\nentra_authority = "auth-p"\n',
    )
    settings = resolve_settings(env={}, path=file)
    assert settings.url == URL_PROFILE
    assert load_config(file).profiles["default"] == Profile(url=URL_PROFILE)
    set_profile_value("default", "ca_bundle", "/etc/ca.pem", path=file)
    assert "entra_" not in file.read_text(encoding="utf-8")


@pytest.mark.parametrize("key", ["entra_tenant_id", "entra_client_id", "entra_scope"])
def test_entra_keys_can_no_longer_be_set(file: Path, key: str) -> None:
    with pytest.raises(ConfigError, match=f"unknown profile key '{key}'"):
        set_profile_value("default", key, "x", path=file)


def test_any_other_unknown_profile_key_is_still_refused(file: Path) -> None:
    _profile_file(file, f'[profiles.default]\nurl = "{URL_PROFILE}"\ncolour = "blue"\n')
    with pytest.raises(ConfigError, match="unknown profile key 'colour'"):
        resolve_settings(env={}, path=file)


def test_the_env_token_is_a_secret_and_stripped(canary_token: str) -> None:
    settings = resolve_settings(
        env={"CONTRACTUM_URL": URL_ENV, "CONTRACTUM_TOKEN": f"{canary_token}\n"},
        path=Path("/nonexistent/config.toml"),
    )
    assert settings.env_token is not None
    assert settings.env_token.get_secret_value() == canary_token
    assert canary_token not in repr(settings)


def test_to_connection_carries_the_transport_fields() -> None:
    settings = Settings(
        profile="p", url=URL_ENV, ca_bundle=Path("/x.pem"), allow_insecure_http=True, timeout=5.0
    )
    connection = settings.to_connection()
    assert (connection.url, connection.ca_bundle, connection.allow_insecure_http) == (
        URL_ENV,
        Path("/x.pem"),
        True,
    )
    assert connection.timeout == 5.0


# --- profile selection ----------------------------------------------------------------------


_TWO_PROFILES = (
    'default_profile = "work"\n'
    '[profiles.work]\nurl = "https://work.example"\n'
    '[profiles.home]\nurl = "https://home.example"\n'
    '[profiles.default]\nurl = "https://default.example"\n'
)


@pytest.mark.parametrize(
    ("flag", "env_profile", "expected_profile", "expected_url"),
    [
        ("home", "work", "home", "https://home.example"),
        (None, "home", "home", "https://home.example"),
        (None, None, "work", "https://work.example"),
    ],
)
def test_profile_choice_flag_then_env_then_default_profile(
    file: Path, flag: str | None, env_profile: str | None, expected_profile: str, expected_url: str
) -> None:
    _profile_file(file, _TWO_PROFILES)
    environ = {"CONTRACTUM_PROFILE": env_profile} if env_profile is not None else {}
    settings = resolve_settings(profile=flag, env=environ, path=file)
    assert (settings.profile, settings.url) == (expected_profile, expected_url)


def test_without_any_choice_the_default_profile_is_used(file: Path) -> None:
    _profile_file(file, '[profiles.default]\nurl = "https://default.example"\n')
    settings = resolve_settings(env={}, path=file)
    assert (settings.profile, settings.url) == ("default", "https://default.example")


def test_an_unknown_named_profile_is_an_error_listing_the_known_ones(file: Path) -> None:
    _profile_file(file, _TWO_PROFILES)
    with pytest.raises(ConfigError) as raised:
        resolve_settings(profile="nope", env={}, path=file)
    assert "home" in (raised.value.hint or "")
    with pytest.raises(ConfigError):
        resolve_settings(profile="nope", env={}, path=file.with_name("missing.toml"))


def test_a_profile_name_must_be_simple(file: Path) -> None:
    for bad in ("a b", "a.b", "", "x" * 65, "../x"):
        with pytest.raises(ConfigError):
            resolve_settings(profile=bad, env={"CONTRACTUM_URL": URL_ENV}, path=file)


def test_the_default_profile_name_needs_no_file(file: Path) -> None:
    settings = resolve_settings(profile="default", env={"CONTRACTUM_URL": URL_ENV}, path=file)
    assert settings.profile == "default"


def test_config_path_prefers_the_environment_then_the_user_directory(tmp_path: Path) -> None:
    assert config_path({"CONTRACTUM_CONFIG": str(tmp_path / "c.toml")}) == tmp_path / "c.toml"
    fallback = config_path({})
    assert fallback.name == "config.toml"
    assert "contractum" in fallback.parts[-2]
    assert config_path() == tmp_path / "config"


def test_resolve_uses_the_config_environment_variable(tmp_path: Path) -> None:
    target = _profile_file(
        tmp_path / "elsewhere.toml", f'[profiles.default]\nurl = "{URL_PROFILE}"\n'
    )
    settings = resolve_settings(env={"CONTRACTUM_CONFIG": str(target)})
    assert settings.url == URL_PROFILE


# --- the profiles file ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '[profiles.x]\nurl = "https://a.example"\ntoken = "abc"\n',
        '[profiles.x]\npassword = "abc"\n',
        '[profiles.x]\nclient_secret = "abc"\n',
        '[profiles.x]\ncolour = "red"\n',
        "[profiles.x]\nurl = 5\n",
        '[profiles.x]\nurl = ""\n',
        '[profiles.x]\nallow_insecure_http = "maybe"\n',
        "[profiles.x]\nallow_insecure_http = 3\n",
        "[profiles]\nx = 1\n",
        'profiles = "no"\n',
        'extra = "no"\n',
        "default_profile = 5\n",
        'default_profile = "not valid"\n',
        '[profiles."bad name"]\nurl = "https://a.example"\n',
        "this is not toml",
    ],
)
def test_invalid_profile_files_are_rejected(file: Path, body: str) -> None:
    _profile_file(file, body)
    with pytest.raises(ConfigError):
        load_config(file)


def test_a_credential_key_is_refused_with_a_pointer_to_the_right_place(file: Path) -> None:
    _profile_file(file, '[profiles.x]\ntoken = "abc"\n')
    with pytest.raises(ConfigError) as raised:
        load_config(file)
    assert "abc" not in str(raised.value)
    assert "CONTRACTUM_TOKEN" in (raised.value.hint or "")


def test_a_missing_file_is_an_empty_configuration(file: Path) -> None:
    assert load_config(file) == ConfigFile()


def test_an_oversized_or_unreadable_file_is_a_config_error(tmp_path: Path, file: Path) -> None:
    file.write_text("#" * (config.MAX_CONFIG_BYTES + 1), encoding="utf-8")
    with pytest.raises(ConfigError, match="64 KiB"):
        load_config(file)
    with pytest.raises(ConfigError, match="could not be read"):
        load_config(tmp_path)
    file.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigError, match="could not be read"):
        load_config(file)


def test_set_unset_and_default_profile_round_trip(file: Path) -> None:
    set_profile_value("work", "url", URL_PROFILE, path=file)
    set_profile_value("work", "allow_insecure_http", "true", path=file)
    set_profile_value("work", "ca_bundle", "/etc/ca.pem", path=file)
    set_profile_value("home", "url", URL_ENV, path=file)
    set_default_profile("work", path=file)
    loaded = load_config(file)
    assert loaded.default_profile == "work"
    assert loaded.profiles["work"] == Profile(
        url=URL_PROFILE, ca_bundle="/etc/ca.pem", allow_insecure_http=True
    )
    unset_profile_value("work", "ca_bundle", path=file)
    unset_profile_value("ghost", "url", path=file)
    reloaded = load_config(file)
    assert reloaded.profiles["work"].ca_bundle is None
    assert reloaded.profiles["home"].url == URL_ENV
    assert "ghost" not in reloaded.profiles


def test_setters_reject_unknown_keys_secrets_and_bad_values(file: Path) -> None:
    for key, value in [
        ("colour", "red"),
        ("token", "x"),
        ("url", ""),
        ("allow_insecure_http", "x"),
    ]:
        with pytest.raises(ConfigError):
            set_profile_value("work", key, value, path=file)
    with pytest.raises(ConfigError):
        set_profile_value("bad name", "url", URL_ENV, path=file)
    with pytest.raises(ConfigError):
        unset_profile_value("work", "token", path=file)
    with pytest.raises(ConfigError):
        set_default_profile("bad name", path=file)
    assert not file.exists()


@pytest.mark.skipif(ON_WINDOWS, reason="Windows does not honour POSIX modes")
def test_the_profiles_file_is_written_privately(file: Path) -> None:
    set_profile_value("work", "url", URL_ENV, path=file)
    assert stat.S_IMODE(file.stat().st_mode) == 0o600


def test_writes_go_through_write_atomic(file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[Path] = []
    real = config.write_atomic

    def spy(path: Any, data: bytes, **kwargs: Any) -> None:
        written.append(Path(path))
        real(path, data, **kwargs)

    monkeypatch.setattr(config, "write_atomic", spy)
    set_profile_value("work", "url", URL_ENV, path=file)
    assert written == [file]


def test_a_locked_file_is_reported_not_overwritten(
    file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_profile_value("work", "url", URL_ENV, path=file)
    before = file.read_bytes()
    monkeypatch.setattr(config, "LOCK_TIMEOUT_SECONDS", 0.05)
    with filelock.FileLock(f"{file}.lock"), pytest.raises(ConfigError, match="locked"):
        set_profile_value("work", "url", URL_PROFILE, path=file)
    assert file.read_bytes() == before


def test_the_default_path_is_used_when_none_is_given(tmp_path: Path) -> None:
    set_profile_value("work", "url", URL_ENV)
    assert load_config().profiles["work"].url == URL_ENV
    assert (tmp_path / "config").exists()


def test_an_empty_configuration_dumps_to_a_valid_document() -> None:
    assert tomllib.loads(_dump(ConfigFile())) == {}


_TEXT = st.text(min_size=1)


@given(
    url=_TEXT,
    bundle=st.none() | _TEXT,
    insecure=st.none() | st.booleans(),
    default=st.none() | st.sampled_from(["a", "b-1", "Z_9"]),
)
def test_the_toml_writer_round_trips_strings_and_booleans_losslessly(
    url: str, bundle: str | None, insecure: bool | None, default: str | None
) -> None:
    original = ConfigFile(
        default_profile=default,
        profiles={
            "a": Profile(url=url, ca_bundle=bundle, allow_insecure_http=insecure),
            "b-1": Profile(),
        },
    )
    parsed = _parse_config(tomllib.loads(_dump(original)))
    assert parsed.default_profile == original.default_profile
    assert dict(parsed.profiles) == dict(original.profiles)


def test_special_characters_survive_the_writer() -> None:
    nasty = 'quote " backslash \\ tab \t newline \n nul \x00 del \x7f unicode é中'
    original = ConfigFile(profiles={"a": Profile(url=nasty)})
    assert _parse_config(tomllib.loads(_dump(original))).profiles["a"].url == nasty


# --- write_atomic ---------------------------------------------------------------------------


def test_write_atomic_creates_parents_and_writes_the_bytes(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "file.bin"
    write_atomic(target, b"hello")
    assert target.read_bytes() == b"hello"
    assert [p.name for p in target.parent.iterdir()] == ["file.bin"]


@pytest.mark.skipif(ON_WINDOWS, reason="Windows does not honour POSIX modes")
@pytest.mark.parametrize("mode", [0o600, 0o644, 0o640])
def test_write_atomic_applies_the_requested_mode(tmp_path: Path, mode: int) -> None:
    target = tmp_path / "file.bin"
    write_atomic(target, b"x", mode=mode)
    assert stat.S_IMODE(target.stat().st_mode) == mode


def test_write_atomic_defaults_to_world_readable_mode(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    write_atomic(target, b"x")
    if not ON_WINDOWS:
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_write_atomic_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    write_atomic(target, b"old")
    write_atomic(str(target), b"new")
    assert target.read_bytes() == b"new"


def test_a_failure_before_the_replace_keeps_the_old_file_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.bin"
    write_atomic(target, b"previous")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_atomic.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(target, b"replacement")
    assert target.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["file.bin"]


def test_a_failure_while_writing_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(fd: int) -> None:
        raise OSError("io error")

    monkeypatch.setattr(_atomic.os, "fsync", boom)
    with pytest.raises(OSError, match="io error"):
        write_atomic(tmp_path / "file.bin", b"data")
    assert list(tmp_path.iterdir()) == []


def test_write_atomic_falls_back_to_chmod_without_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(_atomic.os, "fchmod", raising=False)
    target = tmp_path / "file.bin"
    write_atomic(target, b"x", mode=0o600)
    assert target.read_bytes() == b"x"
    if not ON_WINDOWS:
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


# --- the CLI client factory -----------------------------------------------------------------


def _context(**options: Any) -> click.Context:
    ctx = click.Context(click.Command("probe"))
    ctx.obj = GlobalOptions(**options)
    return ctx


def test_settings_from_ctx_combines_flags_environment_and_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_file = Path(os.environ["CONTRACTUM_CONFIG"])
    profile_file.write_text(
        f'[profiles.work]\nurl = "{URL_PROFILE}"\nca_bundle = "/profile.pem"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CONTRACTUM_CA_BUNDLE", "/env.pem")
    settings = settings_from_ctx(_context(profile="work"))
    assert (settings.url, settings.ca_bundle) == (URL_PROFILE, Path("/env.pem"))
    flagged = settings_from_ctx(
        _context(
            profile="work", url=URL_FLAG, ca_bundle=tmp_path / "flag.pem", allow_insecure_http=True
        )
    )
    assert flagged.url == URL_FLAG
    assert flagged.ca_bundle == tmp_path / "flag.pem"
    assert flagged.allow_insecure_http is True


def test_client_from_ctx_builds_a_client_with_the_env_token(
    monkeypatch: pytest.MonkeyPatch, canary_token: str
) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", URL_ENV)
    monkeypatch.setenv("CONTRACTUM_TOKEN", canary_token)
    with client_from_ctx(_context()) as client:
        assert repr(client) == f"Client(url={URL_ENV!r})"
        assert canary_token not in repr(client)


def test_a_context_without_global_options_uses_the_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", URL_ENV)
    assert settings_from_ctx(click.Context(click.Command("probe"))).url == URL_ENV
