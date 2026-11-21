"""D-186: audit's SQL visibility over identity's real scope equals the access oracle (AUD-05).

Hypothesis draws a tree (ROOT, 1-3 organizations x 1-3 projects x 1-3 teams), random block flags
on organization and team folders, up to ten grants of every role at every level to three users
(biased towards ROOT and the ``audit:query`` holders) and up to fourteen events whose
``scope_path`` is empty, ``[root]`` or a folder's root-first chain.

Two migrated databases are used, as in production: identity's (seeded by SQL, so level binding is
deliberately bypassed) and audit's (events stored through the real ``handle_payload``). For each
user, ``LocalAuthorizer.accessible_folders(principal, "audit:query")`` (identity's
``accessible_scope``) is handed to ``dc_audit.query.visible`` and the event ids the statement
returns are compared with a reference built from ``dc_auth.oracle.folders_with``:

* an event with an empty or ``[root]`` scope_path is visible iff ROOT is in that set;
* any other event iff its own folder (the last element) is in that set.

The tree, block and grant strategies and the SQL seeding are copies of
``identity/tests/integration/test_authz_oracle.py`` (that module is not imported).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from dc_audit.ingest import handle_payload
from dc_audit.migrate import SCRIPT_LOCATION as AUDIT_SCRIPTS
from dc_audit.migrate import VERSION_TABLE as AUDIT_VERSION_TABLE
from dc_audit.query import AuditFilters, build_statement, visible
from dc_auth.oracle import Grant, Tree, TreeFolder, folders_with
from dc_auth.principal import Principal
from dc_auth.roles import Role, roles_with
from dc_core.db import make_async_engine, make_session_factory
from dc_core.events import encode_envelope, subject_for
from dc_core.migrations import run_alembic
from dc_identity.authz import LocalAuthorizer
from dc_identity.migrate import SCRIPT_LOCATION as IDENTITY_SCRIPTS
from dc_identity.migrate import VERSION_TABLE as IDENTITY_VERSION_TABLE
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

PERMISSION = "audit:query"
_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
# Same budget as identity's oracle suite: 40 examples locally, 150 under HYPOTHESIS_PROFILE=ci.
_EXAMPLES = 150 if os.environ.get("HYPOTHESIS_PROFILE") == "ci" else 40

USERS = ("fake:u1", "fake:u2", "fake:u3")
ROLES = tuple(str(role) for role in Role)
QUERY_ROLES = tuple(sorted(roles_with(PERMISSION)))
BASE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
EMPTY = -1  # an event whose scope_path is empty (index 0 is ROOT, so [root])


@dataclass(frozen=True)
class GrantSpec:
    folder: int  # index into Scenario.kinds
    user: str
    role: str


@dataclass(frozen=True)
class Scenario:
    shape: tuple[tuple[int, ...], ...]  # per org: teams per project
    blocks: frozenset[int]
    grants: tuple[GrantSpec, ...]
    events: tuple[int, ...]  # folder index, or EMPTY
    kinds: tuple[str, ...] = field(init=False)
    paths: tuple[str, ...] = field(init=False)
    parents: tuple[int | None, ...] = field(init=False)

    def __post_init__(self) -> None:
        kinds, paths, parents = ["root"], ["root"], [None]
        for o, projects in enumerate(self.shape):
            org = len(kinds)
            kinds.append("organization")
            paths.append(f"root.o{o}")
            parents.append(0)
            for p, teams in enumerate(projects):
                project = len(kinds)
                kinds.append("project")
                paths.append(f"root.o{o}.p{p}")
                parents.append(org)
                for t in range(teams):
                    kinds.append("team")
                    paths.append(f"root.o{o}.p{p}.t{t}")
                    parents.append(project)
        object.__setattr__(self, "kinds", tuple(kinds))
        object.__setattr__(self, "paths", tuple(paths))
        object.__setattr__(self, "parents", tuple(parents))


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    shape = tuple(
        tuple(draw(st.integers(1, 3)) for _ in range(draw(st.integers(1, 3))))
        for _ in range(draw(st.integers(1, 3)))
    )
    probe = Scenario(shape, frozenset(), (), ())
    blockable = [i for i, kind in enumerate(probe.kinds) if kind in ("organization", "team")]
    blocks = draw(st.sets(st.sampled_from(blockable))) if blockable else set()
    folders = st.integers(0, len(probe.kinds) - 1)
    # Every role at every level, biased towards ROOT and the audit:query holders so the
    # interesting cases (a non-admin ROOT grant cut by a block, add-backs) are drawn often.
    roles = st.one_of(st.sampled_from(ROLES), st.sampled_from(QUERY_ROLES))
    grant_specs = draw(
        st.sets(
            st.builds(
                GrantSpec,
                folder=st.one_of(st.just(0), folders),
                user=st.sampled_from(USERS),
                role=roles,
            ),
            max_size=10,
        )
    )
    events = draw(st.lists(st.one_of(st.just(EMPTY), folders), min_size=1, max_size=14))
    ordered = tuple(sorted(grant_specs, key=lambda g: (g.folder, g.user, g.role)))
    return Scenario(shape, frozenset(blocks), ordered, tuple(events))


@dataclass
class Loaded:
    scenario: Scenario
    folder_ids: tuple[uuid.UUID, ...]
    tree: Tree
    blocks: frozenset[uuid.UUID]
    grants: tuple[Grant, ...]
    events: tuple[tuple[str, tuple[uuid.UUID, ...]], ...]  # (event_id, scope_path)


@dataclass(frozen=True)
class Databases:
    identity: str
    audit: str


def _sync_url(db_url: str) -> str:
    return db_url.replace("postgresql+psycopg://", "postgresql://")


def _create_database(pg_admin_url: str) -> str:
    name = f"t_{uuid.uuid4().hex}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_url(pg_admin_url).set(drivername="postgresql+psycopg", database=name)
    return url.render_as_string(hide_password=False)


def _drop_database(pg_admin_url: str, db_url: str) -> None:
    name = make_url(db_url).database
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def dbs(pg_admin_url: str) -> Iterator[Databases]:
    """A migrated identity database and a migrated audit database, each its own database."""
    identity = _create_database(pg_admin_url)
    audit = _create_database(pg_admin_url)
    try:
        run_alembic(
            script_location=IDENTITY_SCRIPTS,
            version_table=IDENTITY_VERSION_TABLE,
            url=identity,
            command="upgrade",
        )
        run_alembic(
            script_location=AUDIT_SCRIPTS,
            version_table=AUDIT_VERSION_TABLE,
            url=audit,
            command="upgrade",
        )
        yield Databases(identity=identity, audit=audit)
    finally:
        _drop_database(pg_admin_url, identity)
        _drop_database(pg_admin_url, audit)


def load_identity(db_url: str, scenario: Scenario) -> Loaded:
    """Replace identity's folders and grants with ``scenario`` (raw SQL, no level binding)."""
    folder_ids = tuple(uuid.uuid4() for _ in scenario.kinds)
    audit = ("system:test", "system:test")
    with psycopg.connect(_sync_url(db_url), autocommit=True) as conn:
        conn.execute("TRUNCATE folder, role_assignments, group_members, local_groups CASCADE")
        for index, kind in enumerate(scenario.kinds):  # parents always come first
            parent = scenario.parents[index]
            conn.execute(
                "INSERT INTO folder (id, parent_id, kind, slug, display_name, path, created_by, "
                "updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    folder_ids[index],
                    None if parent is None else folder_ids[parent],
                    kind,
                    scenario.paths[index].rsplit(".", 1)[-1],
                    scenario.paths[index],
                    scenario.paths[index],
                    *audit,
                ),
            )
        for index in sorted(scenario.blocks):
            conn.execute(
                "UPDATE folder SET block_inheritance = true WHERE id = %s", (folder_ids[index],)
            )
        for spec in scenario.grants:
            conn.execute(
                "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role, "
                "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, %s)",
                (folder_ids[spec.folder], "user", spec.user, spec.role, *audit),
            )
    tree = Tree(
        {
            folder_ids[i]: TreeFolder(
                folder_ids[i],
                None if scenario.parents[i] is None else folder_ids[scenario.parents[i] or 0],
                scenario.kinds[i],
            )
            for i in range(len(scenario.kinds))
        }
    )
    grants = tuple(
        Grant(folder_ids[spec.folder], "user", spec.user, spec.role) for spec in scenario.grants
    )
    events = tuple(
        (str(uuid.uuid4()), () if index == EMPTY else tree.chain(folder_ids[index]))
        for index in scenario.events
    )
    return Loaded(
        scenario,
        folder_ids,
        tree,
        frozenset(folder_ids[i] for i in scenario.blocks),
        grants,
        events,
    )


