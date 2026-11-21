"""RBAC-03, RBAC-04: the SQL evaluator equals the brute-force oracle (D-28, D-29, D-31).

Hypothesis draws a tree (1-3 orgs x 1-3 projects x 1-3 teams under ROOT), a block subset on
organization and team folders, local groups with random membership, Entra directory group GUIDs
and up to ten grants over all eleven roles, made to every principal kind. Grants are inserted by
SQL, so level binding is deliberately bypassed: the evaluator must agree with the oracle for any
rows.

Every principal kind is resolved through the real ``PrincipalGate`` (a ``FakeDirectoryClient``
stands in for Graph; nothing is monkeypatched): a generic OIDC user, a local user, an Entra user
and an Entra service principal (both directly registered, with directory groups), a service
account (in local groups) presenting a key, and a PAT of a local user and of an Entra user (D-60,
D-80, the assumption-delta invariant). For each, the grantee set the gate produces, the effective
roles at every folder and ``LocalAuthorizer.check`` are compared with ``dc_auth.oracle``.

``dc_auth.oracle.decide`` has no notion of ``via``, so the decision comparison is like with like:
a JWT principal is compared on every sampled permission, a PAT or key principal only where the
permission is not ``interactive_only``. For a PAT or key principal and an ``interactive_only``
permission the refusal is asserted separately (D-79, D-35).
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from dc_auth import roles as auth_roles
from dc_auth.oracle import (
    Grant,
    Tree,
    TreeFolder,
    decide,
    folders_with,
    permissions_at,
    roles_at,
)
from dc_auth.principal import Principal
from dc_auth.roles import Role
from dc_auth.testing import ENTRA_TENANT_ID
from dc_core.db import make_async_engine, make_session_factory
from dc_core.migrations import run_alembic
from dc_core.problems import ProblemError
from dc_identity.access import Grantees, effective_roles, grantee_set, grantees_for
from dc_identity.authz import LocalAuthorizer, is_interactive
from dc_identity.credentials import hash_secret
from dc_identity.directory.fake import FakeDirectoryClient
from dc_identity.gate import PrincipalGate
from dc_identity.migrate import SCRIPT_LOCATION, VERSION_TABLE
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.integration

_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
settings.register_profile("ci", max_examples=150, deadline=None, suppress_health_check=_HEALTH)
_EXAMPLES = 150 if os.environ.get("HYPOTHESIS_PROFILE") == "ci" else 40

# Hand-written edge cases keep using three generic-OIDC users (the gate passes them unchanged).
USERS = ("fake:u1", "fake:u2", "fake:u3")

_NAMESPACE = uuid.UUID("5d2e1a60-0b0c-4c6e-9d0e-4f1b2a3c4d5e")


def _guid(name: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, name))


# The real principals the generated scenarios use. Ids are fixed so grants name them and rows are
# seeded once per example.
FAKE_ID = "fake:u1"
LOCAL_UUID = _guid("local-user")
LOCAL_ID = f"local:{LOCAL_UUID}"
ENTRA_USER_OID = _guid("entra-user")
ENTRA_USER_ID = f"entra:{ENTRA_TENANT_ID}:{ENTRA_USER_OID}"
ENTRA_USER_ROW = _guid("entra-user-row")
SP_OID = _guid("service-principal")
SP_ID = f"entra:{ENTRA_TENANT_ID}:{SP_OID}"
SA_UUID = _guid("service-account")
SA_ID = f"sa:{SA_UUID}"
# Directory group GUIDs the Entra principals are drawn into, and grants are made to.
ENTRA_NAMES = (_guid("entra-group-a"), _guid("entra-group-b"))
IDENTITIES = (FAKE_ID, LOCAL_ID, ENTRA_USER_ID, SP_ID, SA_ID)
MEMBER_IDENTITIES = (FAKE_ID, LOCAL_ID, ENTRA_USER_ID, SA_ID)  # a local group holds these types
ENTRA_HOLDERS = (ENTRA_USER_ID, SP_ID)  # the principals the directory puts into groups
GROUP_COUNT = 2
ROLES = tuple(str(role) for role in Role)
PERMISSIONS_UNDER_TEST = (
    "folder:read",
    "contract:read",
    "contract:read-metadata",
    "contract:create",
    "folder:set-block",
)
# interactive_only permissions (D-79): a folder one with a visibility fallback, one root-evaluated.
INTERACTIVE_PERMISSIONS_UNDER_TEST = ("grant:manage", "service-account:deactivate", "user:create")


def grantee_type_of(identity: str) -> str:
    """The ``principal_type`` the identity's own grants and memberships carry."""
    if identity == SP_ID:
        return "service_principal"
    if identity == SA_ID:
        return "service_account"
    return "user"


