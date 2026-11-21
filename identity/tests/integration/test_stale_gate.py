"""WR-03: authorization writes re-check their permission inside the transaction, under the lock.

The ``require()`` gate runs in its own session before the handler. A revocation that commits in
between leaves the handler holding a stale ALLOW. These tests call the domain functions directly
with a principal that currently holds no grant (as if revoked after the gate) and expect the
re-check to refuse the write: no state change and no audit event.

The same holds for a target that vanished after the gate: every domain write looks its target up
again under the lock and answers the route's 404 (or the fixed 422 for an unusable grantee) itself,
and a folder delete re-reads its blockers after the purge calls.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dc_auth.principal import Principal, principal_to_actor
from dc_auth.testing import ENTRA_TENANT_ID
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from dc_identity.api.entra import get_group_registration, get_service_principal_registration
from dc_identity.api.grants import get_grant
from dc_identity.api.groups import get_group
from dc_identity.api.service_accounts import get_service_account
from dc_identity.authz import load_folder_facts
from dc_identity.credentials import create_pat, create_service_account_key
from dc_identity.deactivation import deactivate_service_account, deactivate_service_principal
from dc_identity.entra import register_group, unregister_group
from dc_identity.folder_usage import RemoteFolderUsage
from dc_identity.folders import blockers_for, delete_folder, set_block_inheritance
from dc_identity.grants import create_grant, revoke_grant
from dc_identity.groups import (
    add_member,
    create_group,
    delete_group,
    remove_member,
    rename_group,
)
from dc_identity.service_accounts import create_service_account, rename_service_account

pytestmark = pytest.mark.integration

Sql = Callable[..., list[dict[str, Any]]]
AdminFlow = Callable[[Any, Any], Awaitable[str]]


def principal(sub: str) -> Principal:
    return Principal(
        id=f"fake:{sub}",
        type="user",
        display=sub,
        issuer="http://fake",
        subject=sub,
        via="jwt",
        scopes=frozenset({"api"}),
    )


def uow_for(app: Any, actor: Principal) -> UnitOfWork:
    state = app.state
    return UnitOfWork(
        state.session_factory,
        actor=principal_to_actor(actor),
        outbox_model=state.outbox_model,
        service=state.service,
    )


def count(sql: Sql, env: Any, action: str) -> int:
    rows = sql(env.db_url, "SELECT count(*) AS n FROM outbox WHERE action = %s", action)
    return int(rows[0]["n"])


async def test_set_block_inheritance_rechecks_the_permission_under_the_lock(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    grant_sql: Callable[..., None],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        org, _project, team = await folder_chain(client, token, root, "stale")
        grant_sql(env.db_url, "fake:oa", "org_admin", org)
        folder_id = uuid.UUID(team)

        # No grant (revoked after the gate): refused, flag untouched, nothing audited.
        with pytest.raises(ProblemError) as denied:
            async with uow_for(env.app, principal("revoked")) as uow:
                await set_block_inheritance(uow, principal("revoked"), folder_id, enabled=True)
        assert denied.value.status == 403
        assert denied.value.slug == "forbidden"
        assert count(sql, env, "folder.block_set") == 0
        flag = sql(env.db_url, "SELECT block_inheritance FROM folder WHERE id = %s", team)
        assert flag[0]["block_inheritance"] is False

        # Positive control: an Org admin (permission evaluated at the parent Project) succeeds.
        async with uow_for(env.app, principal("oa")) as uow:
            folder, changed = await set_block_inheritance(
                uow, principal("oa"), folder_id, enabled=True
            )
        assert changed is True
        assert folder.block_inheritance is True
        assert count(sql, env, "folder.block_set") == 1


async def test_create_group_rechecks_the_permission_under_the_lock(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    grant_sql: Callable[..., None],
    sql: Sql,
) -> None:
    env = make_identity()
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        org, _project, _team = await folder_chain(client, token, root, "stalegrp")
        grant_sql(env.db_url, "fake:oa", "org_admin", org)
        async with env.app.state.session_factory() as session:
            owner = await load_folder_facts(session, uuid.UUID(org))
        assert owner is not None

        with pytest.raises(ProblemError) as denied:
            async with uow_for(env.app, principal("revoked")) as uow:
                await create_group(uow, principal("revoked"), owner, name="never")
        assert denied.value.status == 403
        assert count(sql, env, "group.created") == 0
        assert sql(env.db_url, "SELECT id FROM local_groups WHERE name = 'never'") == []

        async with uow_for(env.app, principal("oa")) as uow:
            group = await create_group(uow, principal("oa"), owner, name="ok")
        assert group.name == "ok"
        assert count(sql, env, "group.created") == 1


DomainCall = Callable[[UnitOfWork], Awaitable[Any]]


async def refusal(app: Any, actor: Principal, call: DomainCall) -> ProblemError:
    with pytest.raises(ProblemError) as caught:
        async with uow_for(app, actor) as uow:
            await call(uow)
    return caught.value


async def test_every_domain_write_answers_404_for_a_target_that_is_gone(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    grant_sql: Callable[..., None],
    sql: Sql,
) -> None:
    env = make_identity()
    ga = principal("ga")
    gone = uuid.uuid4()
    expires = datetime.now(UTC) + timedelta(days=30)
    calls: dict[str, DomainCall] = {
        "create_service_account": lambda uow: create_service_account(
            uow, ga, owner_folder_id=gone, name="x"
        ),
        "rename_service_account": lambda uow: rename_service_account(uow, ga, gone, name="x"),
        "create_service_account_key": lambda uow: create_service_account_key(
            uow, ga, gone, name="k", expires_at=expires, settings=env.settings
        ),
        "deactivate_service_account": lambda uow: deactivate_service_account(uow, ga, gone),
        "deactivate_service_principal": lambda uow: deactivate_service_principal(uow, ga, gone),
        "create_grant": lambda uow: create_grant(
            uow,
            ga,
            gone,
            principal_type="user",
            principal_id=f"local:{uuid.uuid4()}",
            role="team_reader",
        ),
        "revoke_grant": lambda uow: revoke_grant(uow, ga, gone),
        "add_member": lambda uow: add_member(uow, ga, gone, username="someone", principal_id=None),
        "rename_group": lambda uow: rename_group(uow, ga, gone, name="x"),
        "remove_member": lambda uow: remove_member(uow, ga, gone, f"local:{uuid.uuid4()}"),
        "delete_group": lambda uow: delete_group(uow, ga, gone),
        "register_group": lambda uow: register_group(
            uow,
            ga,
            tenant_id=ENTRA_TENANT_ID,
            owner_folder_id=gone,
            object_id=str(uuid.uuid4()),
            display_name="g",
        ),
        "unregister_group": lambda uow: unregister_group(uow, ga, gone),
        "delete_folder": lambda uow: delete_folder(uow, ga, gone, RemoteFolderUsage()),
        "blockers_for": lambda uow: blockers_for(uow.session, gone, RemoteFolderUsage()),
    }
    async with asgi_client(env.app) as client:
        await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        grant_sql(env.db_url, ga.id, "global_admin", root)
        before = sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"]
        answers = {name: await refusal(env.app, ga, call) for name, call in calls.items()}
    assert {name: (e.status, e.slug) for name, e in answers.items()} == {
        name: (404, "not-found") for name in calls
    }
    assert sql(env.db_url, "SELECT count(*) AS n FROM outbox")[0]["n"] == before


async def test_unusable_grantees_and_members_are_refused_inside_the_write(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    grant_sql: Callable[..., None],
    create_user: Callable[..., Awaitable[Any]],
    sql: Sql,
) -> None:
    env = make_identity()
    ga = principal("ga")
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        org, _project, team = await folder_chain(client, token, root, "guard")
        grant_sql(env.db_url, ga.id, "global_admin", root)
        member = await create_user(client, token, "member")
        async with env.app.state.session_factory() as session:
            owner = await load_folder_facts(session, uuid.UUID(org))
        assert owner is not None
        async with uow_for(env.app, ga) as uow:
            group = await create_group(uow, ga, owner, name="crew")
        # Two roles of the group at one folder: the membership ceiling is evaluated once there.
        for role in ("team_reader", "team_editor"):
            grant_sql(env.db_url, str(group.id), role, team, "local_group")
        async with uow_for(env.app, ga) as uow:
            await add_member(uow, ga, group.id, username=member.username, principal_id=None)
        team_id = uuid.UUID(team)

        def grant_to(principal_type: str, principal_id: str) -> DomainCall:
            return lambda uow: create_grant(
                uow,
                ga,
                team_id,
                principal_type=principal_type,
                principal_id=principal_id,
                role="team_reader",
            )

        answers = {
            "user given an sa: id": await refusal(
                env.app, ga, grant_to("user", f"sa:{uuid.uuid4()}")
            ),
            "unknown principal type": await refusal(env.app, ga, grant_to("robot", "x")),
            "service principal given a local id": await refusal(
                env.app, ga, grant_to("service_principal", f"local:{uuid.uuid4()}")
            ),
            "member named twice": await refusal(
                env.app,
                ga,
                lambda uow: add_member(
                    uow, ga, group.id, username=member.username, principal_id=member.principal_id
                ),
            ),
            "member added again": await refusal(
                env.app,
                ga,
                lambda uow: add_member(
                    uow, ga, group.id, username=member.username, principal_id=None
                ),
            ),
        }
    assert {name: (e.status, e.slug) for name, e in answers.items()} == {
        "user given an sa: id": (422, "unknown-principal"),
        "unknown principal type": (422, "unknown-principal"),
        "service principal given a local id": (404, "not-found"),
        "member named twice": (422, "validation-error"),
        "member added again": (409, "conflict"),
    }
    assert count(sql, env, "grant.created") == 0
    assert count(sql, env, "group.member_added") == 1


async def test_a_folder_delete_rechecks_root_blockers_and_archived_ancestors_in_the_transaction(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    grant_sql: Callable[..., None],
    sql: Sql,
) -> None:
    env = make_identity()
    ga = principal("ga")
    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        _org, project, team = await folder_chain(client, token, root, "del")
        grant_sql(env.db_url, ga.id, "global_admin", root)

        def delete(folder_id: str, remote: RemoteFolderUsage) -> DomainCall:
            return lambda uow: delete_folder(uow, ga, uuid.UUID(folder_id), remote)

        root_refused = await refusal(env.app, ga, delete(root, RemoteFolderUsage()))
        # A contract that appeared after the route's own check still blocks the delete.
        busy = await refusal(env.app, ga, delete(team, RemoteFolderUsage(contracts=1)))
        sql(env.db_url, "UPDATE folder SET archived_at = now() WHERE id = %s", project)
        archived = await refusal(env.app, ga, delete(team, RemoteFolderUsage()))
    assert (root_refused.status, root_refused.slug) == (422, "validation-error")
    assert (busy.status, busy.slug) == (409, "folder-not-empty")
    assert busy.extensions["blockers"] == [{"kind": "contracts", "count": 1}]
    assert archived.status == 409
    assert sql(env.db_url, "SELECT id FROM folder WHERE id = %s", team) != []
    assert count(sql, env, "folder.deleted") == 0


async def test_create_pat_re_proves_its_owner_under_the_row_lock(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    sql: Sql,
) -> None:
    env = make_identity()
    caller = principal("caller")
    expires = datetime.now(UTC) + timedelta(days=30)

    def pat_for(principal_id: str) -> DomainCall:
        actor = principal("owner").model_copy(update={"id": principal_id})
        return lambda uow: create_pat(
            uow, actor, name="p", expires_at=expires, settings=env.settings
        )

    async with asgi_client(env.app) as client:
        token = await admin_token(client, env)
        live = await create_user(client, token, "live-owner")
        gone = await create_user(client, token, "gone-owner")
        sql(env.db_url, "UPDATE users SET is_active = false WHERE username = %s", gone.username)
        refused = {
            "inactive local owner": await refusal(env.app, caller, pat_for(gone.principal_id)),
            "unknown entra owner": await refusal(
                env.app, caller, pat_for(f"entra:{ENTRA_TENANT_ID}:{uuid.uuid4()}")
            ),
        }
        # Without a limit (no max_active given) the owner's live PATs are not counted.
        async with uow_for(env.app, caller) as uow:
            row, minted = await pat_for(live.principal_id)(uow)
    assert {name: (e.status, e.slug) for name, e in refused.items()} == {
        "inactive local owner": (401, "unauthenticated"),
        "unknown entra owner": (401, "unauthenticated"),
    }
    assert minted.startswith("dcp_")
    stored = sql(
        env.db_url, "SELECT local_user_id, name FROM api_credentials WHERE id = %s", row.id
    )
    assert stored == [
        {"local_user_id": uuid.UUID(live.principal_id.removeprefix("local:")), "name": "p"}
    ]
    assert count(sql, env, "credential.created") == 1


async def test_read_handlers_answer_404_for_a_row_gone_after_the_target_resolver(
    make_identity: Callable[..., Any], asgi_client: Any, admin_token: AdminFlow, sql: Sql
) -> None:
    """Each read handler loads its row again in its own session; a row deleted after the route's
    target resolver ran (or a registration of another kind) is the same 404 as an unknown id."""
    env = make_identity()
    gone = uuid.uuid4()
    async with asgi_client(env.app) as client:
        await admin_token(client, env)
        root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
        sql(
            env.db_url,
            "INSERT INTO entra_registrations (kind, tenant_id, object_id, owner_folder_id, "
            "display_name, created_by, updated_by) VALUES ('group', %s, %s, %s, 'g', 't', 't')",
            ENTRA_TENANT_ID,
            str(uuid.uuid4()),
            root,
        )
        group_registration = sql(env.db_url, "SELECT id FROM entra_registrations")[0]["id"]
        reads: dict[str, Callable[[Any], Awaitable[Any]]] = {
            "grant": lambda session: get_grant(gone, session),
            "group": lambda session: get_group(gone, session),
            "service account": lambda session: get_service_account(gone, session),
            "group registration": lambda session: get_group_registration(gone, session),
            "group read as a service principal": lambda session: get_service_principal_registration(
                group_registration, session
            ),
        }
        answers: dict[str, tuple[int, str]] = {}
        for name, read in reads.items():
            async with env.app.state.session_factory() as session:
                with pytest.raises(ProblemError) as caught:
                    await read(session)
            answers[name] = (caught.value.status, caught.value.slug)
    assert answers == dict.fromkeys(reads, (404, "not-found"))
