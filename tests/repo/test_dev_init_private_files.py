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


# --- consumer subpaths: owner-only, except the group-readable e2e subpath ---------------------
# The test runners of compose.test.yaml run as the invoking host user with the group 10001, so the
# e2e subpath alone is group-readable (0550 and 0440); every other consumer stays owner-only.


@pytest.fixture
def out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """OUT in tmp_path, owned by the test user. dev-init runs as root, which writes into its 0500
    and 0550 directories; a non-root test keeps them writable and checks the modes separately."""
    me = (os.getuid(), os.getgid())
    monkeypatch.setattr(gen, "OUT", tmp_path)
    monkeypatch.setattr(gen, "OWNERS", {"e2e": me, "identity": me})

    def ensure_dir(consumer: str) -> Path:
        path = tmp_path / consumer
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(gen, "_ensure_dir", ensure_dir)
    return tmp_path


def test_the_e2e_subpath_is_owned_by_10001_and_group_readable() -> None:
    assert gen.OWNERS["e2e"] == (10001, 10001)
    assert gen.GROUP_READABLE == {"e2e"}
    assert gen._modes("e2e") == (0o550, 0o440)
    for consumer in set(gen.OWNERS) - {"e2e"}:
        assert gen._modes(consumer) == (0o500, 0o400), consumer


@pytest.mark.parametrize(("consumer", "file_mode"), [("e2e", 0o440), ("identity", 0o400)])
def test_a_consumer_file_gets_its_mode(out: Path, consumer: str, file_mode: int) -> None:
    assert gen._write(consumer, "secret", b"value") is True
    assert stat.S_IMODE((out / consumer / "secret").stat().st_mode) == file_mode
    # A second run with the same bytes and permissions changes nothing.
    assert gen._write(consumer, "secret", b"value") is False


def test_a_warm_file_with_a_stale_mode_keeps_its_bytes_and_gets_the_new_mode(out: Path) -> None:
    directory = out / "e2e"
    directory.mkdir()
    gen._write_private(directory / "ca.crt", b"warm", mode=0o400, **_owner())

    assert gen._write("e2e", "ca.crt", b"would-be-new") is True

    assert (directory / "ca.crt").read_bytes() == b"warm"
    assert stat.S_IMODE((directory / "ca.crt").stat().st_mode) == 0o440
