"""Token sign-in: a pasted token is verified, then stored per profile; logout forgets it.

A token is verified with ``GET /api/v1/identity/me`` before it is stored. Signing in never
creates a server session, so logout makes no network call: the token stays valid until it
expires or is revoked. Nothing here prints, logs or raises with a token in the message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr

from contractum.auth.store import CredentialKind, SecretStore, StoredCredential
from contractum.cli.output import warn
from contractum.errors import AuthError, ConfigError
from contractum.models import Identity

if TYPE_CHECKING:
    from contractum.config import Settings

MAX_TOKEN_BYTES = 8 * 1024
_PAT_PREFIX = "dcp_"


def _token_problem(token: str) -> str | None:
    if not token:
        return "the token is empty"
    if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        return "the token is longer than 8 KiB"
    if any(ch.isspace() for ch in token):
        return "the token contains whitespace"
    return None


def pat_label(token: str) -> str | None:
    """Return ``dcp_<public_id>`` for a personal access token or service-account key, else None."""
    if not token.startswith(_PAT_PREFIX):
        return None
    parts = token.split("_", 2)
    return f"{_PAT_PREFIX}{parts[1]}" if len(parts) == 3 and parts[1] else None


def _kind_for(identity: Identity) -> CredentialKind:
    if identity.via == "pat":
        return "pat"
    if identity.via == "key":
        return "service_account"
    return "entra_token" if identity.idp == "entra" else "token"


def verify_identity(settings: Settings, token: str) -> Identity:
    """Ask the server who ``token`` belongs to (``GET /api/v1/identity/me``)."""
    from contractum.client import Client

    with Client(
        settings.url,
        token=token,
        ca_bundle=settings.ca_bundle,
        timeout=settings.timeout,
        allow_insecure_http=settings.allow_insecure_http,
    ) as client:
        return client.whoami()


def login_token(settings: Settings, token: str, *, store: SecretStore, profile: str) -> Identity:
    """Verify a pasted token, store it for ``profile`` and return who it belongs to."""
    problem = _token_problem(token)
    if problem is not None:
        raise ConfigError(problem)
    identity = verify_identity(settings, token)
    kind = _kind_for(identity)
    if kind == "pat" and not identity.read_only:
        warn("this token can write; a read-only personal access token is enough for this tool")
    label = pat_label(token) if kind in ("pat", "service_account") else None
    store.put(
        profile,
        StoredCredential(
            kind=kind,
            server=settings.url,
            access_token=SecretStr(token),
            principal_label=label or identity.display_name,
        ),
    )
    return identity


# --- logout ---------------------------------------------------------------------------------

_REVOKE_HINTS: dict[str, str] = {
    "pat": "the token stays valid until you revoke it in the web interface (your tokens)",
    "service_account": "the key stays valid until an administrator revokes it",
    "token": "the token itself stays valid until it expires or is revoked",
    "entra_token": "the token itself stays valid until it expires",
}
_UNUSABLE_HINT = "the token itself stays valid until it expires or is revoked"


def logout(settings: Settings, *, store: SecretStore, profile: str) -> str:
    """Forget the stored token of ``profile`` (no network call); return what to tell the user.

    An entry that cannot be used (damaged, or a kind this version does not know) is removed too.
    """
    try:
        credential = store.get(profile)
    except AuthError:
        hint = _UNUSABLE_HINT
    else:
        hint = _REVOKE_HINTS[credential.kind] if credential is not None else _UNUSABLE_HINT
    notes: list[str] = []
    if store.delete(profile):
        notes += [f"removed the stored credential for profile {profile!r}", hint]
    else:
        notes.append(f"not signed in (no stored credential for profile {profile!r})")
    if settings.env_token is not None:
        notes.append("CONTRACTUM_TOKEN is still set in the environment; unset it to sign out there")
    return "; ".join(notes)
