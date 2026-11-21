"""Terminal output helpers: messages go to stderr, data goes to stdout."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import click

from contractum.cli.context import GlobalOptions

if TYPE_CHECKING:
    from rich.console import Console


def info(message: str, options: GlobalOptions | None = None) -> None:
    """Print a progress message to stderr unless ``--quiet`` is on."""
    if options is not None and options.quiet:
        return
    click.echo(message, err=True)


def warn(message: str) -> None:
    """Print a warning to stderr."""
    click.echo(f"warning: {message}", err=True)


def error(message: str) -> None:
    """Print an error to stderr."""
    click.echo(f"error: {message}", err=True)


def stdout_console(options: GlobalOptions) -> Console:
    """Return a rich console for stdout, plain without a terminal or when color is off."""
    from rich.console import Console

    is_tty = sys.stdout.isatty()
    return Console(
        no_color=not options.color or "NO_COLOR" in os.environ,
        color_system="auto" if is_tty else None,
        highlight=False,
    )