@dataclass(frozen=True)
class GrantSpec:
    folder: int  # index into Scenario.kinds
    # ("user", "fake:u1") | ("service_principal", "entra:..") | ("service_account", "sa:..")
    # | ("local_group", "0") | ("entra_group", "<group guid>")
    grantee: tuple[str, str]
    role: str


@dataclass(frozen=True)
class Scenario:
    shape: tuple[tuple[int, ...], ...]  # per org: teams per project
    blocks: frozenset[int]
    members: frozenset[tuple[int, str]]  # (group index, user or service account principal id)
    entra: tuple[tuple[str, tuple[str, ...]], ...]  # principal id -> directory group ids
    grants: tuple[GrantSpec, ...]
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

    def entra_of(self, user: str) -> tuple[str, ...]:
        return dict(self.entra).get(user, ())


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    shape = tuple(
        tuple(draw(st.integers(1, 3)) for _ in range(draw(st.integers(1, 3))))
        for _ in range(draw(st.integers(1, 3)))
    )
    probe = Scenario(shape, frozenset(), frozenset(), (), ())
    blockable = [i for i, kind in enumerate(probe.kinds) if kind in ("organization", "team")]
    blocks = draw(st.sets(st.sampled_from(blockable))) if blockable else set()
    members = draw(
        st.sets(
            st.tuples(st.integers(0, GROUP_COUNT - 1), st.sampled_from(MEMBER_IDENTITIES)),
            max_size=6,
        )
    )
    entra = tuple(
        (holder, tuple(draw(st.lists(st.sampled_from(ENTRA_NAMES), unique=True, max_size=2))))
        for holder in ENTRA_HOLDERS
    )
    grantees = (
        [(grantee_type_of(identity), identity) for identity in IDENTITIES]
        + [("local_group", str(g)) for g in range(GROUP_COUNT)]
        + [("entra_group", name) for name in ENTRA_NAMES]
    )
    grant_specs = draw(
        st.sets(
            st.builds(
                GrantSpec,
                folder=st.integers(0, len(probe.kinds) - 1),
                grantee=st.sampled_from(grantees),
                role=st.sampled_from(ROLES),
            ),
            max_size=10,
        )
    )
    ordered = tuple(sorted(grant_specs, key=lambda g: (g.folder, g.grantee, g.role)))
    return Scenario(shape, frozenset(blocks), frozenset(members), entra, ordered)


@dataclass
class Loaded:
    scenario: Scenario
    folder_ids: tuple[uuid.UUID, ...]
    group_ids: tuple[uuid.UUID, ...]
    tree: Tree
    blocks: frozenset[uuid.UUID]
    grants: tuple[Grant, ...]

    def grantees_of(self, identity: str) -> frozenset[tuple[str, str]]:
        """The oracle's grantee set, built from the scenario alone (never from the gate)."""
        gtype = grantee_type_of(identity)
        found: set[tuple[str, str]] = {(gtype, identity)}
        # A membership row is typed ``user`` or ``service_account`` and matches only that type.
        found.update(
            ("local_group", str(self.group_ids[g]))
            for g, member in self.scenario.members
            if member == identity and _member_type(member) == gtype
        )
        found.update(("entra_group", name) for name in self.scenario.entra_of(identity))
        return frozenset(found)


