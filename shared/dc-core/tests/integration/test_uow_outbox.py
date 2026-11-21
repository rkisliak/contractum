"""UnitOfWork, audit columns, tripwire and the outbox helpers on real Postgres 18."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import uuid_utils.compat as uuid_compat
from alembic.migration import MigrationContext
from alembic.operations import Operations
from dc_core.actor import Actor, system_actor
from dc_core.db import make_async_engine, make_session_factory, new_metadata
from dc_core.migrations import audit_columns, outbox_table, run_alembic
from dc_core.uow import (
    AuditColumnsMixin,
    AuditedSession,
    AuditTripwireError,
    OutboxMixin,
    UnitOfWork,
)
from sqlalchemy import Text, create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

pytestmark = pytest.mark.integration

ALICE = Actor(type="user", id="fake:alice", display="Alice Display", via="jwt", credential_id="j1")
BOB = Actor(type="user", id="fake:bob", display="Bob", via="jwt")


class Base(DeclarativeBase):
    metadata = new_metadata()


class Gadget(Base, AuditColumnsMixin):
    __tablename__ = "gadgets"
    __audit_fields__ = ("id", "name")

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str | None] = mapped_column(Text, nullable=True)


class UowOutbox(Base, OutboxMixin):
    __tablename__ = "outbox"


class Tally(Base, AuditColumnsMixin):
    """Audit columns but no audit event required (a derived counter, for example)."""

    __tablename__ = "tallies"
    __audit_event_required__ = False

    id: Mapped[int] = mapped_column(primary_key=True)
    total: Mapped[int] = mapped_column(nullable=False)


@pytest.fixture
async def factory(fresh_db_url: str):  # type: ignore[no-untyped-def]
    engine = make_async_engine(fresh_db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def _uow(factory: Any, actor: Actor = ALICE) -> UnitOfWork:
    return UnitOfWork(
        factory, actor=actor, outbox_model=UowOutbox, service="probe", request_id="req-1"
    )


async def _rows(factory: Any, model: Any) -> list[Any]:
    async with factory() as session:
        return list((await session.execute(select(model))).scalars())


async def test_audit_columns_adjacency_insert_equal_then_update_later(factory) -> None:  # type: ignore[no-untyped-def]
    gid = uuid_compat.uuid7()
    async with _uow(factory) as uow:
        uow.session.add(Gadget(id=gid, name="g"))
        uow.audit("gadget.created", "gadget", gid, before=None, after={"id": str(gid)})
        await uow.commit()
    (row,) = await _rows(factory, Gadget)
    assert row.created_at == row.updated_at
    assert row.created_by == row.updated_by == "fake:alice"

    async with _uow(factory, BOB) as uow:
        gadget = await uow.session.get(Gadget, gid)
        assert gadget is not None
        gadget.name = "g2"
        uow.audit("gadget.updated", "gadget", gid, before={"name": "g"}, after={"name": "g2"})
        await uow.commit()
    (row,) = await _rows(factory, Gadget)
    assert row.updated_at > row.created_at
    assert row.created_by == "fake:alice"
    assert row.updated_by == "fake:bob"


async def test_audit_columns_empty_flush_without_actor_raises_and_writes_nothing(factory) -> None:  # type: ignore[no-untyped-def]
    session = factory()
    assert isinstance(session.sync_session, AuditedSession)
    session.add(Gadget(id=uuid_compat.uuid7(), name="orphan"))
    with pytest.raises(RuntimeError, match="actor"):
        await session.flush()
    await session.rollback()
    await session.close()
    assert await _rows(factory, Gadget) == []


async def test_audit_columns_ordering_client_values_are_overwritten(factory) -> None:  # type: ignore[no-untyped-def]
    gid = uuid_compat.uuid7()
    async with _uow(factory) as uow:
        uow.session.add(
            Gadget(
                id=gid,
                name="g",
                created_by="mallory",
                updated_by="mallory",
                created_at=text("'2001-01-01'::timestamptz"),  # type: ignore[arg-type]
                updated_at=text("'2001-01-01'::timestamptz"),  # type: ignore[arg-type]
            )
        )
        uow.audit("gadget.created", "gadget", gid, before=None, after=None)
        await uow.commit()
    (row,) = await _rows(factory, Gadget)
    assert row.created_by == row.updated_by == "fake:alice"
    assert row.created_at.year >= 2026
    assert row.created_at == row.updated_at

    async with _uow(factory, BOB) as uow:
        gadget = await uow.session.get(Gadget, gid)
        assert gadget is not None
        gadget.name = "renamed"
        gadget.created_by = "mallory"
        uow.audit("gadget.updated", "gadget", gid, before=None, after=None)
        await uow.commit()
    (row,) = await _rows(factory, Gadget)
    assert row.created_by == "fake:alice"
    assert row.updated_by == "fake:bob"


async def test_audit_columns_adjacency_readable_after_commit_without_lazy_load(factory) -> None:  # type: ignore[no-untyped-def]
    gid = uuid_compat.uuid7()
    async with _uow(factory) as uow:
        gadget = Gadget(id=gid, name="g")
        uow.session.add(gadget)
        uow.audit("gadget.created", "gadget", gid, before=None, after=None)
        await uow.commit()
        # Server-side now() values are fetched with RETURNING: no MissingGreenlet in async.
        assert gadget.created_at == gadget.updated_at
        assert gadget.created_by == "fake:alice"
    async with _uow(factory, BOB) as uow:
        loaded = await uow.session.get(Gadget, gid)
        assert loaded is not None
        loaded.name = "g2"
        uow.audit("gadget.updated", "gadget", gid, before=None, after=None)
        await uow.commit()
        assert loaded.updated_at > loaded.created_at
        assert loaded.updated_by == "fake:bob"


async def test_outbox_empty_rolled_back_unit_leaves_no_row(factory) -> None:  # type: ignore[no-untyped-def]
    gid = uuid_compat.uuid7()
    with pytest.raises(ValueError, match="boom"):
        async with _uow(factory) as uow:
            uow.session.add(Gadget(id=gid, name="g"))
            uow.audit("gadget.created", "gadget", gid, before=None, after=None)
            await uow.session.flush()
            raise ValueError("boom")
    assert await _rows(factory, Gadget) == []
    assert await _rows(factory, UowOutbox) == []


async def test_outbox_empty_unit_without_audited_change_writes_none(factory) -> None:  # type: ignore[no-untyped-def]
    async with _uow(factory) as uow:
        await uow.session.execute(select(Gadget))
        await uow.commit()
    assert await _rows(factory, UowOutbox) == []


async def test_outbox_empty_create_event_stores_before_null_and_actor(factory) -> None:  # type: ignore[no-untyped-def]
    gid = uuid_compat.uuid7()
    async with _uow(factory) as uow:
        gadget = Gadget(id=gid, name="g", secret_hash="$argon2id$v=19$leaked")
        uow.session.add(gadget)
        uow.audit(
            "gadget.created",
            "gadget",
            gid,
            before=None,
            after=UnitOfWork.snapshot(gadget),
            scope_path=[uuid.UUID(int=1), uuid.UUID(int=2)],
            aggregate_version=1,
        )
        await uow.commit()
    async with factory() as session:
        null_before = (
            await session.execute(text("SELECT count(*) FROM outbox WHERE before IS NULL"))
        ).scalar_one()
    assert null_before == 1
    (row,) = await _rows(factory, UowOutbox)
    assert row.actor["id"] == "fake:alice"
    assert row.actor["type"] == "user"
    assert row.actor["credential_id"] == "j1"
    assert row.request_id == "req-1"
    assert row.aggregate_version == 1
    assert row.scope_path == [uuid.UUID(int=1), uuid.UUID(int=2)]
    assert row.schema_version == 1
    assert row.attempts == 0
    assert row.published_at is None
    assert "argon2" not in str(row.after)


def test_snapshot_omits_non_allowlisted_columns() -> None:
    gadget = Gadget(id=uuid_compat.uuid7(), name="g", secret_hash="$argon2id$secret")
    snap = UnitOfWork.snapshot(gadget)
    assert set(snap) == {"id", "name"}
    assert "secret_hash" not in snap
    assert isinstance(snap["id"], str)


async def test_forgotten_audit_trips_the_tripwire_and_nothing_commits(factory) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(AuditTripwireError):
        async with _uow(factory) as uow:
            uow.session.add(Gadget(id=uuid_compat.uuid7(), name="g"))
            await uow.commit()
    assert await _rows(factory, Gadget) == []
    assert await _rows(factory, UowOutbox) == []


async def test_direct_session_commit_without_outbox_trips_the_tripwire(factory) -> None:  # type: ignore[no-untyped-def]
    session = factory()
    session.sync_session.info["actor"] = ALICE
    session.add(Gadget(id=uuid_compat.uuid7(), name="g"))
    with pytest.raises(AuditTripwireError):
        await session.commit()
    await session.rollback()
    await session.close()
    assert await _rows(factory, Gadget) == []


async def test_audit_may_only_be_called_once_per_unit(factory) -> None:  # type: ignore[no-untyped-def]
    async with _uow(factory) as uow:
        uow.audit("a", "t", "1", before=None, after=None)
        with pytest.raises(RuntimeError, match="once"):
            uow.audit("b", "t", "1", before=None, after=None)


async def test_system_actor_shape() -> None:
    actor = system_actor("relay")
    assert (actor.type, actor.id, actor.via) == ("system", "system:relay", "system")


async def test_outbox_table_helper_matches_mixin_and_creates_partial_index(
    fresh_db_url: str,
) -> None:
    engine = create_engine(fresh_db_url)
    with engine.begin() as conn:
        ops = Operations(MigrationContext.configure(conn))
        outbox_table(ops)
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("outbox")}
    assert columns == {c.name for c in UowOutbox.__table__.columns}
    index = {i["name"]: i for i in inspector.get_indexes("outbox")}["ix_outbox_unpublished"]
    assert index["column_names"] == ["id"]
    assert "published_at IS NULL" in str(index["dialect_options"]["postgresql_where"])
    uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("outbox")}
    assert ("event_id",) in uniques
    engine.dispose()


def test_audit_columns_helper_returns_the_four_columns() -> None:
    cols = audit_columns()
    assert [c.name for c in cols] == ["created_by", "created_at", "updated_by", "updated_at"]
    assert all(not c.nullable for c in cols)


MIGRATION_ENV = """
from alembic import context
from sqlalchemy import create_engine