def _envelope(event_id: str, scope_path: Sequence[uuid.UUID], n: int) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "occurred_at": (BASE + timedelta(seconds=n)).isoformat(),
        "service": "contracts",
        "aggregate_type": "contract",
        "aggregate_id": f"c-{n}",
        "aggregate_version": None,
        "scope_path": [str(f) for f in scope_path],
        "action": "contract.created",
        "actor": {"type": "user", "id": "fake:author", "display": "Author", "via": "jwt"},
        "request_id": None,
        "before": None,
        "after": {"id": f"c-{n}"},
        "schema_version": 1,
    }


def _principal(user: str) -> Principal:
    subject = user.split(":", 1)[1]
    return Principal(
        id=user,
        type="user",
        display=subject,
        issuer="test",
        subject=subject,
        via="jwt",
        scopes=frozenset({"api"}),
    )


def reference(loaded: Loaded, user: str) -> frozenset[str]:
    """The events ``user`` may see according to the oracle alone."""
    want = folders_with(loaded.tree, loaded.blocks, loaded.grants, {("user", user)}, PERMISSION)
    root = loaded.tree.root_id
    return frozenset(
        event_id
        for event_id, scope_path in loaded.events
        if (scope_path[-1] if scope_path else root) in want
    )


async def visible_through_sql(dbs: Databases, loaded: Loaded) -> dict[str, frozenset[str]]:
    """Seed the audit store, then ask identity and audit, per user, exactly as the route does."""
    identity_engine = make_async_engine(dbs.identity)
    audit_engine = make_async_engine(dbs.audit)
    try:
        audit_factory = make_session_factory(audit_engine)
        with psycopg.connect(_sync_url(dbs.audit), autocommit=True) as conn:
            conn.execute("TRUNCATE audit_event")
        subject = subject_for("contracts", "contract", "contract.created")
        for n, (event_id, scope_path) in enumerate(loaded.events):
            payload = encode_envelope(_envelope(event_id, scope_path, n))
            assert await handle_payload(audit_factory, payload, subject=subject)
        authorizer = LocalAuthorizer(make_session_factory(identity_engine))
        seen: dict[str, frozenset[str]] = {}
        for user in USERS:
            scope = await authorizer.accessible_folders(_principal(user), PERMISSION)
            predicate = visible(scope)
            if predicate is None:
                seen[user] = frozenset()
                continue
            limit = len(loaded.events)
            async with audit_factory() as session:
                rows = (
                    (await session.execute(build_statement(predicate, AuditFilters(), limit)))
                    .scalars()
                    .all()
                )
            assert len(rows) <= limit
            seen[user] = frozenset(str(row.event_id) for row in rows)
        return seen
    finally:
        await identity_engine.dispose()
        await audit_engine.dispose()