def _member_type(member_id: str) -> str:
    return "service_account" if member_id.startswith("sa:") else "user"


def _sync_url(db_url: str) -> str:
    return db_url.replace("postgresql+psycopg://", "postgresql://")


# Public ids of the seeded credentials; the secret is a fixed throwaway (the principals are built
# directly, as the opaque resolver would, so no token is ever presented).
LOCAL_PAT = "00000000000000a1"
ENTRA_PAT = "00000000000000a2"
SA_KEY = "00000000000000a3"


def _seed_principals(conn: psycopg.Connection[Any], root_id: uuid.UUID) -> None:
    """Rows for every principal kind: all active, directly registered, no session cutoff."""
    audit = ("system:test", "system:test")
    conn.execute(
        "INSERT INTO users (id, username, display_name, created_by, updated_by) "
        "VALUES (%s, 'oracle-local', 'Oracle Local', %s, %s) ON CONFLICT DO NOTHING",
        (uuid.UUID(LOCAL_UUID), *audit),
    )
    conn.execute(
        "INSERT INTO entra_users (id, tenant_id, object_id, display_name, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (ENTRA_USER_ROW, ENTRA_TENANT_ID, ENTRA_USER_OID, ENTRA_USER_OID, *audit),
    )
    conn.execute(
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, directly_registered, created_by, updated_by) "
        "VALUES ('user', %s, %s, NULL, 'oracle entra user', true, %s, %s)",
        (ENTRA_TENANT_ID, ENTRA_USER_OID, *audit),
    )
    conn.execute(
        "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
        "display_name, directly_registered, created_by, updated_by) "
        "VALUES ('service_principal', %s, %s, %s, 'oracle pipeline', true, %s, %s)",
        (ENTRA_TENANT_ID, SP_OID, root_id, *audit),
    )
    conn.execute(
        "INSERT INTO service_accounts (id, owner_folder_id, name, created_by, updated_by) "
        "VALUES (%s, %s, 'oracle-sa', %s, %s)",
        (uuid.UUID(SA_UUID), root_id, *audit),
    )
    secret_hash = hash_secret("oracle-throwaway-secret")
    # read_only stays false: narrowing has no counterpart in dc_auth.oracle (test_pat_read_only).
    for public_id, kind, owner_column, owner in (
        (LOCAL_PAT, "pat", "local_user_id", uuid.UUID(LOCAL_UUID)),
        (ENTRA_PAT, "pat", "entra_user_id", ENTRA_USER_ROW),
        (SA_KEY, "service_account_key", "service_account_id", uuid.UUID(SA_UUID)),
    ):
        conn.execute(
            f"INSERT INTO api_credentials (public_id, secret_hash, kind, {owner_column}, name, "  # noqa: S608
            "read_only, expires_at, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, 'oracle', false, now() + interval '1 day', %s, %s)",
            (public_id, secret_hash, kind, owner, *audit),
        )


