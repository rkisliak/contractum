"""Connection settings: the profiles file and the flag > environment > profile > default order.

The profiles file is TOML (``CONTRACTUM_CONFIG`` or ``<user config dir>/contractum/config.toml``)::

    default_profile = "work"

    [profiles.work]
    url = "https://contractum.example.com"
    ca_bundle = "/etc/ssl/corp-ca.pem"

It never holds credentials: a key that looks like a token, secret or password is refused.
``CONTRACTUM_TOKEN`` is read from the environment only and is never written anywhere.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import filelock
import platformdirs
from pydantic import SecretStr

from contractum._atomic import write_atomic
from contractum._transport import ConnectionSettings
from contractum.errors import ConfigError

DEFAULT_PROFILE = "default"
MAX_CONFIG_BYTES = 64 * 1024
LOCK_TIMEOUT_SECONDS = 10.0
_PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SECRET_WORDS = ("token", "secret", "password")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

ENV_URL = "CONTRACTUM_URL"
ENV_TOKEN = "CONTRACTUM_TOKEN"  # noqa: S105 - the variable name, not a secret
ENV_PROFILE = "CONTRACTUM_PROFILE"
ENV_CA_BUNDLE = "CONTRACTUM_CA_BUNDLE"
ENV_ALLOW_INSECURE_HTTP = "CONTRACTUM_ALLOW_INSECURE_HTTP"
ENV_CONFIG = "CONTRACTUM_CONFIG"


@dataclass(frozen=True)
class Profile:
    """The values one named profile may hold; None means not set in the profile."""

    url: str | None = None
    ca_bundle: str | None = None
    allow_insecure_http: bool | None = None


PROFILE_KEYS: tuple[str, ...] = tuple(item.name for item in fields(Profile))
_BOOL_KEYS = frozenset({"allow_insecure_http"})
_RETIRED_KEY_PREFIX = "entra_"
"""Keys an earlier pre-release wrote (Entra sign-in settings); read and ignored, never written."""


@dataclass(frozen=True)
class ConfigFile:
    """The parsed profiles file."""

    default_profile: str | None = None
    profiles: Mapping[str, Profile] = field(default_factory=dict)


@dataclass(frozen=True)
class Overrides:
    """Explicit values (command-line flags or function arguments); None means not given."""

    url: str | None = None
    ca_bundle: str | os.PathLike[str] | None = None
    allow_insecure_http: bool | None = None


@dataclass(frozen=True)
class Settings:
    """The resolved connection settings of one run."""

    profile: str
    url: str
    ca_bundle: Path | None = None
    allow_insecure_http: bool = False
    timeout: float = 30.0
    env_token: SecretStr | None = None

    def to_connection(self) -> ConnectionSettings:
        """Return the transport view of these settings."""
        return ConnectionSettings(
            url=self.url,
            ca_bundle=self.ca_bundle,
            allow_insecure_http=self.allow_insecure_http,
            timeout=self.timeout,
        )


# --- the profiles file -----------------------------------------------------------------------


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the profiles file path: ``CONTRACTUM_CONFIG`` or the user config directory."""
    environ = os.environ if env is None else env
    override = environ.get(ENV_CONFIG, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_config_dir("contractum")) / "config.toml"


def _check_profile_name(name: str) -> str:
    if not _PROFILE_NAME.fullmatch(name):
        raise ConfigError(
            "a profile name uses letters, digits, '-' and '_' (at most 64 characters)"
        )
    return name


def _check_key(key: str) -> str:
    if any(word in key.lower() for word in _SECRET_WORDS):
        raise ConfigError(
            f"the profile key {key!r} looks like a credential",
            hint="profile files never hold credentials; use CONTRACTUM_TOKEN or `contractum login`",
        )
    if key not in PROFILE_KEYS:
        raise ConfigError(
            f"unknown profile key {key!r}", hint=f"valid keys: {', '.join(PROFILE_KEYS)}"
        )
    return key


def _check_value(key: str, value: object) -> str | bool:
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in _TRUE | _FALSE:
            return value.lower() in _TRUE
        raise ConfigError(f"the profile key {key!r} must be true or false")
    if not isinstance(value, str) or not value:
        raise ConfigError(f"the profile key {key!r} must be a non-empty string")
    return value


def _profile_from_table(name: str, table: object) -> Profile:
    if not isinstance(table, dict):
        raise ConfigError(f"profile {name!r} must be a table")
    values = {
        _check_key(str(key)): _check_value(str(key), value)
        for key, value in table.items()
        if not str(key).startswith(_RETIRED_KEY_PREFIX)
    }
    return Profile(**values)  # type: ignore[arg-type]


def _parse_config(data: Mapping[str, Any]) -> ConfigFile:
    unknown = sorted(set(data) - {"default_profile", "profiles"})
    if unknown:
        raise ConfigError(f"unknown key {unknown[0]!r} in the profiles file")
    default = data.get("default_profile")
    if default is not None:
        if not isinstance(default, str):
            raise ConfigError("default_profile must be a string")
        _check_profile_name(default)
    tables = data.get("profiles", {})
    if not isinstance(tables, dict):
        raise ConfigError("profiles must be a table of profile tables")
    profiles = {
        _check_profile_name(str(name)): _profile_from_table(str(name), table)
        for name, table in tables.items()
    }
    return ConfigFile(default_profile=default, profiles=profiles)


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> ConfigFile:
    """Read the profiles file; a missing file is an empty configuration."""
    target = path if path is not None else config_path(env)
    try:
        if not target.exists():
            return ConfigFile()
        if target.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigError(f"the profiles file {target} is larger than 64 KiB")
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"the profiles file {target} could not be read") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the profiles file {target} is not valid TOML: {exc}") from None
    return _parse_config(data)


