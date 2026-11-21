"""``PrincipalGate`` refusals that need no database: a credential principal without a credential
id, and an ``sa:`` or Entra id on a path no such token takes, all fail closed with 401."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from dc_auth.principal import Principal
from dc_core.problems import ProblemError
from dc_identity.directory import UnconfiguredDirectory
from dc_identity.gate import PrincipalGate, ResolvedPrincipal, resolved_for_request

pytestmark = pytest.mark.unit

TENANT = "4f1c2d3e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


def _no_session() -> Any:
    raise AssertionError("this refusal must not open a database session")


def _gate() -> PrincipalGate:
    return PrincipalGate(_no_session, UnconfiguredDirectory())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("principal_id", "fields"),
    [
        (f"local:{uuid.uuid4()}", {"type": "pat", "via": "pat"}),
        (f"sa:{uuid.uuid4()}", {"type": "service_account", "via": "api_key"}),
        (f"sa:{uuid.uuid4()}", {"type": "service_account"}),
        (f"entra:{TENANT}:{uuid.uuid4()}", {"type": "service_account"}),
        (f"entra:{TENANT}:{uuid.uuid4()}", {"type": "system"}),
    ],
)
async def test_a_principal_no_identity_path_can_vouch_for_is_401(
    make_principal: Callable[..., Principal], principal_id: str, fields: dict[str, Any]
) -> None:
    with pytest.raises(ProblemError) as caught:
        await _gate().resolve(make_principal(principal_id, **fields))
    assert (caught.value.status, caught.value.slug) == (401, "unauthenticated")


async def test_a_request_without_a_gate_answer_asks_the_gate(
    make_principal: Callable[..., Principal],
) -> None:
    principal = make_principal("fake:generic-oidc-user")
    request: Any = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(principal_gate=_gate()))
    )
    resolved = await resolved_for_request(request, principal)
    assert resolved == ResolvedPrincipal("user", "fake:generic-oidc-user")
