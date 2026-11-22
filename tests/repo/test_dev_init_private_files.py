"""dev-init writes every secret file atomically with its final owner and mode from the start.

``_write_private`` creates a new sibling with O_EXCL | O_NOFOLLOW and mode 0600, sets owner and
mode on the descriptor and renames it over the target, so the bytes are never readable by others
and a planted symlink is replaced, never followed.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX file ownership"),
]

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "dev-init" / "generate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dev_init_generate_private", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dev_init_generate_private"] = module
    spec.loader.exec_module(module)
    return module


gen = _load()


def _owner() -> dict[str, int]:
    return {"uid": os.getuid(), "gid": os.getgid()}


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_a_new_file_gets_the_bytes_and_the_final_mode(tmp_path: Path, mode: int) -> None:
    target = tmp_path / "postgres_password"
    gen._write_private(target, b"value-1", mode=mode, **_owner())

    assert target.read_bytes() == b"value-1"
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert [p.name for p in tmp_path.iterdir()] == ["postgres_password"]


def test_an_existing_read_only_file_is_replaced(tmp_path: Path) -> None:
    target = tmp_path / "jwt_private_key"
    gen._write_private(target, b"old", mode=0o400, **_owner())
    gen._write_private(target, b"new", mode=0o400, **_owner())

    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o400


def test_a_planted_symlink_is_replaced_and_its_target_left_alone(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"untouched")
    target = tmp_path / "bootstrap_admin_secret"
    target.symlink_to(elsewhere)

    gen._write_private(target, b"secret-value", mode=0o600, **_owner())

    assert not target.is_symlink()
    assert target.read_bytes() == b"secret-value"
    assert elsewhere.read_bytes() == b"untouched"


def test_a_failed_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise PermissionError("chown refused")

    monkeypatch.setattr(gen.os, "fchown", refuse)
    with pytest.raises(PermissionError):
        gen._write_private(tmp_path / "nats.conf", b"x", mode=0o400, **_owner())
    assert list(tmp_path.iterdir()) == []
