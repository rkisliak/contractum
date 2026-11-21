"""Global CLI options and the option decorators shared by commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import click

if TYPE_CHECKING:
    from contractum.client import Client
    from contractum.config import Settings

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class GlobalOptions:
    """The group-level flags, stored in ``ctx.obj``."""

    profile: str | None = None
    url: str | None = None
    ca_bundle: Path | None = None
    allow_insecure_http: bool = False
    color: bool = True
    quiet: bool = False


def get_options(ctx: click.Context) -> GlobalOptions:
    """Return the global options of the running command, defaults when run outside the group."""
    found = ctx.find_object(GlobalOptions)
    return found if found is not None else GlobalOptions()


def lifecycle_options(f: F) -> F:
    """Add ``--fail-on-deprecated`` and ``--sunset-within DAYS`` to a command.

    The values arrive as the keyword arguments ``fail_on_deprecated`` and ``sunset_within_days``.
    """
    f = click.option(
        "--sunset-within",
        "sunset_within_days",
        type=click.IntRange(min=0),
        default=None,
        metavar="DAYS",
        help="Fail when the contract is scheduled to sunset within DAYS days.",
    )(f)
    f = click.option(
        "--fail-on-deprecated",
        "fail_on_deprecated",
        is_flag=True,
        default=False,
        help="Fail when the contract version is deprecated.",
    )(f)
    return f


def settings_from_ctx(ctx: click.Context) -> Settings:
    """Resolve the connection settings from the global flags, ``CONTRACTUM_*`` and the profile."""
    from contractum.config import Overrides, resolve_settings

    options = get_options(ctx)
    return resolve_settings(
        profile=options.profile,
        overrides=Overrides(
            url=options.url,
            ca_bundle=options.ca_bundle,
            allow_insecure_http=True if options.allow_insecure_http else None,
        ),
    )


def client_from_ctx(ctx: click.Context) -> Client:
    """Build the read-only ``Client`` a command uses for this run."""
    from contractum.client import Client

    return Client.from_settings(settings_from_ctx(ctx))
