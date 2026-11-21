"""Credential storage: the OS keyring first, a ``0600`` file when no keyring can be used.

One entry per profile (service ``contractum``, username = profile name). The entry is a JSON
document produced by ``StoredCredential.to_storage_json``, the only place that reveals secret
values. Nothing here prints, logs or raises with a secret in the message.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

import filelock
import platformdirs
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from contractum._atomic import write_atomic
from contractum.cli.output import warn
from contractum.errors import AuthError, ConfigError

SERVICE = "contractum"
ENV_KEYRING = "CONTRACTUM_KEYRING"
ENV_DATA_DIR = "CONTRACTUM_DATA_DIR"
CREDENTIALS_FILE = "credentials.json"
MAX_STORE_BYTES = 8 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 10.0
_GROUP_OTHER_BITS = 0o077
RELOGIN_HINT = "run `contractum login --token -`"

CredentialKind = Literal["pat", "service_account", "token", "entra_token"]

_UNUSABLE_BACKEND_MODULES = ("keyring.backends.fail", "keyring.backends.null", "keyrings.alt")


class StoredCredential(BaseModel):
    """What ``contractum login`` keeps for one profile; secrets are ``SecretStr``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: CredentialKind
    server: str
    access_token: SecretStr
    principal_label: str | None = None

    def to_storage_json(self) -> str:
        """Return the JSON text to persist; the only call that reveals the secret values."""
        return json.dumps(self.to_storage_dict(), separators=(",", ":"))

    def to_storage_dict(self) -> dict[str, Any]:
        """Return the plain dictionary form used inside the credentials file."""
        return {
            "kind": self.kind,
            "server": self.server,
            "access_token": self.access_token.get_secret_value(),
            "principal_label": self.principal_label,
        }


class KeyringBackend(Protocol):
    """The three calls the store uses on a keyring backend."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


def _usable(backend: object) -> bool:
    module = type(backend).__module__
    return not module.startswith(_UNUSABLE_BACKEND_MODULES)


def same_server(left: str, right: str) -> bool:
    """Compare two server URLs ignoring case and a trailing slash."""
    return left.strip().rstrip("/").lower() == right.strip().rstrip("/").lower()


def default_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return ``CONTRACTUM_DATA_DIR`` or the user data directory."""
    environ = os.environ if env is None else env
    override = environ.get(ENV_DATA_DIR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_data_dir("contractum"))


