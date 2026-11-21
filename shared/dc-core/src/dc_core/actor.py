"""Actor: the audit projection of whoever caused a write (D-03, one-way).

An Actor is built from the verified Principal only. It is never taken from a request body,
a header or a display name.
"""

from typing import Literal

from pydantic import BaseModel

ActorType = Literal["user", "service_principal", "pat", "service_account", "system"]


class Actor(BaseModel, frozen=True):
    type: ActorType
    id: str
    display: str
    via: str
    credential_id: str | None = None
    on_behalf_of: str | None = None


def system_actor(component: str) -> Actor:
    """Actor for work no user initiated (bootstrap seeding, outbox relay, audit consumer)."""
    return Actor(type="system", id=f"system:{component}", display=component, via="system")
