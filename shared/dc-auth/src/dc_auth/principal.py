"""Principal: the verified caller (D-03, one-way). No roles or groups live here (D-05)."""

from typing import Literal

from dc_core.actor import Actor
from pydantic import BaseModel

PrincipalType = Literal["user", "service_principal", "pat", "service_account", "system"]
PrincipalVia = Literal["jwt", "system", "pat", "api_key"]
PRINCIPAL_TYPES: frozenset[str] = frozenset(
    {"user", "service_principal", "pat", "service_account", "system"}
)


class Principal(BaseModel, frozen=True):
    id: str  # namespaced, stable: "<issuer namespace>:<sub>"
    type: PrincipalType
    display: str  # snapshot for audit; never used for authorization
    issuer: str
    subject: str
    via: PrincipalVia
    scopes: frozenset[str] = frozenset()
    credential_id: str | None = None  # jti, Entra uti, or the dcp_ public id (PAT, key)
    on_behalf_of: str | None = None  # reserved
    # Display only (Entra preferred_username): never a key and never an authority input (D-81).
    username_hint: str | None = None
    # Verified iat of a JWT principal, whole seconds. Used only as a session cutoff (a token issued
    # at or before a reactivation is refused) and never as authority; None for PAT and key
    # principals (D-75).
    issued_at: int | None = None

    @property
    def restricted(self) -> bool:
        """True for a bootstrap session whose only scope is ``password:rotate``."""
        return "api" not in self.scopes


def principal_to_actor(p: Principal) -> Actor:
    return Actor(
        type=p.type,
        id=p.id,
        display=p.display,
        via=p.via,
        credential_id=p.credential_id,
        on_behalf_of=p.on_behalf_of,
    )
