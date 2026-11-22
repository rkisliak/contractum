"""``pull`` keeps the pinned bytes exact through Git, and every text file the SDK writes is LF."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from support.auth import run_main
from support.fake_server import FakeContractum, FakeVersion
from support.reports import all_statuses

from contractum._gitattributes import ensure_lines, protect
from contractum.cache import Cache
from contractum.client import Client
from contractum.config import set_profile_value
from contractum.contracts import ContractVersion
from contractum.errors import FrozenLockError
from contractum.pull import pull
from contractum.report.json_out import report_to_json, write_json
from contractum.report.junit import report_to_junit
from contractum.report.markdown import report_to_markdown

BASE = "https://contractum.example"
# The server's own bytes hold a CRLF: only ``-text`` keeps them exact through a commit.
DOC = b"apiVersion: v3.0.2\nkind: DataContract\nid: payments.orders\r\nversion: 1.4.0\n"
SRC = Path(__file__).resolve().parents[2] / "src" / "contractum"


def _server() -> FakeContractum:
    server = FakeContractum()
    server.add_contract("payments.orders", [FakeVersion("1.4.0", DOC, status="latest")])
    return server


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, canary_token: str) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", BASE)
    monkeypatch.setenv("CONTRACTUM_TOKEN", canary_token)


def _pull(root: Path, token: str, *, gitattributes: bool = True) -> None:
    with _server(), Client(BASE, token=token) as client:
        pull(
            client,
            ["payments.orders@1.4.0"],
            directory=root / "contracts",
            lock_path=root / "contractum.lock",
            cache=Cache(root / "cache"),
            gitattributes=gitattributes,
        )


def _assert_lf(data: bytes) -> None:
    assert b"\r" not in data
    assert data.endswith(b"\n")


# --- ensure_lines -----------------------------------------------------------------------------


def test_a_new_file_gets_the_rules_with_lf(tmp_path: Path) -> None:
    target = tmp_path / ".gitattributes"
    assert ensure_lines(target, ["* -text", "* -text"]) is True
    assert target.read_bytes() == b"* -text\n"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o644


def test_missing_rules_are_appended_and_the_user_content_is_kept(tmp_path: Path) -> None:
    target = tmp_path / ".gitattributes"
    original = b"# mine\r\n*.png binary\r\n*.sh text eol=lf"
    target.write_bytes(original)
    if os.name == "posix":
        target.chmod(0o640)
    assert ensure_lines(target, ["*.png binary", "/contractum.lock -text"]) is True
    assert target.read_bytes() == original + b"\n/contractum.lock -text\n"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o640


def test_rules_already_present_leave_the_file_untouched(tmp_path: Path) -> None:
    target = tmp_path / ".gitattributes"
    original = b"*   -text\r\n/contractum.lock\t-text\n"
    target.write_bytes(original)
    before = target.stat().st_mtime_ns
    assert ensure_lines(target, ["* -text", "/contractum.lock -text"]) is False
    assert target.read_bytes() == original
    assert target.stat().st_mtime_ns == before


# --- pull -------------------------------------------------------------------------------------


def test_pull_protects_the_contracts_folder_and_the_lock(tmp_path: Path, canary_token: str) -> None:
    _pull(tmp_path, canary_token)
    assert (tmp_path / "contracts" / ".gitattributes").read_bytes() == b"* -text\n"
    assert (tmp_path / ".gitattributes").read_bytes() == b"/contractum.lock -text\n"
    _pull(tmp_path, canary_token)  # idempotent
    assert (tmp_path / "contracts" / ".gitattributes").read_bytes() == b"* -text\n"
    assert (tmp_path / ".gitattributes").read_bytes() == b"/contractum.lock -text\n"


def test_contracts_in_the_lock_folder_get_one_line_per_file(
    tmp_path: Path, canary_token: str
) -> None:
    with _server(), Client(BASE, token=canary_token) as client:
        result = pull(
            client,
            ["payments.orders@1.4.0"],
            directory=tmp_path,
            lock_path=tmp_path / "contractum.lock",
            cache=Cache(tmp_path / "cache"),
        )
    (item,) = result.contracts
    assert (tmp_path / ".gitattributes").read_text(encoding="utf-8").splitlines() == [
        f"/{item.path.name} -text",
        "/contractum.lock -text",
    ]


def test_a_repository_root_never_gets_a_folder_wide_rule(tmp_path: Path, canary_token: str) -> None:
    (tmp_path / "contracts" / ".git").mkdir(parents=True)
    _pull(tmp_path, canary_token)
    (line,) = (tmp_path / "contracts" / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert line.startswith("/payments.orders-1.4.0-")
    assert line.endswith(" -text")


def test_a_lock_name_that_needs_quoting_warns_and_is_skipped(
    tmp_path: Path, canary_token: str
) -> None:
    with (
        _server(),
        Client(BASE, token=canary_token) as client,
        pytest.warns(UserWarning, match="cannot protect the lock file"),
    ):
        pull(
            client,
            ["payments.orders@1.4.0"],
            directory=tmp_path / "contracts",
            lock_path=tmp_path / "my lock.json",
            cache=Cache(tmp_path / "cache"),
        )
    assert not (tmp_path / ".gitattributes").exists()
    assert (tmp_path / "contracts" / ".gitattributes").read_bytes() == b"* -text\n"


def test_an_unwritable_gitattributes_warns_and_the_pull_still_succeeds(tmp_path: Path) -> None:
    (tmp_path / "contracts" / ".gitattributes").mkdir(parents=True)  # a folder cannot be read
    with pytest.warns(UserWarning, match="cannot update"):
        protect(tmp_path / "contracts", tmp_path / "contractum.lock", [])
    assert (tmp_path / ".gitattributes").read_bytes() == b"/contractum.lock -text\n"


def test_the_sdk_opt_out_writes_no_gitattributes(tmp_path: Path, canary_token: str) -> None:
    _pull(tmp_path, canary_token, gitattributes=False)
    assert not (tmp_path / ".gitattributes").exists()
    assert not (tmp_path / "contracts" / ".gitattributes").exists()
    assert (tmp_path / "contractum.lock").exists()


@pytest.mark.parametrize(
    ("argv", "env", "written"),
    [
        (["--no-gitattributes"], None, False),
        ([], "0", False),
        ([], None, True),
    ],
)
def test_the_cli_option_and_its_variable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    env: str | None,
    written: bool,
) -> None:
    monkeypatch.setenv("CONTRACTUM_CACHE_DIR", str(tmp_path / "cache"))
    if env is not None:
        monkeypatch.setenv("CONTRACTUM_GITATTRIBUTES", env)
    with _server():
        outcome = run_main(
            [
                "pull",
                "payments.orders@1.4.0",
                "--dir",
                str(tmp_path / "contracts"),
                "--lock",
                str(tmp_path / "contractum.lock"),
                *argv,
            ],
            capsys=capsys,
            monkeypatch=monkeypatch,
        )
    assert outcome.code == 0
    assert (tmp_path / ".gitattributes").exists() is written
    assert (tmp_path / "contracts" / ".gitattributes").exists() is written


def test_frozen_and_offline_runs_never_write_gitattributes(
    tmp_path: Path, canary_token: str
) -> None:
    _pull(tmp_path, canary_token, gitattributes=False)
    pull(
        None,
        directory=tmp_path / "contracts",
        lock_path=tmp_path / "contractum.lock",
        offline=True,
        cache=Cache(tmp_path / "cache"),
    )
    assert not (tmp_path / ".gitattributes").exists()


# --- Git with core.autocrlf=true --------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    git = shutil.which("git")
    assert git is not None, "these tests need git on PATH"
    empty = repo.parent / "empty.gitconfig"
    empty.touch()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        [
            git,
            "-c",
            "core.autocrlf=true",
            "-c",
            "core.safecrlf=false",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )


def _commit_and_check_out_again(repo: Path) -> None:
    """Commit everything, delete the working tree's files and check them out again."""
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pin")
    for path in (repo / "contracts").iterdir():
        path.unlink()
    (repo / "contractum.lock").unlink()
    _git(repo, "checkout", "-q", "--", ".")


