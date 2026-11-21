"""The single parser of principal ids: ``local:``, ``entra:`` and ``sa:`` (D-58).

A principal id is the one string that keys grants, group memberships and registrations. Only the
canonical lowercase spelling is a principal id; every other spelling parses to ``None`` so two
spellings of the same identity can never hold different grants. Generic OIDC issuers (``fake:``,
``mock:``) produce ids that are none of these forms; callers treat ``None`` as "not one of ours".
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from dc_auth.issuers import ENTRA_PREFIX as _ENTRA_NAMESPACE

if TYPE_CHECKING:
    from dc_auth.principal import Principal

GUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LOCAL_PREFIX = "local:"
ENTRA_PREFIX = f"{_ENTRA_NAMESPACE}:"
SERVICE_ACCOUNT_PREFIX = "sa:"

PrincipalKind = Literal["local_user", "entra", "service_account"]


@dataclass(frozen=True)
class PrincipalRef:
    """A parsed principal id; only the fields of its ``kind`` are set."""

    kind: PrincipalKind
    canonical: str
    local_id: uuid.UUID | None = None
    tenant_id: str | None = None
    object_id: str | None = None
    service_account_id: uuid.UUID | None = None


def _canonical_uuid(value: str) -> uuid.UUID | None:
    if GUID_PATTERN.fullmatch(value) is None:
        return None
    return uuid.UUID(value)


def parse_principal_id(value: str) -> PrincipalRef | None:
    """Parse a canonical principal id; ``None`` for anything else."""
    if value.startswith(LOCAL_PREFIX):
        local_id = _canonical_uuid(value[len(LOCAL_PREFIX) :])
        if local_id is None:
            return None
        return PrincipalRef("local_user", value, local_id=local_id)
    if value.startswith(SERVICE_ACCOUNT_PREFIX):
        sa_id = _canonical_uuid(value[len(SERVICE_ACCOUNT_PREFIX) :])
        if sa_id is None:
            return None
        return PrincipalRef("service_account", value, service_account_id=sa_id)
    if value.startswith(ENTRA_PREFIX):
        parts = value[len(ENTRA_PREFIX) :].split(":")
        if len(parts) != 2:
            return None
        tenant_id, object_id = parts
        if GUID_PATTERN.fullmatch(tenant_id) is None or GUID_PATTERN.fullmatch(object_id) is None:
            return None
        return PrincipalRef("entra", value, tenant_id=tenant_id, object_id=object_id)
    return None


def local_principal_id(user_id: uuid.UUID) -> str:
    return f"{LOCAL_PREFIX}{user_id}"


def entra_principal_id(tenant_id: str, object_id: str) -> str:
    return f"{ENTRA_PREFIX}{tenant_id}:{object_id}"


def service_account_principal_id(service_account_id: uuid.UUID) -> str:
    return f"{SERVICE_ACCOUNT_PREFIX}{service_account_id}"


def grantee_type(principal: Principal) -> str:
    """The ``principal_type`` a principal's own grants carry: a PAT acts as its owning user."""
    return "user" if principal.type == "pat" else str(principal.type)
