"""Test doubles for the credential store: in-memory and failing keyring backends."""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass

import pytest
from keyring.errors import KeyringError, PasswordDeleteError

from contractum.cli import main


class MemoryKeyring:
    """A keyring backend that keeps passwords in a dictionary."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.entries.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.entries[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        if (service_name, username) not in self.entries:
            raise PasswordDeleteError("not found")
        del self.entries[(service_name, username)]


class FailingKeyring:
    """A keyring backend whose every operation fails like a locked or unavailable keyring."""

    def get_password(self, service_name: str, username: str) -> str | None:
        raise KeyringError("unavailable")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise KeyringError("unavailable")

    def delete_password(self, service_name: str, username: str) -> None:
        raise KeyringError("unavailable")


@dataclass(frozen=True)
class Outcome:
    """The exit code and output of one ``main`` run."""

    code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def run_main(
    argv: list[str],
    *,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stdin: str = "",
) -> Outcome:
    """Run the real ``main`` entry (so exit codes are the documented ones) and capture it."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    capsys.readouterr()
    with pytest.raises(SystemExit) as raised:
        main(argv)
    captured = capsys.readouterr()
    code = raised.value.code
    return Outcome(code if isinstance(code, int) else 1, captured.out, captured.err)
