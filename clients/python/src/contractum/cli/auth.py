"""The ``login``, ``whoami`` and ``logout`` commands."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import click

from contractum.auth import resolve_token_provider
from contractum.auth.local import MAX_TOKEN_BYTES, login_token
from contractum.auth.local import logout as forget_token
from contractum.auth.store import SecretStore
from contractum.cli import output
from contractum.cli.context import get_options, settings_from_ctx
from contractum.models import Identity

if TYPE_CHECKING:
    from contractum.config import Settings

_STDIN = "-"
_ARGV_HINT = (
    "pass the token on stdin (--token -) or set CONTRACTUM_TOKEN; "
    "a token on the command line leaks into shell history"
)


def _read_stdin_text() -> str:
    """Read piped input (bounded) and drop one trailing line break."""
    text = sys.stdin.read(MAX_TOKEN_BYTES + 4)
    if text.endswith("\r\n"):
        return text[:-2]
    return text[:-1] if text.endswith("\n") else text


def _signed_in_message(settings: Settings, identity: Identity) -> str:
    return (
        f"signed in to {settings.url} as {identity.display_name} "
        f"({identity.principal_type} via {identity.via})"
    )


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


@click.command()
@click.option(
    "--token",
    "token",
    metavar="-",
    default=None,
    help="Read the token from stdin; the value must be '-'.",
)
@click.pass_context
def login(ctx: click.Context, token: str | None) -> None:
    """Store a token for this profile after checking it with the server.

    Create a personal access token in the web UI (account menu > Access tokens), or use a
    service-account key, then run `printf '%s' "$TOKEN" | contractum login --token -`. On a
    terminal, `contractum login` asks for the token without echo. Pipelines set CONTRACTUM_TOKEN
    instead and do not log in.
    """
    if token is not None and token != _STDIN:
        raise click.UsageError(_ARGV_HINT)
    settings = settings_from_ctx(ctx)
    if token is not None:
        secret = _read_stdin_text()
    elif _stdin_is_tty():
        secret = click.prompt("Token", hide_input=True)
    else:
        raise click.UsageError(
            "there is no terminal to ask for the token; use --token - with the token on stdin, "
            "or set CONTRACTUM_TOKEN"
        )
    identity = login_token(settings, secret, store=SecretStore(), profile=settings.profile)
    output.info(_signed_in_message(settings, identity), get_options(ctx))


def _source_label(kind: str, settings: Settings, store: SecretStore) -> str:
    fixed = {
        "env": "CONTRACTUM_TOKEN (environment)",
        "token": "stored token",
        "entra_token": "stored Entra token",
    }
    if kind in fixed:
        return fixed[kind]
    credential = store.get(settings.profile)
    label = credential.principal_label if credential is not None else None
    noun = "PAT" if kind == "pat" else "service-account key"
    return f"stored {noun} {label}" if label else f"stored {noun}"


@click.command()
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON.")
@click.pass_context
def whoami(ctx: click.Context, as_json: bool) -> None:
    """Show who the current credential belongs to (never the credential itself)."""
    from contractum.client import Client

    settings = settings_from_ctx(ctx)
    store = SecretStore()
    provider = resolve_token_provider(settings, store=store)
    with Client.from_settings(settings, token_provider=provider) as client:
        identity = client.whoami()
    details = {
        "display_name": identity.display_name,
        "username": identity.username or identity.preferred_username,
        "principal_type": identity.principal_type,
        "via": identity.via,
        "idp": identity.idp,
        "read_only": identity.read_only,
        "interactive": identity.interactive,
        "profile": settings.profile,
        "server": settings.url,
        "credential_source": _source_label(provider.kind, settings, store),
        "global_permissions": list(identity.global_permissions),
    }
    if as_json:
        click.echo(json.dumps(details, indent=2))
        return
    for name, value in details.items():
        text = ", ".join(value) if isinstance(value, list) else value
        click.echo(f"{name.replace('_', ' ')}: {text if text not in (None, '') else '-'}")


@click.command()
@click.pass_context
def logout(ctx: click.Context) -> None:
    """Forget the stored token of this profile."""
    settings = settings_from_ctx(ctx)
    message = forget_token(settings, store=SecretStore(), profile=settings.profile)
    output.info(message, get_options(ctx))