def _quote(text: str) -> str:
    """Return ``text`` as a TOML basic string."""
    out = ['"']
    for char in text:
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _literal(key: str, value: str | bool) -> str:
    if key in _BOOL_KEYS:
        return "true" if value else "false"
    return _quote(str(value))


def _dump(config: ConfigFile) -> str:
    """Serialise a configuration as TOML (string and boolean values only)."""
    lines: list[str] = []
    if config.default_profile is not None:
        lines.append(f"default_profile = {_quote(config.default_profile)}")
    for name in sorted(config.profiles):
        lines += ["", f"[profiles.{name}]"]
        profile = config.profiles[name]
        for key in PROFILE_KEYS:
            value = getattr(profile, key)
            if value is not None:
                lines.append(f"{key} = {_literal(key, value)}")
    return "\n".join(lines).lstrip("\n") + "\n"


def _update(path: Path | None, change: Callable[[ConfigFile], ConfigFile]) -> None:
    """Apply ``change(config) -> config`` to the file under a lock, writing atomically."""
    target = path if path is not None else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with filelock.FileLock(f"{target}.lock", timeout=LOCK_TIMEOUT_SECONDS):
            updated = change(load_config(target))
            write_atomic(target, _dump(updated).encode("utf-8"), mode=0o600)
    except filelock.Timeout:
        raise ConfigError(f"the profiles file {target} is locked by another process") from None


def set_profile_value(profile: str, key: str, value: object, *, path: Path | None = None) -> None:
    """Set one key of a profile, creating the profile when needed."""
    name = _check_profile_name(profile)
    clean = _check_value(_check_key(key), value)

    def change(config: ConfigFile) -> ConfigFile:
        current = config.profiles.get(name, Profile())
        changed = replace(current, **{key: clean})  # type: ignore[arg-type]
        profiles = {**config.profiles, name: changed}
        return replace(config, profiles=profiles)

    _update(path, change)


def unset_profile_value(profile: str, key: str, *, path: Path | None = None) -> None:
    """Remove one key of a profile; an unknown profile or an unset key is not an error."""
    name = _check_profile_name(profile)
    _check_key(key)

    def change(config: ConfigFile) -> ConfigFile:
        current = config.profiles.get(name)
        if current is None:
            return config
        return replace(config, profiles={**config.profiles, name: replace(current, **{key: None})})

    _update(path, change)


def set_default_profile(name: str, *, path: Path | None = None) -> None:
    """Make ``name`` the profile used when no flag or environment variable names one."""
    clean = _check_profile_name(name)
    _update(path, lambda config: replace(config, default_profile=clean))


# --- resolution ------------------------------------------------------------------------------


def _env(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "").strip()
    return value or None


def _env_bool(environ: Mapping[str, str], name: str) -> bool | None:
    value = _env(environ, name)
    if value is None:
        return None
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


def _first(*candidates: Any) -> Any:
    """Return the first candidate that is not None."""
    return next((item for item in candidates if item is not None), None)


def _select_profile(
    flag: str | None, environ: Mapping[str, str], config: ConfigFile
) -> tuple[str, bool]:
    """Return the profile name and whether it must exist in the file."""
    chosen = _first(flag, _env(environ, ENV_PROFILE), config.default_profile)
    if chosen is None:
        return DEFAULT_PROFILE, False
    name = _check_profile_name(chosen)
    return name, name != DEFAULT_PROFILE


def _missing_url_error(path: Path) -> ConfigError:
    return ConfigError(
        "no Contractum URL is configured",
        hint=(
            f"pass --url, set {ENV_URL}, or add `url` to a profile in {path} "
            "(profiles are optional)"
        ),
    )


def resolve_settings(
    *,
    profile: str | None = None,
    overrides: Overrides | None = None,
    env: Mapping[str, str] | None = None,
    path: Path | None = None,
) -> Settings:
    """Resolve the settings: explicit values > environment > profile file > defaults."""
    explicit = overrides if overrides is not None else Overrides()
    environ = os.environ if env is None else env
    file_path = path if path is not None else config_path(environ)
    config = load_config(file_path)
    name, must_exist = _select_profile(profile, environ, config)
    stored = config.profiles.get(name)
    if stored is None:
        if must_exist:
            known = ", ".join(sorted(config.profiles)) or "none"
            raise ConfigError(
                f"profile {name!r} is not defined in {file_path}", hint=f"known: {known}"
            )
        stored = Profile()
    url = _first(explicit.url, _env(environ, ENV_URL), stored.url)
    if url is None:
        raise _missing_url_error(file_path)
    bundle = _first(explicit.ca_bundle, _env(environ, ENV_CA_BUNDLE), stored.ca_bundle)
    token = _env(environ, ENV_TOKEN)
    return Settings(
        profile=name,
        url=url,
        ca_bundle=Path(bundle).expanduser() if bundle is not None else None,
        allow_insecure_http=bool(
            _first(
                explicit.allow_insecure_http,
                _env_bool(environ, ENV_ALLOW_INSECURE_HTTP),
                stored.allow_insecure_http,
                False,
            )
        ),
        env_token=SecretStr(token) if token is not None else None,
    )
