"""PAT guards that answer before any database work: expiry, owner kind, foreign principals.

Each case below hands the function a session or unit of work that fails the test when touched,
so the assertion also proves the refusal happens before a query is sent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dc_auth.principal import Principal
from dc_core.problems import ProblemError
from dc_identity.credentials import (
    LocalOpaqueResolver,
    check_expiry,
    count_live_pats,
    create_pat,
    list_all_pats,
    list_own_pats,
    revoke_own_pat,
)
from dc_identity.settings import IdentitySettings

pytestmark = pytest.mark.unit

TENANT = "4f1c2d3e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


class _Untouchable:
    """A session (or unit of work) that fails the test on any use."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the database must not be used (accessed {name!r})")


class _UnitOfWork:
    session = _Untouchable()


def _future(days: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


@pytest.mark.parametrize(
    "expires_at",
    [
        datetime(2099, 1, 1),  # naive: no timezone
        datetime.now(UTC) - timedelta(seconds=1),
        datetime.now(UTC) + timedelta(days=366),
    ],
)
def test_an_expiry_outside_the_window_is_422(expires_at: datetime) -> None:
    with pytest.raises(ProblemError) as caught:
        check_expiry(expires_at, IdentitySettings())
    assert (caught.value.status, caught.value.slug) == (422, "expiry-out-of-range")


def test_the_stored_ceiling_overrides_the_environment_value() -> None:
    check_expiry(_future(300), IdentitySettings())
    with pytest.raises(ProblemError):
        check_expiry(_future(300), IdentitySettings(), max_days=30)


@pytest.mark.parametrize(
    ("principal_id", "ptype"),
    [
        (f"sa:{uuid.uuid4()}", "service_account"),
        (f"entra:{TENANT}:{uuid.uuid4()}", "service_principal"),
        (f"sa:{uuid.uuid4()}", "user"),
        ("fake:someone", "user"),
    ],
)
async def test_only_a_local_or_entra_human_may_create_a_pat(
    make_principal: Callable[..., Principal], principal_id: str, ptype: str
) -> None:
    actor = make_principal(principal_id, type=ptype)
    with pytest.raises(ProblemError) as caught:
        await create_pat(
            _UnitOfWork(),  # type: ignore[arg-type]
            actor,
            name="pat",
            expires_at=_future(),
            settings=IdentitySettings(),
        )
    assert (caught.value.status, caught.value.slug) == (403, "forbidden")


@pytest.mark.parametrize(
    ("principal_id", "ptype"),
    [
        (f"sa:{uuid.uuid4()}", "service_account"),
        (f"entra:{TENANT}:{uuid.uuid4()}", "service_principal"),
        ("fake:someone", "user"),
        (f"sa:{uuid.uuid4()}", "pat"),
    ],
)
async def test_a_principal_that_owns_no_pats_sees_none_and_revokes_none(
    make_principal: Callable[..., Principal], principal_id: str, ptype: str
) -> None:
    actor = make_principal(principal_id, type=ptype)
    session: Any = _Untouchable()
    assert await count_live_pats(session, actor) == 0
    assert await list_own_pats(session, actor, cursor=None, limit=10) == ([], None)
    with pytest.raises(ProblemError) as caught:
        await revoke_own_pat(_UnitOfWork(), actor, uuid.uuid4())  # type: ignore[arg-type]
    assert (caught.value.status, caught.value.slug) == (404, "not-found")


@pytest.mark.parametrize("owner", ["fake:someone", f"sa:{uuid.uuid4()}", "not an id"])
async def test_the_admin_listing_filtered_on_a_non_owner_id_is_an_empty_page(owner: str) -> None:
    session: Any = _Untouchable()
    assert await list_all_pats(session, owner=owner, cursor=None, limit=10) == ([], None)


@pytest.mark.parametrize("token", ["", "Bearer x", "dcp_", "dcp_short_secret", "pat_abc_def"])
async def test_a_malformed_opaque_token_is_rejected_without_a_lookup(token: str) -> None:
    resolver = LocalOpaqueResolver(_Untouchable())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="malformed opaque token"):
        await resolver.resolve(token)
