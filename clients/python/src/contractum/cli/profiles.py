"""The ``config`` command group: named connection profiles in the profiles file.

The file never holds credentials; a key that looks like one is refused. Nothing here reads the
credential store.
"""

from __future__ import annotations

import os

import click

from contractum import config as profiles
from contractum.cli import output
from contractum.cli.context import get_options
from contractum.errors import ConfigError


def _profile_name(ctx: click.Context, loaded: profiles.ConfigFile) -> str:
    """The profile a subcommand acts on: the flag, CONTRACTUM_PROFILE, the default or `default`."""
    return (
        get_options(ctx).profile
        or os.environ.get(profiles.ENV_PROFILE, "").strip()
        or loaded.default_profile
        or profiles.DEFAULT_PROFILE
    )


@click.group()
def config() -> None:
    """Manage connection profiles (URL, CA bundle, insecure HTTP); never credentials.

    Use the global --profile NAME option to act on a profile other than the default one, for
    example: contractum --profile prod config set url https://contractum.example.com
    """


@config.command("path")
def path_cmd() -> None:
    """Print the profiles file location."""
    click.echo(str(profiles.config_path()))


@config.command("list")
def list_cmd() -> None:
    """List the profiles and their values; the default profile is marked with *."""
    loaded = profiles.load_config()
    if not loaded.profiles:
        output.info("no profiles are defined")
        return
    for name in sorted(loaded.profiles):
        marker = "*" if name == loaded.default_profile else " "
        click.echo(f"{marker} {name}")
        profile = loaded.profiles[name]
        for key in profiles.PROFILE_KEYS:
            value = getattr(profile, key)
            if value is not None:
                text = str(value).lower() if isinstance(value, bool) else value
                click.echo(f"    {key} = {text}")


@config.command("get")
@click.argument("key")
@click.pass_context
def get_cmd(ctx: click.Context, key: str) -> None:
    """Print one value of the profile."""
    loaded = profiles.load_config()
    name = _profile_name(ctx, loaded)
    if key not in profiles.PROFILE_KEYS:
        raise ConfigError(
            f"unknown profile key {key!r}", hint=f"valid keys: {', '.join(profiles.PROFILE_KEYS)}"
        )
    stored = loaded.profiles.get(name)
    value = getattr(stored, key, None) if stored is not None else None
    if value is None:
        raise ConfigError(f"{key} is not set in profile {name!r}")
    click.echo(str(value).lower() if isinstance(value, bool) else str(value))


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def set_cmd(ctx: click.Context, key: str, value: str) -> None:
    """Set one value of the profile, creating the profile when needed."""
    name = _profile_name(ctx, profiles.load_config())
    profiles.set_profile_value(name, key, value)
    output.info(f"set {key} in profile {name!r}", get_options(ctx))


@config.command("unset")
@click.argument("key")
@click.pass_context
def unset_cmd(ctx: click.Context, key: str) -> None:
    """Remove one value from the profile."""
    name = _profile_name(ctx, profiles.load_config())
    profiles.unset_profile_value(name, key)
    output.info(f"unset {key} in profile {name!r}", get_options(ctx))


@config.command("use")
@click.argument("name")
@click.pass_context
def use_cmd(ctx: click.Context, name: str) -> None:
    """Make NAME the default profile."""
    loaded = profiles.load_config()
    if name != profiles.DEFAULT_PROFILE and name not in loaded.profiles:
        known = ", ".join(sorted(loaded.profiles)) or "none"
        raise ConfigError(
            f"profile {name!r} is not defined",
            hint=f"known: {known}; create it with: contractum --profile {name} config set url URL",
        )
    profiles.set_default_profile(name)
    output.info(f"default profile is now {name!r}", get_options(ctx))
