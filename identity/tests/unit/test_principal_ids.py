"""The single parser of principal ids (``local:``, ``entra:`` and ``sa:``), D-58."""

from __future__ import annotations

import uuid

import pytest
from dc_auth.testing import ENTRA_TENANT_ID
from dc_identity import principal_ids as pids
from dc_identity.principal_ids import (
    ENTRA_PREFIX,
    LOCAL_PREFIX,
    SERVICE_ACCOUNT_PREFIX,
    entra_principal_id,
    grantee_type,
    local_principal_id,
    parse_principal_id,
    service_account_principal_id,
)

pytestmark = pytest.mark.unit

OID = "9c8b7a6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


def test_prefixes_are_the_wire_forms() -> None:
    assert LOCAL_PREFIX == "local:"
    assert ENTRA_PREFIX == "entra:"
    assert SERVICE_ACCOUNT_PREFIX == "sa:"
    assert pids.GUID_PATTERN.fullmatch(OID)


def test_canonical_local_id_parses_to_a_local_user() -> None:
    user_id = uuid.uuid4()
    ref = parse_principal_id(f"local:{user_id}")
    assert ref is not None
    assert ref.kind == "local_user"
    assert ref.local_id == user_id
    assert ref.canonical == f"local:{user_id}"
    assert ref.tenant_id is None


def test_canonical_entra_id_parses_to_tenant_and_object() -> None:
    ref = parse_principal_id(f"entra:{ENTRA_TENANT_ID}:{OID}")
    assert ref is not None
    assert ref.kind == "entra"
    assert ref.tenant_id == ENTRA_TENANT_ID
    assert ref.object_id == OID
    assert ref.canonical == f"entra:{ENTRA_TENANT_ID}:{OID}"


def test_canonical_service_account_id_parses() -> None:
    sa_id = uuid.uuid4()
    ref = parse_principal_id(f"sa:{sa_id}")
    assert ref is not None
    assert ref.kind == "service_account"
    assert ref.service_account_id == sa_id
    assert ref.canonical == f"sa:{sa_id}"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "local:",
        "local:not-a-uuid",
        f"LOCAL:{uuid.uuid4()}",
        f"local:{str(uuid.uuid4()).upper()}",
        f"local:{uuid.uuid4()}:extra",
        f"entra:{ENTRA_TENANT_ID}",
        f"entra:{ENTRA_TENANT_ID}:{OID}:extra",
        f"entra:{ENTRA_TENANT_ID.upper()}:{OID}",
        f"entra:{ENTRA_TENANT_ID}:{OID.upper()}",
        f"entra::{OID}",
        "sa:",
        "sa:xyz",
        f"sa:{str(uuid.uuid4()).upper()}",
        f"fake:{uuid.uuid4()}",
        f"mock:{OID}",
        f" local:{uuid.uuid4()}",
        f"local:{uuid.uuid4()}\n",
    ],
)
def test_every_other_spelling_is_not_a_principal_id(value: str) -> None:
    assert parse_principal_id(value) is None


def test_builders_produce_the_canonical_forms() -> None:
    user_id = uuid.uuid4()
    sa_id = uuid.uuid4()
    assert local_principal_id(user_id) == f"local:{user_id}"
    assert entra_principal_id(ENTRA_TENANT_ID, OID) == f"entra:{ENTRA_TENANT_ID}:{OID}"
    assert service_account_principal_id(sa_id) == f"sa:{sa_id}"


def test_grantee_type_maps_pat_to_user_and_leaves_the_rest() -> None:
    class _P:
        def __init__(self, type_: str) -> None:
            self.type = type_

    assert grantee_type(_P("pat")) == "user"  # type: ignore[arg-type]
    for other in ("user", "service_principal", "service_account", "system"):
        assert grantee_type(_P(other)) == other  # type: ignore[arg-type]