def _offline(repo: Path) -> None:
    pull(
        None,
        directory=repo / "contracts",
        lock_path=repo / "contractum.lock",
        offline=True,
        cache=Cache(repo.parent / "no-cache"),
    )


@pytest.mark.parametrize("protected", [True, False])
def test_only_the_gitattributes_line_keeps_a_checkout_byte_exact(
    tmp_path: Path, canary_token: str, protected: bool
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with _server(), Client(BASE, token=canary_token) as client:
        pull(
            client,
            ["payments.orders@1.4.0"],
            directory=repo / "contracts",
            lock_path=repo / "contractum.lock",
            cache=Cache(tmp_path / "cache"),
            gitattributes=protected,
        )
    _commit_and_check_out_again(repo)
    (document,) = (p for p in (repo / "contracts").iterdir() if p.name != ".gitattributes")
    if protected:
        assert document.read_bytes() == DOC
        _offline(repo)
    else:
        assert document.read_bytes() == DOC.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        with pytest.raises(FrozenLockError, match="does not match the lock"):
            _offline(repo)


# --- every text file the SDK writes is LF -----------------------------------------------------


def test_pull_writes_lf_lock_gitattributes_and_cache_metadata(
    tmp_path: Path, canary_token: str
) -> None:
    _pull(tmp_path, canary_token)
    for path in (
        tmp_path / "contractum.lock",
        tmp_path / ".gitattributes",
        tmp_path / "contracts" / ".gitattributes",
    ):
        _assert_lf(path.read_bytes())
    (meta,) = (tmp_path / "cache").rglob("meta.json")
    _assert_lf(meta.read_bytes())
    (document,) = (tmp_path / "cache").rglob("document")
    assert document.read_bytes() == DOC  # contract bytes are the server's, CRLF included


def test_reports_are_lf(tmp_path: Path) -> None:
    report = all_statuses()
    _assert_lf(report_to_json(report).encode("utf-8"))
    _assert_lf(report_to_junit(report))
    _assert_lf(report_to_markdown(report).encode("utf-8"))
    write_json(report, tmp_path / "report.json")
    _assert_lf((tmp_path / "report.json").read_bytes())


def test_rendered_documents_and_the_profiles_file_are_lf(tmp_path: Path) -> None:
    source = tmp_path / "orders.yaml"
    source.write_bytes(DOC)
    contract = ContractVersion.from_file(source)
    _assert_lf(contract.render("yaml").encode("utf-8"))
    _assert_lf(contract.render("json").encode("utf-8"))
    target = tmp_path / "config.toml"
    set_profile_value("dev", "url", BASE, path=target)
    set_profile_value("prod", "url", BASE, path=target)
    _assert_lf(target.read_bytes())


_TEXT_WRITERS = frozenset({"write_text", "write_bytes"})
# ``open(os.devnull, "w")`` replaces sys.stdout when the CLI drops its output; it is not a file.
_ALLOWED = {("cli/__init__.py", "devnull")}


def _text_mode(call: ast.Call) -> str | None:
    mode = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        if set(mode.value) & set("wax+") and "b" not in mode.value:
            return mode.value
    return None


def _target_name(call: ast.Call) -> str:
    first = call.args[0] if call.args else None
    return first.attr if isinstance(first, ast.Attribute) else ""


def test_every_file_write_goes_through_write_atomic_with_bytes() -> None:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith("_generated/"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _TEXT_WRITERS:
                found.append(f"{relative}:{node.lineno} {name}")
            elif name in {"open", "fdopen"} and _text_mode(node) is not None:
                if (relative, _target_name(node)) not in _ALLOWED:
                    found.append(f"{relative}:{node.lineno} {name} in text mode")
    assert found == [], "write files with contractum._atomic.write_atomic(bytes): " + json.dumps(
        found
    )