def assert_equals_oracle(dbs: Databases, scenario: Scenario) -> dict[str, frozenset[str]]:
    loaded = load_identity(dbs.identity, scenario)
    got = asyncio.run(visible_through_sql(dbs, loaded))
    for user in USERS:
        assert got[user] == reference(loaded, user), (user, scenario)
    return got


@settings(deadline=None, max_examples=_EXAMPLES, suppress_health_check=_HEALTH)
@given(scenarios())
def test_sql_visibility_equals_the_oracle(dbs: Databases, scenario: Scenario) -> None:
    assert_equals_oracle(dbs, scenario)


# ---- named scenarios ------------------------------------------------------------------------
# One chain: 0 ROOT, 1 organization, 2 project, 3 team; a second organization is folder 4..6.
TWO_ORGS = ((1,), (1,))
ALL_EVENTS = (EMPTY, 0, 1, 2, 3, 4, 5, 6)  # empty, [root], every folder


def _scenario(*, blocks: Sequence[int] = (), grants: Sequence[GrantSpec] = ()) -> Scenario:
    return Scenario(TWO_ORGS, frozenset(blocks), tuple(grants), ALL_EVENTS)


def _event_ids(dbs: Databases, scenario: Scenario) -> tuple[dict[str, frozenset[str]], Loaded]:
    loaded = load_identity(dbs.identity, scenario)
    got = asyncio.run(visible_through_sql(dbs, loaded))
    for user in USERS:
        assert got[user] == reference(loaded, user), user
    return got, loaded


