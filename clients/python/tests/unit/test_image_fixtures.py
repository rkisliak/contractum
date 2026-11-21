"""The image smoke fixtures stay consistent and the CLI Dockerfile stays hardened (SDK-03, D-08)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from contractum.cli import main
from contractum.lock import load_lock

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "image"
DOCKERFILE = ROOT / "Dockerfile"
SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)


def _final_stage() -> str:
    """The text of the last build stage (from its last FROM line on)."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"(?m)^FROM ", text)]
    return text[starts[-1] :]


def _logical_lines(text: str) -> list[str]:
    """Dockerfile instructions with backslash continuations joined and comments dropped."""
    joined = re.sub(r"\\\r?\n", " ", text)
    return [ln.strip() for ln in joined.splitlines() if ln.strip() and not ln.startswith("#")]


def _frozen_offline(data: str) -> int:
    argv = [
        "test",
        str(FIXTURES / data),
        "--contract",
        "payments.orders",
        "--lock",
        str(FIXTURES / "contractum.lock"),
        "--frozen",
        "--offline",
    ]
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert isinstance(raised.value.code, int)
    return raised.value.code


@pytest.mark.unit
def test_lock_hash_equals_the_pulled_file() -> None:
    lock = load_lock(FIXTURES / "contractum.lock")
    assert len(lock.contracts) == 1
    entry = lock.contracts[0]
    pulled = FIXTURES / entry.file
    assert hashlib.sha256(pulled.read_bytes()).hexdigest() == entry.content_sha256
    assert pulled.read_bytes() == (FIXTURES / "payments.orders.yaml").read_bytes()


@pytest.mark.unit
def test_frozen_offline_exits_0_on_conforming_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTRACTUM_URL", raising=False)
    assert _frozen_offline("orders.csv") == 0


@pytest.mark.unit
def test_frozen_offline_exits_1_on_the_bad_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTRACTUM_URL", raising=False)
    assert _frozen_offline("orders_bad.csv") == 1


@pytest.mark.unit
def test_final_stage_upgrades_and_drops_pip_before_dropping_privileges() -> None:
    stage = _final_stage()
    user = re.search(r"(?m)^USER ", stage)
    assert user is not None
    assert stage.index("apt-get upgrade") < user.start()
    assert stage.index("/usr/local/bin/python -m pip uninstall") < user.start()
    assert "useradd --system --create-home --uid 10001 contractum" in stage
    assert "USER contractum" in stage


@pytest.mark.unit
def test_dockerfile_ends_with_the_contractum_entrypoint() -> None:
    last_entrypoint = [ln for ln in _logical_lines(_final_stage()) if ln.startswith("ENTRYPOINT")]
    assert last_entrypoint == ['ENTRYPOINT ["contractum"]']


@pytest.mark.unit
def test_dockerfile_bakes_no_credential() -> None:
    names: list[str] = []
    for line in _logical_lines(DOCKERFILE.read_text(encoding="utf-8")):
        keyword, _, rest = line.partition(" ")
        if keyword == "ARG":
            names.append(rest.split("=", 1)[0].strip())
        elif keyword == "ENV":
            names.extend(re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", rest))
    assert names, "the parser found no ARG or ENV names, so it checks nothing"
    assert [n for n in names if SECRET_NAME.search(n)] == []