def load_scenario(db_url: str, scenario: Scenario, *, shuffle_seed: int | None = None) -> Loaded:
    """Replace the database content with ``scenario``; optionally shuffle row insertion order."""
    rng = random.Random(shuffle_seed)  # noqa: S311 - test ordering, not security

    def shuffled(items: Sequence[Any]) -> list[Any]:
        ordered = list(items)
        if shuffle_seed is not None:
            rng.shuffle(ordered)
        return ordered

    folder_ids = tuple(uuid.uuid4() for _ in scenario.kinds)
    group_ids = tuple(uuid.uuid4() for _ in range(GROUP_COUNT))
    audit = ("system:test", "system:test")
    with psycopg.connect(_sync_url(db_url), autocommit=True) as conn:
        conn.execute(
            "TRUNCATE folder, role_assignments, group_members, local_groups, entra_registrations, "
            "entra_users, service_accounts, api_credentials"
        )
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
        for index in shuffled(sorted(scenario.blocks)):
            conn.execute(
                "UPDATE folder SET block_inheritance = true WHERE id = %s", (folder_ids[index],)
            )
        for index, group_id in enumerate(group_ids):
            conn.execute(
                "INSERT INTO local_groups (id, owner_folder_id, name, created_by, updated_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (group_id, folder_ids[0], f"group-{index}", *audit),
            )
        for group, user in shuffled(sorted(scenario.members)):
            conn.execute(
                "INSERT INTO group_members (group_id, member_type, member_id, created_by, "
                "updated_by) VALUES (%s, %s, %s, %s, %s)",
                (group_ids[group], _member_type(user), user, *audit),
            )
        _seed_principals(conn, folder_ids[0])
        for spec in shuffled(scenario.grants):
            kind, name = spec.grantee
            principal_id = str(group_ids[int(name)]) if kind == "local_group" else name
            conn.execute(
                "INSERT INTO role_assignments (folder_id, principal_type, principal_id, role, "
                "created_by, updated_by) VALUES (%s, %s, %s, %s, %s, %s)",
                (folder_ids[spec.folder], kind, principal_id, spec.role, *audit),
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
        Grant(
            folder_ids[spec.folder],
            spec.grantee[0],
            str(group_ids[int(spec.grantee[1])])
            if spec.grantee[0] == "local_group"
            else spec.grantee[1],
            spec.role,
        )
        for spec in scenario.grants
    )
    return Loaded(
        scenario,
        folder_ids,
        group_ids,
        tree,
        frozenset(folder_ids[i] for i in scenario.blocks),
        grants,
    )


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


@dataclass(frozen=True)
class Subject:
    """A principal as the request pipeline would build it, and the identity its grants carry."""

    label: str
    principal: Principal
    identity: str  # the grantee identity: a PAT acts as its owner, a key as its service account


def _jwt_principal(identity: str, kind: str, oid: str) -> Principal:
    return Principal(
        id=identity,
        type=kind,  # type: ignore[arg-type]
        display=oid,  # the entra_users row's display_name, so the gate writes nothing
        issuer="test",
        subject=oid,
        via="jwt",
        scopes=frozenset({"api"}),
        issued_at=int(time.time()),  # the Plan 13 cutoff is NULL for every seeded row
    )


def _opaque_principal(identity: str, kind: str, via: str, public_id: str) -> Principal:
    return Principal(
        id=identity,
        type=kind,  # type: ignore[arg-type]
        display="oracle",
        issuer="opaque",
        subject=public_id,
        via=via,  # type: ignore[arg-type]
        scopes=frozenset({"api"}),
        credential_id=public_id,
    )


def all_subjects() -> tuple[Subject, ...]:
    return (
        Subject("generic OIDC user", _principal(FAKE_ID), FAKE_ID),
        Subject("local user", _jwt_principal(LOCAL_ID, "user", LOCAL_UUID), LOCAL_ID),
        Subject("entra user", _jwt_principal(ENTRA_USER_ID, "user", ENTRA_USER_OID), ENTRA_USER_ID),
        Subject("service principal", _jwt_principal(SP_ID, "service_principal", SP_OID), SP_ID),
        Subject(
            "service account key",
            _opaque_principal(SA_ID, "service_account", "api_key", SA_KEY),
            SA_ID,
        ),
        Subject("local user PAT", _opaque_principal(LOCAL_ID, "pat", "pat", LOCAL_PAT), LOCAL_ID),
        Subject(
            "entra user PAT",
            _opaque_principal(ENTRA_USER_ID, "pat", "pat", ENTRA_PAT),
            ENTRA_USER_ID,
        ),
    )


