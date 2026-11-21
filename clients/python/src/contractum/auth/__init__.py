"""Credential resolution: which token provider a call uses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from contractum.auth.tokens import BearerAuth, StaticToken, TokenProvider
from contractum.errors import AuthError

if TYPE_CHECKING:
    from contractum.auth.store import SecretStore
    from contractum.config import Settings

__all__ = ["BearerAuth", "StaticToken", "TokenProvider", "resolve_token_provider"]

LOGIN_HINT = "run `contractum login --token -` or set CONTRACTUM_TOKEN"


def resolve_token_provider(
    settings: Settings,
    *,
    store: SecretStore | None = None,
) -> TokenProvider:
    """Return the provider for the configured token.

    Order: ``CONTRACTUM_TOKEN`` (kind ``env``, never written to disk), then the token stored for
    the profile by ``contractum login``, which is used only for the server it was issued for.
    Without either the error says how to get a token. A token is never refreshed.
    """
    if settings.env_token is not None:
        return StaticToken(settings.env_token.get_secret_value(), kind="env")
    from contractum.auth.store import SecretStore, same_server

    active = store if store is not None else SecretStore()
    credential = active.get(settings.profile)
    if credential is None:
        raise AuthError("no token is configured", hint=LOGIN_HINT)
    if not same_server(credential.server, settings.url):
        raise AuthError(
            f"the stored token for profile {settings.profile!r} belongs to {credential.server}",
            hint="run `contractum login --token -` for this server",
        )
    return StaticToken(credential.access_token.get_secret_value(), kind=credential.kind)