class SecretStore:
    """Read and write the credential of a profile."""

    def __init__(
        self,
        service: str = SERVICE,
        *,
        backend: KeyringBackend | None = None,
        path: Path | None = None,
        env: Mapping[str, str] | None = None,
        warn_func: Callable[[str], None] = warn,
    ) -> None:
        self._service = service
        self._backend = backend
        self._path = path
        self._env = os.environ if env is None else env
        self._warn = warn_func
        self._warned = False

    # --- where --------------------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """The directory holding the credentials file."""
        return self._path.parent if self._path is not None else default_data_dir(self._env)

    @property
    def credentials_path(self) -> Path:
        """The credentials file used when no keyring is available."""
        return self._path if self._path is not None else self.data_dir / CREDENTIALS_FILE

    def _mode(self) -> str:
        value = self._env.get(ENV_KEYRING, "").strip()
        if value == "0":
            return "file"
        if value == "1":
            return "keyring"
        return "auto"

    def location(self) -> str:
        """Describe where credentials go now: the OS keyring or the credentials file path."""
        if self._mode() != "file" and self._keyring_backend(required=False) is not None:
            return "the OS keyring"
        return str(self.credentials_path)

    # --- keyring ------------------------------------------------------------------------

    def _keyring_backend(self, *, required: bool) -> KeyringBackend | None:
        if self._backend is not None:
            return self._backend
        import keyring

        backend = keyring.get_keyring()
        if _usable(backend):
            found: KeyringBackend = backend
            return found
        if required:
            raise ConfigError(
                "CONTRACTUM_KEYRING=1 but no OS keyring is available",
                hint="unset CONTRACTUM_KEYRING to fall back to the credentials file",
            )
        return None

    def _fallback_notice(self) -> None:
        if not self._warned:
            self._warned = True
            self._warn(
                f"no usable OS keyring; credentials are kept in {self.credentials_path} "
                "(readable by you only)"
            )

    def _with_keyring(self, action: Callable[[KeyringBackend], Any]) -> tuple[bool, Any]:
        """Run ``action`` on the keyring; return (used, result), (False, None) to fall back."""
        mode = self._mode()
        if mode == "file":
            return False, None
        from keyring.errors import KeyringError

        backend = self._keyring_backend(required=mode == "keyring")
        if backend is None:
            self._fallback_notice()
            return False, None
        try:
            return True, action(backend)
        except KeyringError:
            if mode == "keyring":
                raise ConfigError("the OS keyring refused the operation") from None
            self._fallback_notice()
            return False, None

    # --- file ---------------------------------------------------------------------------

    def _read_file(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        if os.name == "posix" and path.stat().st_mode & _GROUP_OTHER_BITS:
            raise ConfigError(
                f"the credentials file {path} is readable by other users",
                hint=f"run: chmod 600 {path}",
            )
        try:
            if path.stat().st_size > MAX_STORE_BYTES:
                raise ConfigError(f"the credentials file {path} is too large")
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ConfigError(f"the credentials file {path} could not be read") from None
        if not isinstance(data, dict):
            raise ConfigError(f"the credentials file {path} is not a JSON object")
        return data

    def _update_file(self, change: Callable[[dict[str, Any]], bool]) -> bool:
        path = self.credentials_path
        existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not existed and os.name == "posix":
            path.parent.chmod(0o700)
        try:
            with filelock.FileLock(f"{path}.lock", timeout=LOCK_TIMEOUT_SECONDS):
                data = self._read_file(path)
                if not change(data):
                    return False
                write_atomic(path, json.dumps(data).encode("utf-8"), mode=0o600)
                return True
        except filelock.Timeout:
            raise ConfigError(f"the credentials file {path} is locked by another process") from None

    # --- public -------------------------------------------------------------------------

    def get(self, profile: str) -> StoredCredential | None:
        """Return the stored credential of ``profile`` or None."""
        used, raw = self._with_keyring(lambda b: b.get_password(self._service, profile))
        if used and isinstance(raw, str):
            return self._parse_json(profile, raw)
        if self._mode() == "keyring":
            return None
        entry = self._read_file(self.credentials_path).get(profile)
        return None if entry is None else self._parse(profile, entry)

    def put(self, profile: str, credential: StoredCredential) -> None:
        """Store ``credential`` for ``profile``, replacing the previous one."""
        payload = credential.to_storage_json()
        used, _ = self._with_keyring(lambda b: b.set_password(self._service, profile, payload))
        if used:
            self._drop_file_entry(profile)
            return
        entry = credential.to_storage_dict()

        def change(data: dict[str, Any]) -> bool:
            data[profile] = entry
            return True

        self._update_file(change)

    def delete(self, profile: str) -> bool:
        """Remove the credential of ``profile`` everywhere; True when something was removed."""
        from keyring.errors import PasswordDeleteError

        def remove(backend: KeyringBackend) -> bool:
            if backend.get_password(self._service, profile) is None:
                return False
            try:
                backend.delete_password(self._service, profile)
            except PasswordDeleteError:
                return False
            return True

        _, from_keyring = self._with_keyring(remove)
        return self._drop_file_entry(profile) or bool(from_keyring)

    def _drop_file_entry(self, profile: str) -> bool:
        if not self.credentials_path.exists():
            return False

        def change(data: dict[str, Any]) -> bool:
            return data.pop(profile, None) is not None

        return self._update_file(change)

    def _parse_json(self, profile: str, raw: str) -> StoredCredential:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            entry = None
        return self._parse(profile, entry)

    def _parse(self, profile: str, entry: Any) -> StoredCredential:
        """Validate a stored entry; a damaged entry or an unknown kind cannot be used.

        ``delete`` still removes such an entry, so ``contractum logout`` clears it.
        """
        try:
            return StoredCredential.model_validate(entry)
        except ValidationError:
            raise AuthError(
                f"the stored credential for profile {profile!r} cannot be used",
                hint=RELOGIN_HINT,
            ) from None