config = context.config
url = config.get_main_option("sqlalchemy.url")
version_table = config.attributes["version_table"]
with create_engine(url).connect() as connection:
    context.configure(connection=connection, target_metadata=None, version_table=version_table)
    with context.begin_transaction():
        context.run_migrations()
"""

MIGRATION_REVISION = '''
"""baseline"""
import sqlalchemy as sa
from alembic import op
from dc_core.migrations import audit_columns, outbox_table

revision = "0001"
down_revision = None


def upgrade():
    outbox_table(op)
    op.create_table("things", sa.Column("id", sa.Integer, primary_key=True), *audit_columns())


def downgrade():
    op.drop_table("things")
    op.drop_table("outbox")
'''


def test_run_alembic_upgrade_current_downgrade_with_own_version_table(
    fresh_db_url: str, tmp_path: Path
) -> None:
    script_dir = tmp_path / "migrations"
    (script_dir / "versions").mkdir(parents=True)
    (script_dir / "env.py").write_text(MIGRATION_ENV)
    (script_dir / "script.py.mako").write_text("")
    (script_dir / "versions" / "0001_baseline.py").write_text(MIGRATION_REVISION)
    kwargs: dict[str, Any] = {
        "script_location": str(script_dir),
        "version_table": "alembic_version_probe",
        "url": fresh_db_url,
    }
    run_alembic(command="upgrade", **kwargs)
    engine = create_engine(fresh_db_url)
    tables = set(inspect(engine).get_table_names())
    assert {"outbox", "things", "alembic_version_probe"} <= tables
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version_probe")).scalar_one()
            == "0001"
        )
    run_alembic(command="current", **kwargs)
    run_alembic(command="downgrade", revision="base", **kwargs)
    assert "things" not in set(inspect(engine).get_table_names())
    engine.dispose()


async def test_tripwire_flags_do_not_survive_a_commit(factory) -> None:  # type: ignore[no-untyped-def]
    """WR-07: the outbox row of the first commit must not excuse an unaudited second write."""
    async with _uow(factory) as uow:
        first = Gadget(id=uuid_compat.uuid7(), name="first")
        uow.session.add(first)
        uow.audit("gadget.created", "gadget", first.id, before=None, after=None)
        await uow.commit()
        assert "outbox_written" not in uow.session.sync_session.info
        assert "audited_write" not in uow.session.sync_session.info

        uow.session.add(Gadget(id=uuid_compat.uuid7(), name="second"))  # no audit()
        with pytest.raises(AuditTripwireError):
            await uow.commit()
    assert [g.name for g in await _rows(factory, Gadget)] == ["first"]
    assert len(await _rows(factory, UowOutbox)) == 1


async def test_a_stale_audited_flag_does_not_leak_into_the_next_transaction(factory) -> None:  # type: ignore[no-untyped-def]
    session = factory()
    session.sync_session.info["actor"] = ALICE
    session.add(Gadget(id=uuid_compat.uuid7(), name="g"))
    with pytest.raises(AuditTripwireError):
        await session.commit()
    await session.rollback()
    assert "audited_write" not in session.sync_session.info
    await session.close()


async def test_a_row_without_a_required_event_is_written_updated_and_deleted_without_audit(
    factory,  # type: ignore[no-untyped-def]
) -> None:
    async with _uow(factory) as uow:
        uow.session.add(Tally(id=1, total=1))
        await uow.commit()
    async with _uow(factory, BOB) as uow:
        tally = await uow.session.get(Tally, 1)
        assert tally is not None
        tally.total = 2
        await uow.commit()
    (row,) = await _rows(factory, Tally)
    assert (row.total, row.created_by, row.updated_by) == (2, "fake:alice", "fake:bob")
    async with _uow(factory) as uow:
        await uow.session.delete(await uow.session.get(Tally, 1))
        await uow.commit()
    assert await _rows(factory, Tally) == []
    assert await _rows(factory, UowOutbox) == []


async def test_deleting_an_audited_row_needs_an_event(factory) -> None:  # type: ignore[no-untyped-def]
    gid = uuid_compat.uuid7()
    async with _uow(factory) as uow:
        uow.session.add(Gadget(id=gid, name="g"))
        uow.audit("gadget.created", "gadget", gid, before=None, after=None)
        await uow.commit()
    with pytest.raises(AuditTripwireError):
        async with _uow(factory) as uow:
            await uow.session.delete(await uow.session.get(Gadget, gid))
            await uow.commit()
    assert len(await _rows(factory, Gadget)) == 1
    async with _uow(factory) as uow:
        await uow.session.delete(await uow.session.get(Gadget, gid))
        uow.audit("gadget.deleted", "gadget", gid, before={"name": "g"}, after=None)
        await uow.commit()
    assert await _rows(factory, Gadget) == []
    assert [r.action for r in await _rows(factory, UowOutbox)] == [
        "gadget.created",
        "gadget.deleted",
    ]


async def test_assigning_an_unchanged_value_is_not_an_audited_write(factory) -> None:  # type: ignore[no-untyped-def]
    gid = uuid_compat.uuid7()
    async with _uow(factory) as uow:
        uow.session.add(Gadget(id=gid, name="same"))
        uow.audit("gadget.created", "gadget", gid, before=None, after=None)
        await uow.commit()
    async with _uow(factory, BOB) as uow:
        gadget = await uow.session.get(Gadget, gid)
        assert gadget is not None
        gadget.name = "same"
        await uow.commit()  # no audit(), no tripwire: nothing changed
    (row,) = await _rows(factory, Gadget)
    assert row.updated_by == "fake:alice"


async def test_deleting_a_non_audited_row_is_not_an_audited_write(factory) -> None:  # type: ignore[no-untyped-def]
    async with _uow(factory) as uow:
        uow.audit("probe.ping", "probe", "1", before=None, after=None)
        await uow.commit()
    (outbox_row,) = await _rows(factory, UowOutbox)
    async with _uow(factory) as uow:
        await uow.session.delete(await uow.session.get(UowOutbox, outbox_row.id))
        await uow.commit()
    assert await _rows(factory, UowOutbox) == []


async def test_commit_if_pending_commits_only_a_touched_unit(factory) -> None:  # type: ignore[no-untyped-def]
    async with _uow(factory) as uow:
        assert uow.has_pending() is False
        await uow.commit_if_pending()
        assert uow.committed is False
        gid = uuid_compat.uuid7()
        uow.session.add(Gadget(id=gid, name="pending"))
        uow.audit("gadget.created", "gadget", gid, before=None, after=None)
        assert uow.has_pending() is True
        await uow.commit_if_pending()
        assert uow.committed is True
        assert uow.has_pending() is False
    assert [g.name for g in await _rows(factory, Gadget)] == ["pending"]


CHECK_ENV = """
from alembic import context
from sqlalchemy import MetaData, create_engine

config = context.config
url = config.get_main_option("sqlalchemy.url")
with create_engine(url).connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=MetaData(),
        version_table=config.attributes["version_table"],
    )
    with context.begin_transaction():
        context.run_migrations()
"""


def test_run_alembic_check_passes_in_sync_and_fails_on_drift(
    fresh_db_url: str, tmp_path: Path
) -> None:
    from alembic.util.exc import AutogenerateDiffsDetected

    script_dir = tmp_path / "migrations"
    (script_dir / "versions").mkdir(parents=True)
    (script_dir / "env.py").write_text(CHECK_ENV)
    (script_dir / "script.py.mako").write_text("")
    kwargs: dict[str, Any] = {
        "script_location": str(script_dir),
        "version_table": "alembic_version_check",
        "url": fresh_db_url,
    }
    run_alembic(command="check", **kwargs)  # no revisions, no tables: nothing to generate
    engine = create_engine(fresh_db_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE stray (id integer PRIMARY KEY)"))
    engine.dispose()
    with pytest.raises(AutogenerateDiffsDetected):
        run_alembic(command="check", **kwargs)
