"""Tracer: bearer token -> issuer registry -> Principal -> route policy -> Authorizer ->
UnitOfWork write -> audit columns -> exactly one outbox row, on real Postgres 18."""

from __future__ import annotations

import uuid
from typing import Annotated
from uuid import UUID

import pytest
import uuid_utils.compat as uuid_compat
from dc_auth.authorizer import Decision, FolderScope, Outcome
from dc_auth.deps import UnitOfWorkDep, auth_lifespan, require
from dc_auth.issuers import AuthSettings
from dc_auth.principal import Principal
from dc_auth.testing import FakeIssuer
from dc_core.app import create_app
from dc_core.db import make_async_engine, make_session_factory, new_metadata
from dc_core.settings import CoreSettings
from dc_core.uow import AuditColumnsMixin, OutboxMixin, UnitOfWork
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Text, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

pytestmark = pytest.mark.integration


class Base(DeclarativeBase):
    metadata = new_metadata()


class Widget(Base, AuditColumnsMixin):
    __tablename__ = "widgets"
    __audit_fields__ = ("id", "name")

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class ProbeOutbox(Base, OutboxMixin):
    __tablename__ = "outbox"


class AdminOnlyAuthorizer:
    """Test PDP: ALLOW only for the principal id 'fake:admin', NOT_FOUND otherwise."""

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        if principal.id == "fake:admin":
            return Decision(outcome=Outcome.ALLOW)
        return Decision(outcome=Outcome.NOT_FOUND)

    async def accessible_folders(self, principal: Principal, permission: str) -> FolderScope:
        return FolderScope(all=principal.id == "fake:admin")


class WidgetIn(BaseModel):
    name: str


def _build(fresh_db_url: str, fake_issuer: FakeIssuer):  # type: ignore[no-untyped-def]
    engine = make_async_engine(fresh_db_url)
    factory = make_session_factory(engine)
    router = APIRouter()

    @router.post("/api/v1/probe/widgets", status_code=201)
    async def create_widget(
        body: WidgetIn,
        principal: Annotated[Principal, require("widget:create")],
        uow: UnitOfWorkDep,
    ) -> dict[str, str]:
        widget = Widget(id=uuid_compat.uuid7(), name=body.name)
        uow.session.add(widget)
        uow.audit(
            "widget.created",
            "widget",
            widget.id,
            before=None,
            after=UnitOfWork.snapshot(widget),
        )
        await uow.commit()
        return {"id": str(widget.id)}

    @router.post("/api/v1/probe/forgetful", status_code=201)
    async def forgetful_widget(
        body: WidgetIn,
        principal: Annotated[Principal, require("widget:create")],
        uow: UnitOfWorkDep,
    ) -> dict[str, str]:
        # A business write that forgot both audit() and commit(): the safety net must trip.
        uow.session.add(Widget(id=uuid_compat.uuid7(), name=body.name))
        return {"id": "never-committed"}

    app = create_app(
        service="probe",
        settings=CoreSettings(service="probe", database_url=fresh_db_url),
        routers=[router],
        session_factory=factory,
        outbox_model=ProbeOutbox,
        lifespan_hooks=[
            auth_lifespan(
                AuthSettings(issuers=[fake_issuer.issuer_config()]),
                authorizer_factory=lambda request: AdminOnlyAuthorizer(),
            )
        ],
    )
    return app, engine


async def _counts(engine):  # type: ignore[no-untyped-def]
    async with engine.connect() as conn:
        widgets = (await conn.execute(select(Widget.__table__))).all()
        outbox = (await conn.execute(select(ProbeOutbox.__table__))).all()
    return widgets, outbox


async def test_authenticated_authorized_write_is_audited(fresh_db_url, fake_issuer, asgi_client):  # type: ignore[no-untyped-def]
    app, engine = _build(fresh_db_url, fake_issuer)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    token = fake_issuer.mint("admin", name="Display Name Is Not The Id")

    async with asgi_client(app) as client:
        response = await client.post(
            "/api/v1/probe/widgets",
            json={"name": "w1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 201, response.text
        widget_id = response.json()["id"]

    # The app disposed its engine on lifespan exit; reconnect to inspect.
    engine = make_async_engine(fresh_db_url)
    widgets, outbox = await _counts(engine)
    await engine.dispose()

    assert len(widgets) == 1
    assert str(widgets[0].id) == widget_id
    assert widgets[0].created_by == "fake:admin"
    assert widgets[0].updated_by == "fake:admin"
    assert len(outbox) == 1
    row = outbox[0]
    assert row.action == "widget.created"
    assert row.aggregate_id == widget_id
    assert row.before is None
    assert row.after == {"id": widget_id, "name": "w1"}
    assert row.actor["type"] == "user"
    assert row.actor["id"] == "fake:admin"  # namespace + sub, never the display name
    assert row.actor["via"] == "jwt"
    assert row.actor["display"] == "Display Name Is Not The Id"
    uuid.UUID(str(row.event_id))


async def test_missing_bearer_token_is_401_and_writes_nothing(
    fresh_db_url,  # type: ignore[no-untyped-def]
    fake_issuer,
    asgi_client,
):
    app, engine = _build(fresh_db_url, fake_issuer)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with asgi_client(app) as client:
        response = await client.post("/api/v1/probe/widgets", json={"name": "w1"})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:dc:problem:unauthenticated"
    assert response.headers["www-authenticate"] == "Bearer"

    engine = make_async_engine(fresh_db_url)
    widgets, outbox = await _counts(engine)
    await engine.dispose()
    assert widgets == []
    assert outbox == []


async def test_authenticated_but_unreadable_target_is_404_and_writes_nothing(
    fresh_db_url,  # type: ignore[no-untyped-def]
    fake_issuer,
    asgi_client,
):
    app, engine = _build(fresh_db_url, fake_issuer)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    token = fake_issuer.mint("someone")

    async with asgi_client(app) as client:
        response = await client.post(
            "/api/v1/probe/widgets",
            json={"name": "w1"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 404
    assert response.json()["type"] == "urn:dc:problem:not-found"

    engine = make_async_engine(fresh_db_url)
    widgets, outbox = await _counts(engine)
    await engine.dispose()
    assert widgets == []
    assert outbox == []


async def test_a_handler_that_forgets_audit_and_commit_never_answers_2xx(
    fresh_db_url,  # type: ignore[no-untyped-def]
    fake_issuer,
    asgi_client,
):
    """WR-02: the unit-of-work exit runs before the response is sent (function scope)."""
    app, engine = _build(fresh_db_url, fake_issuer)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    token = fake_issuer.mint("admin")

    async with asgi_client(app, raise_app_exceptions=False) as client:
        response = await client.post(
            "/api/v1/probe/forgetful",
            json={"name": "w1"},
            headers={"Authorization": f"Bearer {token}"},
        )
    # With the default request scope the 201 is already sent when the tripwire fires.
    assert response.status_code == 500, response.text
    assert response.json()["type"] == "urn:dc:problem:internal-error"

    engine = make_async_engine(fresh_db_url)
    widgets, outbox = await _counts(engine)
    await engine.dispose()
    assert widgets == []
    assert outbox == []
