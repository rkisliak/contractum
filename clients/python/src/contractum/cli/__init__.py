"""The ``contractum`` command line."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import traceback
from pathlib import Path
from typing import Any, NoReturn

import click

from contractum.cli import output
from contractum.cli.context import GlobalOptions
from contractum.errors import ContractumError

COMMANDS: dict[str, str] = {
    "login": "contractum.cli.auth:login",
    "logout": "contractum.cli.auth:logout",
    "whoami": "contractum.cli.auth:whoami",
    "config": "contractum.cli.profiles:config",
    "list": "contractum.cli.fetch:list_cmd",
    "versions": "contractum.cli.fetch:versions",
    "show": "contractum.cli.fetch:show",
    "pull": "contractum.cli.fetch:pull",
    "test": "contractum.cli.run:test_cmd",
    "compare": "contractum.cli.compare:compare",
}

EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
EXIT_BROKEN_PIPE = 141


class _PipeClosedError(Exception):
    """A command hit a closed stdout; raised past click, which would exit 1 on EPIPE."""


class LazyGroup(click.Group):
    """A group that imports a command's module only when the command is looked up."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = set(self.commands)
        for name, target in COMMANDS.items():
            module_name = target.partition(":")[0]
            if importlib.util.find_spec(module_name) is not None:
                names.add(name)
        return sorted(names)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        target = COMMANDS.get(cmd_name)
        if target is None:
            return None
        module_name, _, attribute = target.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                return None
            raise
        loaded: click.Command | None = getattr(module, attribute, None)
        return loaded

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except BrokenPipeError as exc:
            raise _PipeClosedError from exc


@click.group(cls=LazyGroup)
@click.version_option(package_name="contractum", message="contractum %(version)s")
@click.option("--profile", metavar="NAME", default=None, help="Connection profile to use.")
@click.option("--url", metavar="URL", default=None, help="Contractum API base URL.")
@click.option(
    "--ca-bundle",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="PEM file with the CA certificates to trust.",
)
@click.option(
    "--allow-insecure-http",
    is_flag=True,
    default=False,
    help="Allow a plain http URL (local development only).",
)
@click.option("--no-color", is_flag=True, default=False, help="Do not use colors.")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Print only results and errors.")
@click.pass_context
def cli(
    ctx: click.Context,
    profile: str | None,
    url: str | None,
    ca_bundle: Path | None,
    allow_insecure_http: bool,
    no_color: bool,
    quiet: bool,
) -> None:
    """Fetch published data contracts and test your data against them."""
    ctx.obj = GlobalOptions(
        profile=profile,
        url=url,
        ca_bundle=ca_bundle,
        allow_insecure_http=allow_insecure_http,
        color=not no_color,
        quiet=quiet,
    )


def _silence_stdout() -> None:
    """Point stdout at the null device so the interpreter's exit flush does not fail again."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        sys.stdout = open(os.devnull, "w")  # held open for the rest of the process


def _report_contractum_error(exc: ContractumError) -> int:
    output.error(str(exc))
    if exc.hint:
        click.echo(f"hint: {exc.hint}", err=True)
    return exc.exit_code


def _report_internal_error(exc: Exception) -> int:
    click.echo(f"internal error: {type(exc).__name__}", err=True)
    if os.environ.get("CONTRACTUM_DEBUG") == "1":
        traceback.print_exc()
    else:
        click.echo("set CONTRACTUM_DEBUG=1 for a traceback", err=True)
    return EXIT_USAGE


def _run(argv: list[str] | None) -> int:
    try:
        result = cli.main(args=argv, prog_name="contractum", standalone_mode=False)
    except (click.exceptions.Abort, KeyboardInterrupt):
        click.echo("aborted", err=True)
        return EXIT_INTERRUPTED
    except click.ClickException as exc:
        exc.show()
        return EXIT_USAGE
    except (BrokenPipeError, _PipeClosedError):
        _silence_stdout()
        return EXIT_BROKEN_PIPE
    except ContractumError as exc:
        return _report_contractum_error(exc)
    except Exception as exc:  # the last line of defence must never exit 1
        return _report_internal_error(exc)
    return result if isinstance(result, int) else 0


def main(argv: list[str] | None = None) -> NoReturn:
    """Run the CLI and exit: 0 ok, 1 a command reported a data mismatch, 2 any failure."""
    sys.exit(_run(argv))
