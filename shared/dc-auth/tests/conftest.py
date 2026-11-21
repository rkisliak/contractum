"""dc-auth test helpers: a fixed-outcome authorizer and a probe app wired with the fake issuer."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from uuid import UUID

import pytest
from dc_auth.authorizer import Decision, FolderScope, Outcome
from dc_auth.deps import auth_lifespan
from dc_auth.issuers import AuthSettings
from dc_auth.principal import Principal
from dc_auth.testing import FakeIssuer
from dc_core.app import create_app
from dc_core.settings import CoreSettings
from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ProbeAppBuilder = Callable[..., FastAPI]


class StaticAuthorizer:
    """Answers every check with one outcome; sees everything only when that outcome is ALLOW."""

    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        return Decision(outcome=self.outcome)

    async def accessible_folders(self, principal: Principal, permission: str) -> FolderScope:
        return FolderScope(all=self.outcome is Outcome.ALLOW)


@pytest.fixture
def probe_app(fake_issuer: FakeIssuer) -> ProbeAppBuilder:
    """Factory for a ``probe`` service app that trusts the fake issuer and a fixed PDP outcome."""

    def _build(
        *,
        routers: Sequence[APIRouter] = (),
        internal_routers: Sequence[APIRouter] = (),
        outcome: Outcome = Outcome.ALLOW,
        database_url: str = "postgresql+psycopg://x/y",
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> FastAPI:
        authorizer = StaticAuthorizer(outcome)
        return create_app(
            service="probe",
            settings=CoreSettings(service="probe", database_url=database_url),
            routers=routers,
            internal_routers=internal_routers,
            session_factory=session_factory,
            lifespan_hooks=[
                auth_lifespan(
                    AuthSettings(issuers=[fake_issuer.issuer_config()]),
                    authorizer_factory=lambda request: authorizer,
                )
            ],
        )

    return _build