def make_gate(factory: Any, scenario: Scenario) -> PrincipalGate:
    """The real gate over a fake directory that puts the Entra principals into their groups."""
    directory = FakeDirectoryClient()
    directory.add_principal("user", ENTRA_USER_OID, groups=scenario.entra_of(ENTRA_USER_ID))
    directory.add_principal("service_principal", SP_OID, groups=scenario.entra_of(SP_ID))
    return PrincipalGate(factory, directory)


def comparable(principal: Principal, permission: str) -> bool:
    """``decide`` knows no ``via``: compare it only where interactivity cannot change it (D-79)."""
    return is_interactive(principal) or not auth_roles.PERMISSIONS[permission].interactive_only


async def collect_roles(db_url: str, loaded: Loaded) -> dict[tuple[str, uuid.UUID], frozenset[str]]:
    engine = make_async_engine(db_url)
    try:
        factory = make_session_factory(engine)
        out: dict[tuple[str, uuid.UUID], frozenset[str]] = {}
        async with factory() as session:
            for user in USERS:
                grantees = Grantees("user", user, loaded.scenario.entra_of(user))
                for folder_id in loaded.folder_ids:
                    out[(user, folder_id)] = await effective_roles(session, grantees, folder_id)
        return out
    finally:
        await engine.dispose()


async def assert_matches_oracle(db_url: str, loaded: Loaded) -> None:
    engine = make_async_engine(db_url)
    try:
        factory = make_session_factory(engine)
        gate = make_gate(factory, loaded.scenario)
        authorizer = LocalAuthorizer(factory, gate)
        for subject in all_subjects():
            expected_grantees = loaded.grantees_of(subject.identity)
            # The gate resolves first, as in every request; grantees_for returns its answer.
            await gate.resolve(subject.principal)
            grantees = grantees_for(subject.principal)
            assert (grantees.principal_type, grantees.principal_id) == (
                grantee_type_of(subject.identity),
                subject.identity,
            ), subject.label
            async with factory() as session:
                assert await grantee_set(session, grantees) == expected_grantees, subject.label
                for folder_id in loaded.folder_ids:
                    got = await effective_roles(session, grantees, folder_id)
                    want = roles_at(
                        loaded.tree, loaded.blocks, loaded.grants, expected_grantees, folder_id
                    )
                    assert got == want, (
                        subject.label,
                        loaded.tree.folders[folder_id].kind,
                        got,
                        want,
                    )
        for subject_index, subject in enumerate(all_subjects()):
            expected_grantees = loaded.grantees_of(subject.identity)
            for folder_index, folder_id in enumerate(loaded.folder_ids):
                # Each decision runs the gate and several queries; roles and grantees above cover
                # every folder for every principal, so decisions rotate over a third of the
                # folders per principal (every folder is still decided for some principal).
                if (folder_index + subject_index) % 3 != 0:
                    continue
                held = permissions_at(
                    loaded.tree, loaded.blocks, loaded.grants, expected_grantees, folder_id
                )
                for permission in (*PERMISSIONS_UNDER_TEST, *INTERACTIVE_PERMISSIONS_UNDER_TEST):
                    want_outcome = decide(
                        loaded.tree,
                        loaded.blocks,
                        loaded.grants,
                        expected_grantees,
                        permission,
                        folder_id,
                    )
                    where = (subject.label, permission, folder_id)
                    if comparable(subject.principal, permission):
                        decision = await authorizer.check(subject.principal, permission, folder_id)
                        assert decision.outcome.value == want_outcome, where
                        if want_outcome == "allow" and permission in PERMISSIONS_UNDER_TEST:
                            assert decision.permissions == held, where
                            assert decision.folder is not None and decision.folder.id == folder_id
                    elif want_outcome == "allow":
                        # A PAT or key the owner's grants allow still may not do this (D-79).
                        with pytest.raises(ProblemError) as refused:
                            await authorizer.check(subject.principal, permission, folder_id)
                        assert refused.value.status == 403, where
                        assert refused.value.slug == "interactive-session-required", where
                    else:
                        # Never ALLOW, and never an interactive refusal that would reveal a target
                        # the caller cannot see (D-35): the same outcome decide gives.
                        decision = await authorizer.check(subject.principal, permission, folder_id)
                        assert decision.outcome.value == want_outcome, where
    finally:
        await engine.dispose()


