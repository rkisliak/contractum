"""The exit-code contract of main(), the lazy command map and the lifecycle options."""

from __future__ import annotations

import importlib.machinery
import importlib.metadata
import io
import os
import subprocess
import sys
import textwrap
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import click
import pytest
import respx
from click.testing import CliRunner
from httpx import ConnectError, Response

from contractum import cli as cli_module
from contractum.cli import COMMANDS, LazyGroup, cli, main
from contractum.cli.context import GlobalOptions, get_options, lifecycle_options
from contractum.cli.output import error, info, stdout_console, warn
from contractum.errors import (
    AmbiguousReferenceError,
    ContractumError,
    ExtraNotInstalledError,
    RateLimitedError,
    ServerError,
    UsageError,
)

SECRET = "secret-value"


@pytest.fixture
def plant() -> Iterator[Callable[[Callable[[], Any]], None]]:
    """Add a command named ``planted`` that runs the given callable, removed afterwards."""

    def add(body: Callable[[], Any]) -> None:
        @click.command(name="planted")
        def planted() -> Any:
            return body()

        cli.add_command(planted)

    yield add
    cli.commands.pop("planted", None)


def _raiser(exc: BaseException) -> Callable[[], Any]:
    def body() -> Any:
        raise exc

    return body


def _exit_code(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert isinstance(raised.value.code, int)
    return raised.value.code


def test_contractum_error_exits_2_with_message(
    plant: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    plant(_raiser(ContractumError("boom", hint="try again")))
    assert _exit_code(["planted"]) == 2
    err = capsys.readouterr().err
    assert "error: boom" in err
    assert "hint: try again" in err


def test_contractum_error_without_hint_prints_no_hint(
    plant: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    plant(_raiser(UsageError("bad call")))
    assert _exit_code(["planted"]) == 2
    assert "hint:" not in capsys.readouterr().err


def test_internal_error_exits_2_and_never_prints_the_message(
    plant: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    plant(_raiser(RuntimeError(SECRET)))
    assert _exit_code(["planted"]) == 2
    captured = capsys.readouterr()
    assert "internal error: RuntimeError" in captured.err
    assert "CONTRACTUM_DEBUG=1" in captured.err
    assert SECRET not in captured.err + captured.out
    assert "Traceback" not in captured.err


def test_debug_env_prints_a_traceback(
    plant: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTRACTUM_DEBUG", "1")
    plant(_raiser(RuntimeError(SECRET)))
    assert _exit_code(["planted"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "internal error: RuntimeError" in err


def test_unknown_option_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert _exit_code(["--no-such-option"]) == 2
    assert "No such option" in capsys.readouterr().err


def test_unknown_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert _exit_code(["no-such-command"]) == 2
    assert "No such command" in capsys.readouterr().err


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), click.exceptions.Abort()])
def test_interrupt_exits_130(
    plant: Callable[..., None], capsys: pytest.CaptureFixture[str], exc: BaseException
) -> None:
    plant(_raiser(exc))
    assert _exit_code(["planted"]) == 130
    assert "aborted" in capsys.readouterr().err


def test_broken_pipe_exits_141_and_replaces_a_stdout_without_a_descriptor(
    plant: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    plant(_raiser(BrokenPipeError()))
    assert _exit_code(["planted"]) == 141
    replacement = sys.stdout
    try:
        assert replacement.name == os.devnull
    finally:
        replacement.close()


def test_broken_pipe_points_the_stdout_descriptor_at_the_null_device(
    plant: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []

    class FakeStdout:
        def fileno(self) -> int:
            return 77

    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(os, "open", lambda path, flags: 55)
    monkeypatch.setattr(os, "dup2", lambda source, target: calls.append((source, target)))
    plant(_raiser(BrokenPipeError()))
    assert _exit_code(["planted"]) == 141
    assert calls == [(55, 77)]


def test_broken_pipe_in_a_real_process_exits_141(tmp_path: Path) -> None:
    script = tmp_path / "plant.py"
    script.write_text(
        textwrap.dedent(
            """
            import click
            from contractum.cli import cli, main

            @click.command(name="planted")
            def planted():
                raise BrokenPipeError()

            cli.add_command(planted)
            main(["planted"])
            """
        )
    )
    done = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 141
    assert done.stderr == ""


def test_command_returning_1_exits_1(plant: Callable[..., None]) -> None:
    plant(lambda: 1)
    assert _exit_code(["planted"]) == 1


def test_command_returning_none_exits_0(plant: Callable[..., None]) -> None:
    plant(lambda: None)
    assert _exit_code(["planted"]) == 0


CLIENT_VERSION = importlib.metadata.version("contractum")
SERVER = "https://contractum.example.test"


def test_version_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    # No server configured (isolated environment): only the client line.
    assert _exit_code(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"contractum {CLIENT_VERSION}"


@respx.mock(assert_all_called=True)
def test_version_also_names_the_configured_server_version(
    capsys: pytest.CaptureFixture[str], respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{SERVER}/api/v1/identity/version").mock(
        return_value=Response(
            200,
            json={"service": "identity", "version": "1.4.0", "commit": "c", "built_at": "t"},
        )
    )
    assert _exit_code(["--url", SERVER, "--version"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"contractum {CLIENT_VERSION}", "server: 1.4.0"]
    request = route.calls.last.request
    assert "authorization" not in request.headers  # public route: no token is sent


@pytest.mark.parametrize(
    "answer",
    [
        ConnectError("refused"),
        Response(503, json={}),
        Response(200, text="not json"),
        Response(200, json={"version": 7}),
        Response(200, json={"version": "1.4.0\x1b[31m"}),
        Response(200, json={"service": "identity"}),
        Response(200, json=["1.4.0"]),
        Response(200, content=b"\xff\xfe"),
    ],
    ids=[
        "unreachable",
        "server-error",
        "not-json",
        "not-a-string",
        "control-characters",
        "no-version",
        "not-an-object",
        "not-utf8",
    ],
)
def test_a_server_version_failure_reads_unavailable_and_still_exits_0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, answer: object
) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", SERVER)
    with respx.mock() as mock:
        route = mock.get(f"{SERVER}/api/v1/identity/version")
        if isinstance(answer, Exception):
            route.mock(side_effect=answer)
        else:
            route.mock(return_value=answer)
        assert _exit_code(["--version"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"contractum {CLIENT_VERSION}",
        "server: unavailable",
    ]


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("CONTRACTUM_URL", "http://contractum.example"),
        ("CONTRACTUM_CA_BUNDLE", "/nonexistent/ca.pem"),
    ],
    ids=["insecure-url", "unreadable-ca-bundle"],
)
def test_an_unusable_server_setting_reads_unavailable_without_a_request(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv("CONTRACTUM_URL", SERVER)
    monkeypatch.setenv(variable, value)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{SERVER}/api/v1/identity/version")
        assert _exit_code(["--version"]) == 0
        assert not route.called
    assert capsys.readouterr().out.splitlines() == [
        f"contractum {CLIENT_VERSION}",
        "server: unavailable",
    ]


def test_without_a_command_or_version_the_cli_asks_for_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _exit_code(["--quiet"]) == 2
    assert "Missing command." in capsys.readouterr().err


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert _exit_code(["--help"]) == 0
    assert "--profile" in capsys.readouterr().out


def test_main_without_argv_reads_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["contractum", "--version"])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 0


def test_command_names_cover_the_documented_set() -> None:
    assert sorted(COMMANDS) == sorted(
        [
            "login",
            "logout",
            "whoami",
            "config",
            "list",
            "versions",
            "show",
            "pull",
            "test",
            "compare",
        ]
    )


def test_list_commands_lists_only_importable_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(COMMANDS, "ghost", "contractum.cli.no_such_module:ghost")
    with click.Context(cli) as ctx:
        names = cli.list_commands(ctx)
    assert "ghost" not in names
    assert names == sorted(names)


def test_get_command_returns_none_for_a_missing_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(COMMANDS, "ghost", "contractum.cli.no_such_module:ghost")
    with click.Context(cli) as ctx:
        assert cli.get_command(ctx, "ghost") is None
        assert cli.get_command(ctx, "not-in-the-map") is None


def test_get_command_loads_a_lazy_module(monkeypatch: pytest.MonkeyPatch) -> None:
    @click.command(name="lazy")
    def lazy() -> None:
        return None

    module = types.ModuleType("contractum.cli._lazy_planted")
    module.__spec__ = importlib.machinery.ModuleSpec("contractum.cli._lazy_planted", None)
    module.lazy = lazy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "contractum.cli._lazy_planted", module)
    monkeypatch.setitem(COMMANDS, "lazy", "contractum.cli._lazy_planted:lazy")
    with click.Context(cli) as ctx:
        assert cli.get_command(ctx, "lazy") is lazy
        assert "lazy" in cli.list_commands(ctx)


def test_get_command_reraises_a_missing_dependency_of_the_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "planted_broken.py").write_text("import module_that_does_not_exist\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(COMMANDS, "broken", "planted_broken:broken")
    with click.Context(cli) as ctx, pytest.raises(ModuleNotFoundError):
        cli.get_command(ctx, "broken")


def test_group_is_lazy() -> None:
    assert isinstance(cli, LazyGroup)
    assert cli_module.COMMANDS is COMMANDS


def test_global_options_land_in_the_context(tmp_path: Path) -> None:
    bundle = tmp_path / "ca.pem"
    bundle.write_text("pem")
    seen: dict[str, GlobalOptions] = {}

    @click.command(name="probe")
    @click.pass_context
    def probe(ctx: click.Context) -> None:
        seen["options"] = get_options(ctx)

    cli.add_command(probe)
    try:
        result = CliRunner().invoke(
            cli,
            [
                "--profile",
                "prod",
                "--url",
                "https://contracts.example.test",
                "--ca-bundle",
                str(bundle),
                "--allow-insecure-http",
                "--no-color",
                "-q",
                "probe",
            ],
        )
    finally:
        cli.commands.pop("probe", None)
    assert result.exit_code == 0
    assert seen["options"] == GlobalOptions(
        profile="prod",
        url="https://contracts.example.test",
        ca_bundle=bundle,
        allow_insecure_http=True,
        color=False,
        quiet=True,
    )


def test_get_options_defaults_outside_the_group() -> None:
    with click.Context(click.Command("x")) as ctx:
        assert get_options(ctx) == GlobalOptions()


def test_missing_ca_bundle_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert _exit_code(["--ca-bundle", str(tmp_path / "missing.pem"), "whoami"]) == 2
    assert "does not exist" in capsys.readouterr().err


def _lifecycle_command() -> click.Command:
    @click.command(name="lc")
    @lifecycle_options
    def lc(fail_on_deprecated: bool, sunset_within_days: int | None) -> None:
        click.echo(f"{fail_on_deprecated} {sunset_within_days}")

    return lc


def test_lifecycle_options_defaults() -> None:
    result = CliRunner().invoke(_lifecycle_command(), [])
    assert result.exit_code == 0
    assert result.output.strip() == "False None"


def test_lifecycle_options_values() -> None:
    result = CliRunner().invoke(
        _lifecycle_command(), ["--fail-on-deprecated", "--sunset-within", "30"]
    )
    assert result.exit_code == 0
    assert result.output.strip() == "True 30"


def test_lifecycle_sunset_within_rejects_a_negative_number() -> None:
    result = CliRunner().invoke(_lifecycle_command(), ["--sunset-within", "-1"])
    assert result.exit_code == 2


def test_lifecycle_sunset_within_exits_2_through_main(
    plant: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    cli.add_command(_lifecycle_command())
    try:
        assert _exit_code(["lc", "--sunset-within", "abc"]) == 2
    finally:
        cli.commands.pop("lc", None)
    assert "Invalid value" in capsys.readouterr().err


def test_output_helpers(capsys: pytest.CaptureFixture[str]) -> None:
    info("progress")
    info("loud", GlobalOptions())
    info("hidden", GlobalOptions(quiet=True))
    warn("careful")
    error("broken")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == ["progress", "loud", "warning: careful", "error: broken"]


def test_stdout_console_is_plain_without_a_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    console = stdout_console(GlobalOptions())
    console.print("[bold red]hello[/]")
    assert capsys.readouterr().out == "hello\n"


def test_stdout_console_honours_no_color_and_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert stdout_console(GlobalOptions(color=False)).no_color is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert stdout_console(GlobalOptions()).no_color is True
    monkeypatch.delenv("NO_COLOR")
    assert stdout_console(GlobalOptions()).no_color is False


def test_stdout_console_uses_color_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert stdout_console(GlobalOptions()).no_color is False


def test_error_messages_never_include_more_than_their_fields() -> None:
    error_ = ServerError(
        503, "https://contractum.example/problems/unavailable", "Unavailable", "try later"
    )
    assert str(error_) == (
        "Unavailable (HTTP 503, https://contractum.example/problems/unavailable): try later"
    )
    bare = ServerError(500)
    assert str(bare) == "server error (HTTP 500)"
    assert (bare.status, bare.problem_type, bare.title, bare.detail) == (500, None, None, None)
    assert RateLimitedError("slow down", retry_after=2.0).retry_after == 2.0
    ambiguous = AmbiguousReferenceError("two matches", ["a", "b"])
    assert ambiguous.candidates == ("a", "b")
    extra = ExtraNotInstalledError("pandas", "testing a DataFrame")
    assert str(extra) == (
        "testing a DataFrame needs the optional extra: pip install 'contractum[pandas]'"
    )
    assert (extra.extra, extra.feature) == ("pandas", "testing a DataFrame")


def test_every_error_class_exits_2() -> None:
    from contractum import errors

    classes = [
        value
        for value in vars(errors).values()
        if isinstance(value, type) and issubclass(value, ContractumError)
    ]
    assert len(classes) == 18
    assert {cls.exit_code for cls in classes} == {2}


def test_python_dash_m_runs_the_cli() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "contractum", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert done.stdout.strip() == f"contractum {CLIENT_VERSION}"