def _at(loaded: Loaded, *indexes: int) -> frozenset[str]:
    """The event ids seeded for ``ALL_EVENTS`` positions (EMPTY is position 0, ROOT 1, ...)."""
    return frozenset(loaded.events[ALL_EVENTS.index(i)][0] for i in indexes)


def test_a_global_reader_does_not_see_a_blocked_organization(dbs: Databases) -> None:
    got, loaded = _event_ids(
        dbs, _scenario(blocks=[1], grants=[GrantSpec(0, "fake:u1", "global_reader")])
    )
    # ROOT-only events and the unblocked organization, never the blocked one.
    assert got["fake:u1"] == _at(loaded, EMPTY, 0, 4, 5, 6)


def test_a_global_admin_is_never_cut(dbs: Databases) -> None:
    got, loaded = _event_ids(
        dbs, _scenario(blocks=[1, 3], grants=[GrantSpec(0, "fake:u1", "global_admin")])
    )
    assert got["fake:u1"] == _at(loaded, *ALL_EVENTS)


def test_a_grant_inside_a_blocked_subtree_adds_it_back_next_to_a_root_grant(
    dbs: Databases,
) -> None:
    got, loaded = _event_ids(
        dbs,
        _scenario(
            blocks=[1],
            grants=[GrantSpec(0, "fake:u1", "global_reader"), GrantSpec(1, "fake:u1", "org_admin")],
        ),
    )
    assert got["fake:u1"] == _at(loaded, *ALL_EVENTS)


def test_a_team_admin_sees_only_its_team(dbs: Databases) -> None:
    got, loaded = _event_ids(dbs, _scenario(grants=[GrantSpec(3, "fake:u1", "team_admin")]))
    assert got["fake:u1"] == _at(loaded, 3)


def test_an_org_admin_sees_its_organization_but_no_root_only_event(dbs: Databases) -> None:
    got, loaded = _event_ids(dbs, _scenario(grants=[GrantSpec(1, "fake:u1", "org_admin")]))
    assert got["fake:u1"] == _at(loaded, 1, 2, 3)


@pytest.mark.parametrize("role", ["policy_manager", "org_reader", "consumer", "team_reader"])
def test_roles_without_audit_query_see_nothing(dbs: Databases, role: str) -> None:
    folder = 0 if role in ("policy_manager", "consumer") else (1 if role == "org_reader" else 3)
    got, _ = _event_ids(dbs, _scenario(grants=[GrantSpec(folder, "fake:u1", role)]))
    assert got["fake:u1"] == frozenset()


def test_no_grants_means_an_empty_scope(dbs: Databases) -> None:
    got, _ = _event_ids(dbs, _scenario())
    assert got == {user: frozenset() for user in USERS}