SCOPE_PERMISSIONS = (
    "folder:read",
    "contract:read",
    "contract:read-metadata",
    "template:read",
    "group:read",
)


async def assert_scope_matches_oracle(db_url: str, loaded: Loaded) -> None:
    """``FolderScope.covers`` on the compact scope equals ``folders_with`` for every folder."""
    engine = make_async_engine(db_url)
    try:
        factory = make_session_factory(engine)
        authorizer = LocalAuthorizer(factory, make_gate(factory, loaded.scenario))
        for subject in all_subjects():
            grantees = loaded.grantees_of(subject.identity)
            for permission in SCOPE_PERMISSIONS:
                scope = await authorizer.accessible_folders(subject.principal, permission)
                want = folders_with(loaded.tree, loaded.blocks, loaded.grants, grantees, permission)
                for folder_id in loaded.folder_ids:
                    got = scope.covers(folder_id, loaded.tree.chain(folder_id))
                    assert got == (folder_id in want), (
                        subject.label,
                        permission,
                        loaded.tree.folders[folder_id].kind,
                        got,
                    )
                # compactness: an `all` scope never lists a folder it already covers
                for folder_id in scope.folder_ids if scope.all else ():
                    chain = loaded.tree.chain(folder_id)
                    assert any(a in scope.excluded_subtrees for a in chain), (
                        subject.label,
                        permission,
                    )
    finally:
        await engine.dispose()


@pytest.fixture
def rbac_db(fresh_db_url: str) -> str:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=fresh_db_url,
        command="upgrade",
    )
    return fresh_db_url


@settings(
    deadline=None,
    max_examples=_EXAMPLES,
    suppress_health_check=_HEALTH,
)
@given(scenarios())
def test_sql_roles_and_decisions_equal_the_oracle(rbac_db: str, scenario: Scenario) -> None:
    loaded = load_scenario(rbac_db, scenario)
    asyncio.run(assert_matches_oracle(rbac_db, loaded))
    asyncio.run(assert_scope_matches_oracle(rbac_db, loaded))


# ---- hand-written edge cases for the RBAC-04 truths -------------------------------------------


def _chain_scenario(*, blocks: Sequence[int] = (), grants: Sequence[GrantSpec] = ()) -> Scenario:
    # folders: 0 root, 1 org, 2 project, 3 team
    return Scenario(((1,),), frozenset(blocks), frozenset(), (), tuple(grants))


def _roles_for(db_url: str, loaded: Loaded, user: str, index: int) -> frozenset[str]:
    return asyncio.run(collect_roles(db_url, loaded))[(user, loaded.folder_ids[index])]


def test_adjacency_a_grant_at_the_blocked_folder_applies_but_its_parent_grant_does_not(
    rbac_db: str,
) -> None:
    scenario = _chain_scenario(
        blocks=[1],
        grants=[
            GrantSpec(0, ("user", "fake:u1"), "global_reader"),
            GrantSpec(1, ("user", "fake:u1"), "org_reader"),
            GrantSpec(2, ("user", "fake:u1"), "project_editor"),
        ],
    )
    loaded = load_scenario(rbac_db, scenario)
    roles = asyncio.run(collect_roles(rbac_db, loaded))
    ids = loaded.folder_ids
    assert roles[("fake:u1", ids[0])] == {"global_reader"}
    assert roles[("fake:u1", ids[1])] == {"org_reader"}  # root grant cut at the blocked org
    assert roles[("fake:u1", ids[3])] == {"org_reader", "project_editor"}
    for user in USERS:
        for folder_id in ids:
            assert roles[(user, folder_id)] == roles_at(
                loaded.tree, loaded.blocks, loaded.grants, loaded.grantees_of(user), folder_id
            )


def test_empty_no_blocks_is_purely_additive_and_no_grants_means_no_roles(rbac_db: str) -> None:
    nothing = load_scenario(rbac_db, _chain_scenario())
    roles = asyncio.run(collect_roles(rbac_db, nothing))
    assert all(held == frozenset() for held in roles.values())

    async def denied() -> list[str]:
        engine = make_async_engine(rbac_db)
        try:
            authorizer = LocalAuthorizer(make_session_factory(engine))
            return [
                (await authorizer.check(_principal("fake:u2"), perm, folder_id)).outcome.value
                for folder_id in nothing.folder_ids
                for perm in ("folder:read", "contract:read", "contract:create", "folder:set-block")
            ]
        finally:
            await engine.dispose()

    assert set(asyncio.run(denied())) == {"not_found"}

    additive = load_scenario(
        rbac_db,
        _chain_scenario(
            grants=[
                GrantSpec(1, ("user", "fake:u1"), "org_reader"),
                GrantSpec(3, ("user", "fake:u1"), "team_editor"),
            ]
        ),
    )
    roles = asyncio.run(collect_roles(rbac_db, additive))
    assert roles[("fake:u1", additive.folder_ids[3])] == {"org_reader", "team_editor"}
    assert roles[("fake:u1", additive.folder_ids[2])] == {"org_reader"}
    assert roles[("fake:u1", additive.folder_ids[0])] == frozenset()


def test_ordering_the_deepest_block_decides_and_insertion_order_is_irrelevant(
    rbac_db: str,
) -> None:
    # two orgs' worth of chain: root -> org(blocked) -> project -> team(blocked)
    scenario = _chain_scenario(
        blocks=[1, 3],
        grants=[
            GrantSpec(0, ("user", "fake:u1"), "global_admin"),
            GrantSpec(0, ("user", "fake:u2"), "consumer"),
            GrantSpec(1, ("user", "fake:u2"), "policy_manager"),
            GrantSpec(2, ("user", "fake:u2"), "project_admin"),
            GrantSpec(3, ("local_group", "0"), "team_reader"),
            GrantSpec(2, ("entra_group", "entra-a"), "project_editor"),
        ],
    )
    scenario = Scenario(
        scenario.shape,
        scenario.blocks,
        frozenset({(0, "fake:u3")}),
        (("fake:u3", ("entra-a",)),),
        scenario.grants,
    )
    baseline = load_scenario(rbac_db, scenario)
    expected = asyncio.run(collect_roles(rbac_db, baseline))
    # the team block cuts everything granted above it, except the global admin
    team = baseline.folder_ids[3]
    assert expected[("fake:u1", team)] == {"global_admin"}
    assert expected[("fake:u2", team)] == frozenset()
    assert expected[("fake:u3", team)] == {"team_reader"}
    # u2 at the project: the org block cuts the root consumer, org and project grants remain
    assert expected[("fake:u2", baseline.folder_ids[2])] == {"policy_manager", "project_admin"}

    for seed in range(5):
        shuffled = load_scenario(rbac_db, scenario, shuffle_seed=seed)
        got = asyncio.run(collect_roles(rbac_db, shuffled))
        positional = {
            (user, shuffled.folder_ids.index(fid)): held for (user, fid), held in got.items()
        }
        assert positional == {
            (user, baseline.folder_ids.index(fid)): held for (user, fid), held in expected.items()
        }


def _scope_for(db_url: str, loaded: Loaded, user: str, permission: str) -> Any:
    async def run() -> Any:
        engine = make_async_engine(db_url)
        try:
            authorizer = LocalAuthorizer(make_session_factory(engine))
            return await authorizer.accessible_folders(_principal(user), permission)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_scope_a_root_consumer_behind_a_blocked_org_is_cut_but_the_admin_is_not(
    rbac_db: str,
) -> None:
    scenario = _chain_scenario(
        blocks=[1],
        grants=[
            GrantSpec(0, ("user", "fake:u1"), "global_admin"),
            GrantSpec(0, ("user", "fake:u2"), "consumer"),
            GrantSpec(3, ("user", "fake:u3"), "team_reader"),
        ],
    )
    loaded = load_scenario(rbac_db, scenario)
    org, team = loaded.folder_ids[1], loaded.folder_ids[3]
    admin = _scope_for(rbac_db, loaded, "fake:u1", "contract:read-published")
    assert admin.all and admin.excluded_subtrees == frozenset() and not admin.folder_ids
    consumer = _scope_for(rbac_db, loaded, "fake:u2", "contract:read-published")
    assert consumer.all and consumer.excluded_subtrees == {org}
    assert not consumer.covers(team, loaded.tree.chain(team))
    assert consumer.covers(loaded.folder_ids[0], loaded.tree.chain(loaded.folder_ids[0]))
    reader = _scope_for(rbac_db, loaded, "fake:u3", "contract:read")
    assert not reader.all and reader.folder_ids == {team}  # exact expansion, no wire id list
    nothing = _scope_for(rbac_db, loaded, "fake:u1", "folder:list")
    assert nothing.is_empty  # a non-folder permission never yields a scope


def test_scope_a_grant_inside_a_blocked_subtree_is_listed_exactly_next_to_a_root_grant(
    rbac_db: str,
) -> None:
    scenario = _chain_scenario(
        blocks=[1],
        grants=[
            GrantSpec(0, ("user", "fake:u1"), "global_reader"),
            GrantSpec(1, ("user", "fake:u1"), "org_reader"),
        ],
    )
    loaded = load_scenario(rbac_db, scenario)
    scope = _scope_for(rbac_db, loaded, "fake:u1", "folder:read")
    org = loaded.folder_ids[1]
    below = set(loaded.folder_ids[1:])
    assert scope.all and scope.excluded_subtrees == {org}
    assert scope.folder_ids == below  # the org and everything under it, added back exactly


def test_a_pat_or_key_is_refused_what_its_owner_may_do_interactively_and_only_that(
    rbac_db: str,
) -> None:
    """Non-vacuity for the D-79 branch: the oracle allows, the jwt is allowed, the rest refused."""
    scenario = _chain_scenario(
        grants=[
            GrantSpec(0, ("user", LOCAL_ID), "global_admin"),
            GrantSpec(0, ("user", ENTRA_USER_ID), "global_admin"),
            GrantSpec(0, ("service_account", SA_ID), "global_admin"),
            GrantSpec(0, ("service_principal", SP_ID), "global_admin"),
        ]
    )
    loaded = load_scenario(rbac_db, scenario)
    team = loaded.folder_ids[3]

    async def outcomes() -> dict[str, str]:
        engine = make_async_engine(rbac_db)
        try:
            factory = make_session_factory(engine)
            authorizer = LocalAuthorizer(factory, make_gate(factory, scenario))
            found: dict[str, str] = {}
            for subject in all_subjects():
                if subject.identity not in (LOCAL_ID, ENTRA_USER_ID, SA_ID, SP_ID):
                    continue
                try:
                    decision = await authorizer.check(subject.principal, "grant:manage", team)
                    found[subject.label] = decision.outcome.value
                    # Not interactive-only: the same PAT or key is allowed as the owner.
                    read = await authorizer.check(subject.principal, "folder:read", team)
                    assert read.outcome.value == "allow", subject.label
                except ProblemError as exc:
                    found[subject.label] = exc.slug
            return found
        finally:
            await engine.dispose()

    assert asyncio.run(outcomes()) == {
        "local user": "allow",
        "entra user": "allow",
        "service account key": "interactive-session-required",
        # WR-01: an Entra service principal's app token is a JWT but not a person.
        "service principal": "interactive-session-required",
        "local user PAT": "interactive-session-required",
        "entra user PAT": "interactive-session-required",
    }
