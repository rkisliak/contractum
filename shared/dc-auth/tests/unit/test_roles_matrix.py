"""The Phase 2 role matrix as data (D-30, D-31, D-32, A1, A4, RBAC-03..09)."""

from __future__ import annotations

import itertools

import pytest
from dc_auth.roles import (
    ALL_KINDS,
    GRANTABLE,
    PERMISSIONS,
    ROLE_LEVELS,
    ROLE_PERMISSIONS,
    Role,
    can_grant,
    is_bound_to,
    permissions_of,
    roles_with,
)

pytestmark = pytest.mark.unit


def _contract(roles: list[str]) -> frozenset[str]:
    return frozenset(p for p in permissions_of(roles) if p.startswith("contract:"))


def test_there_are_exactly_twelve_roles_with_the_decided_names() -> None:
    assert {r.value for r in Role} == {
        "global_admin",
        "global_reader",
        "org_admin",
        "org_reader",
        "project_admin",
        "project_editor",
        "team_admin",
        "team_editor",
        "team_reader",
        "policy_manager",
        "consumer",
        "notifier",
    }


def test_every_role_has_a_nonempty_level_binding() -> None:
    assert set(ROLE_LEVELS) == set(Role)
    assert all(ROLE_LEVELS[role] for role in Role)


def test_levels_follow_the_role_prefix() -> None:
    for role in Role:
        if role.startswith("global_"):
            assert ROLE_LEVELS[role] == {"root"}
        elif role.startswith("org_"):
            assert ROLE_LEVELS[role] == {"organization"}
        elif role.startswith("project_"):
            assert ROLE_LEVELS[role] == {"project"}
        elif role.startswith("team_"):
            assert ROLE_LEVELS[role] == {"team"}
    assert ROLE_LEVELS[Role.POLICY_MANAGER] == ALL_KINDS
    assert ROLE_LEVELS[Role.CONSUMER] == ALL_KINDS
    assert ROLE_LEVELS[Role.NOTIFIER] == {"root"}


def test_is_bound_to_agrees_with_levels_for_all_48_pairs() -> None:
    pairs = list(itertools.product(Role, sorted(ALL_KINDS)))
    assert len(pairs) == 48
    for role, kind in pairs:
        assert is_bound_to(role, kind) == (kind in ROLE_LEVELS[role])
    assert not is_bound_to("not_a_role", "root")


@pytest.mark.parametrize(
    ("admin", "editor", "reader"),
    [
        (Role.TEAM_ADMIN, Role.TEAM_EDITOR, Role.TEAM_READER),
        (Role.PROJECT_ADMIN, Role.PROJECT_EDITOR, None),
        (Role.ORG_ADMIN, None, Role.ORG_READER),
    ],
)
def test_admin_is_a_superset_of_editor_is_a_superset_of_reader_for_contracts(
    admin: Role, editor: Role | None, reader: Role | None
) -> None:
    chain = [r for r in (admin, editor, reader) if r is not None]
    for higher, lower in itertools.pairwise(chain):
        assert _contract([higher]) >= _contract([lower])


def test_policy_manager_reads_metadata_and_manages_templates_but_never_content() -> None:
    held = permissions_of([Role.POLICY_MANAGER])
    assert "contract:read-metadata" in held
    template_folder_perms = {
        name
        for name, spec in PERMISSIONS.items()
        if name.startswith("template:") and spec.target == "folder"
    }
    assert template_folder_perms <= held
    for denied in (
        "contract:read",
        "contract:create",
        "contract:update",
        "contract:publish",
        "contract:delete",
        "contract:restore",
    ):
        assert denied not in held


RULESET_PERMISSIONS = frozenset(
    {
        "ruleset:read",
        "ruleset:read-published",
        "ruleset:create",
        "ruleset:update",
        "ruleset:publish",
        "ruleset:deprecate",
        "ruleset:mandate",
    }
)


def test_the_phase_5_permissions_are_registered_with_their_shape() -> None:
    expected = {
        "ruleset:read": ("folder", "read", "ruleset:read"),
        "ruleset:read-published": ("folder", "read", "ruleset:read-published"),
        "ruleset:create": ("folder", "write", "folder:read"),
        "ruleset:update": ("folder", "write", "ruleset:read"),
        "ruleset:publish": ("folder", "write", "ruleset:read"),
        "ruleset:deprecate": ("folder", "write", "ruleset:read"),
        "ruleset:mandate": ("folder", "write", "ruleset:read"),
        "contract:validate": ("folder", "read", "contract:validate"),
    }
    for name, (target, access, visible_via) in expected.items():
        spec = PERMISSIONS[name]
        assert (spec.target, spec.access, spec.visible_via) == (target, access, visible_via), name
        assert spec.interactive_only is False, name
        assert spec.evaluated_at == "target", name


def test_policy_manager_manages_rule_sets_and_validates_but_never_touches_content() -> None:
    held = permissions_of([Role.POLICY_MANAGER])
    assert RULESET_PERMISSIONS <= held
    assert "contract:validate" in held
    for denied in ("contract:read", "contract:update", "contract:publish", "contract:delete"):
        assert denied not in held


def test_global_admin_holds_every_phase_5_permission() -> None:
    held = permissions_of([Role.GLOBAL_ADMIN])
    assert RULESET_PERMISSIONS | {"contract:validate"} <= held


@pytest.mark.parametrize("role", list(Role))
def test_folder_read_goes_with_ruleset_read_published(role: Role) -> None:
    held = ROLE_PERMISSIONS[role]
    assert ("folder:read" in held) == ("ruleset:read-published" in held)
    if role is Role.NOTIFIER:
        assert "folder:read" not in held


@pytest.mark.parametrize("role", list(Role))
def test_contract_validate_is_held_iff_contract_read_or_policy_manager(role: Role) -> None:
    held = ROLE_PERMISSIONS[role]
    assert ("contract:validate" in held) == ("contract:read" in held or role is Role.POLICY_MANAGER)
    if role in (Role.CONSUMER, Role.NOTIFIER):
        assert "contract:validate" not in held


def test_only_the_policy_manager_and_admin_hold_ruleset_writes() -> None:
    writes = RULESET_PERMISSIONS - {"ruleset:read", "ruleset:read-published"}
    for name in writes:
        assert roles_with(name) == {"policy_manager", "global_admin"}, name


def test_consumer_reads_published_versions_only() -> None:
    # Phase 3 (A15 revised): a Consumer also holds contract:view so it can view, by id, a contract
    # it can list; the handler then serves the Latest only.
    assert permissions_of([Role.CONSUMER]) == {
        "folder:read",
        "ruleset:read-published",
        "contract:read-published",
        "contract:view",
        "card:create",
    }
    for role in Role:
        if role is not Role.CONSUMER:
            continue
        for denied in (
            "contract:read",
            "contract:read-metadata",
            "contract:create",
            "contract:update",
            "contract:publish",
            "contract:delete",
            "contract:restore",
        ):
            assert denied not in ROLE_PERMISSIONS[role]


def test_contract_view_is_its_own_visibility_so_a_denied_view_is_always_404() -> None:
    assert PERMISSIONS["contract:view"].visible_via == "contract:view"


@pytest.mark.parametrize("role", list(Role))
def test_contract_view_is_held_iff_the_role_sees_contracts(role: Role) -> None:
    held = ROLE_PERMISSIONS[role]
    assert ("contract:view" in held) == (
        "contract:read-metadata" in held or "contract:read-published" in held
    )


def test_consumer_is_only_a_grantable_role_name_no_role_implies_it() -> None:
    granters = {role for role in Role if Role.CONSUMER in GRANTABLE[role]}
    assert granters == {
        Role.GLOBAL_ADMIN,
        Role.ORG_ADMIN,
        Role.PROJECT_ADMIN,
        Role.TEAM_ADMIN,
    }
    # No other role carries the consumer permission set implicitly beyond what read roles hold.
    assert roles_with("contract:read-published") >= {"consumer"}


def test_a4_global_reader_reads_published_templates_not_drafts() -> None:
    reader = permissions_of([Role.GLOBAL_READER])
    assert "template:read-published" in reader
    assert "template:read" not in reader
    assert {p for p in reader if p.startswith("template:")} == {"template:read-published"}
    for role in (Role.ORG_READER, Role.TEAM_READER):
        assert not {p for p in permissions_of([role]) if p.startswith("template:")}
    for role in (Role.POLICY_MANAGER, Role.GLOBAL_ADMIN):
        held = permissions_of([role])
        assert {"template:read", "template:read-published"} <= held


def test_the_matrix_only_names_registered_permissions() -> None:
    for role, held in ROLE_PERMISSIONS.items():
        assert held <= set(PERMISSIONS), role


def test_every_folder_permission_is_held_by_some_role_and_admin_holds_all() -> None:
    for name, spec in PERMISSIONS.items():
        if spec.target == "folder":
            assert roles_with(name), name
    admin = ROLE_PERMISSIONS[Role.GLOBAL_ADMIN]
    assert admin == {n for n, s in PERMISSIONS.items() if s.target in ("folder", "root")}


def test_permission_registry_shape() -> None:
    assert {n for n, s in PERMISSIONS.items() if s.target == "special"} == {
        "password:rotate",
        "authz:check",
    }
    assert {n for n, s in PERMISSIONS.items() if s.target == "principal"} == {
        "folder:list",
        "contract:list",
        "template:list",
        "group:list",
        "pat:list",
        "pat:manage",
        "entra:list",
        "service-account:list",
        "card:read",
        "card:write",
        "card:manage-secret",
        "audit:list",
        "me:read",
        "me:update",
        "principal:read-names",
        "settings:read-public",
        "settings:read-runtime",
    }
    assert {n for n, s in PERMISSIONS.items() if s.target == "root"} == {
        "user:create",
        "user:read",
        "user:update",
        "user:deactivate",
        "audit:read",
        "mail:send",
        "settings:seed-webhook-hosts",
        "pat:read-all",
        "pat:revoke-any",
        "settings:read",
        "settings:write",
    }
    assert PERMISSIONS["folder:set-block"].evaluated_at == "parent"
    for name in ("contract:read", "contract:read-metadata", "template:read"):
        assert PERMISSIONS[name].visible_via == name
    assert all(s.visible_via is None for s in PERMISSIONS.values() if s.target != "folder")


def test_grant_ceiling_never_includes_own_admin_role_or_global_roles() -> None:
    for admin in (Role.ORG_ADMIN, Role.PROJECT_ADMIN, Role.TEAM_ADMIN):
        assert admin not in GRANTABLE[admin]
        assert not {r for r in GRANTABLE[admin] if r.startswith("global_")}
    assert Role.ORG_ADMIN not in GRANTABLE[Role.PROJECT_ADMIN]
    assert Role.PROJECT_ADMIN not in GRANTABLE[Role.TEAM_ADMIN]


def test_only_global_admin_grants_org_admin_and_global_roles() -> None:
    for role in Role:
        grants_org_admin = Role.ORG_ADMIN in GRANTABLE[role]
        assert grants_org_admin == (role is Role.GLOBAL_ADMIN)
        if role is not Role.GLOBAL_ADMIN:
            assert not {r for r in GRANTABLE[role] if r.startswith("global_")}


def test_can_grant_examples() -> None:
    assert can_grant(["team_admin"], "team_editor")
    assert not can_grant(["team_admin"], "team_admin")
    assert can_grant(["global_admin"], "global_admin")  # A1
    assert not can_grant(["policy_manager"], "consumer")
    assert not can_grant(["global_admin"], "not_a_role")
    assert not can_grant(["not_a_role"], "consumer")
    assert can_grant(["team_reader", "org_admin"], "project_admin")


def test_unknown_roles_contribute_nothing() -> None:
    assert permissions_of(["nonsense"]) == frozenset()
    assert permissions_of([]) == frozenset()
    assert permissions_of(["nonsense", "consumer"]) == permissions_of(["consumer"])


def test_the_data_is_immutable_at_runtime() -> None:
    for mapping in (PERMISSIONS, ROLE_PERMISSIONS, ROLE_LEVELS, GRANTABLE):
        with pytest.raises(TypeError):
            mapping["x"] = frozenset()  # type: ignore[index]


def test_org_and_project_admins_rename_in_their_subtree_oq4() -> None:
    for role in (Role.GLOBAL_ADMIN, Role.ORG_ADMIN, Role.PROJECT_ADMIN, Role.TEAM_ADMIN):
        assert "folder:update" in ROLE_PERMISSIONS[role]
    assert "folder:create" not in ROLE_PERMISSIONS[Role.TEAM_ADMIN]
    assert roles_with("folder:archive") == {"global_admin"}
    assert roles_with("folder:delete") == {"global_admin"}


def test_contract_writes_are_visible_only_through_contract_read() -> None:
    # A Consumer or Policy Manager must not tell a draft-only contract from an unknown id by the
    # status of a write (403 versus 404): the PDP answers 403 only when visible_via is held.
    assert PERMISSIONS["contract:update"].visible_via == "contract:read"
    assert PERMISSIONS["contract:publish"].visible_via == "contract:read"
    assert "contract:read" not in permissions_of([Role.CONSUMER])
    for role in (Role.CONSUMER, Role.POLICY_MANAGER):
        assert "contract:read" not in ROLE_PERMISSIONS[role]


def test_contract_delete_is_held_by_editors_and_admins_and_visible_via_contract_read() -> None:
    # CONT-04, D-55: an editor removes a contract; a readers-only or metadata-only role never does.
    assert PERMISSIONS["contract:delete"].visible_via == "contract:read"
    assert roles_with("contract:delete") == {
        "team_editor",
        "team_admin",
        "project_editor",
        "project_admin",
        "org_admin",
        "global_admin",
    }
    assert "contract:delete" not in permissions_of([Role.CONSUMER])


def test_contract_restore_is_held_by_org_and_global_admin_only_and_is_its_own_visibility() -> None:
    # D-56: restoring a deleted contract is an Organization or Global admin act; for everyone else
    # the contract stays unknown (404), because restore is its own visibility.
    assert PERMISSIONS["contract:restore"].visible_via == "contract:restore"
    assert roles_with("contract:restore") == {"org_admin", "global_admin"}
    assert permissions_of([Role.CONSUMER]) == {
        "folder:read",
        "ruleset:read-published",
        "contract:read-published",
        "contract:view",
        "card:create",
    }


def test_pat_permissions_are_principal_level_and_held_through_no_role() -> None:
    # D-68: any non-restricted principal manages its own PATs; no role grants or withholds them.
    for name in ("pat:list", "pat:manage"):
        assert PERMISSIONS[name].target == "principal"
        assert PERMISSIONS[name].visible_via is None
        assert not roles_with(name)


def test_entra_registration_permissions_belong_to_global_and_org_admins_only() -> None:
    # D-59: ROOT or an Organization owns an Entra registration; its admins read and manage it, and
    # a denied read or write is a 404 for everyone who cannot read the registration at its owner.
    assert PERMISSIONS["entra:list"].target == "principal"
    assert PERMISSIONS["entra:list"].visible_via is None
    assert not roles_with("entra:list")
    for name in ("entra:read", "entra:manage"):
        assert PERMISSIONS[name].target == "folder"
        assert PERMISSIONS[name].visible_via == "entra:read"
    assert roles_with("entra:manage") == {"global_admin", "org_admin"}
    assert roles_with("entra:read") == {"global_admin", "org_admin"}


# --- D-79: permissions a PAT or key may never exercise -------------------------------------------

INTERACTIVE_ONLY_GOLDEN = frozenset(
    {
        "pat:manage",
        "pat:revoke-any",
        "grant:manage",
        "group:manage",
        "entra:manage",
        "user:create",
        "user:update",
        "user:deactivate",
        # Quick 261120-0yc: changing platform settings is interactive only.
        "settings:write",
        # Quick 261119-16f: a local principal changes its own display name, interactive only.
        "me:update",
        "service-account:create",
        "service-account:manage",
        "service-account:manage-keys",
        "service-account:deactivate",
        # D-79: issuing or rotating a card's webhook signing secret is credential management.
        "card:manage-secret",
    }
)


def test_interactive_only_defaults_to_false_on_a_new_spec() -> None:
    from dc_auth.roles import PermissionSpec

    assert PermissionSpec("x:y", "folder", "read").interactive_only is False
    assert (
        PermissionSpec("x:y", "folder", "write", "target", "folder:read").interactive_only is False
    )


def test_the_interactive_only_permissions_are_exactly_the_golden_set() -> None:
    # Every later permission that creates or revokes credentials, changes grants, memberships or
    # registrations, or deactivates principals must set interactive_only and extend this set.
    assert {
        n for n, spec in PERMISSIONS.items() if spec.interactive_only
    } == INTERACTIVE_ONLY_GOLDEN


def test_read_and_list_permissions_are_never_interactive_only() -> None:
    for name in (
        "pat:list",
        "grant:read",
        "group:read",
        "entra:read",
        "user:read",
        "folder:read",
        "service-account:list",
        "service-account:read",
    ):
        assert PERMISSIONS[name].interactive_only is False, name


# --- D-66: every permission is classified read or write ----------------------------------------

READ_GOLDEN = frozenset(
    {
        "authz:check",
        "folder:list",
        "contract:list",
        "template:list",
        "group:list",
        "entra:list",
        "pat:list",
        "user:read",
        "audit:read",
        "pat:read-all",
        "folder:read",
        "grant:read",
        "group:read",
        "entra:read",
        "contract:read",
        "contract:read-metadata",
        "contract:read-published",
        "contract:view",
        "template:read",
        "template:read-published",
        "service-account:list",
        "service-account:read",
        "card:read",
        "ruleset:read",
        "ruleset:read-published",
        "contract:validate",
        "ruleset:dry-run",
        "governance:drift-read",
        "audit:list",
        "audit:query",
        "me:read",
        "principal:read-names",
        "settings:read",
        "settings:read-public",
        "settings:read-runtime",
    }
)


def _read_permissions() -> frozenset[str] | None:
    from dc_auth import roles

    return getattr(roles, "READ_PERMISSIONS", None)


def test_every_permission_is_classified_read_or_write() -> None:
    assert all(getattr(spec, "access", None) in ("read", "write") for spec in PERMISSIONS.values())


def test_read_permissions_are_exactly_the_golden_set() -> None:
    # A new permission must declare its access; a new read must extend this set on purpose.
    assert len(READ_GOLDEN) == 35
    assert _read_permissions() == READ_GOLDEN
    assert READ_GOLDEN == {n for n, s in PERMISSIONS.items() if getattr(s, "access", "") == "read"}


def test_a_permission_spec_cannot_be_built_without_access() -> None:
    from dc_auth.roles import PermissionSpec

    with pytest.raises(TypeError):
        PermissionSpec("x:y", "folder")  # type: ignore[call-arg]


def test_user_deactivate_is_a_root_write_held_by_the_global_admin_only() -> None:
    spec = PERMISSIONS["user:deactivate"]
    assert (spec.target, spec.access, spec.visible_via) == ("root", "write", None)
    assert spec.interactive_only is True
    assert roles_with("user:deactivate") == {"global_admin"}


def test_pat_oversight_permissions_are_root_level_and_held_by_the_global_admin_only() -> None:
    for name in ("pat:read-all", "pat:revoke-any"):
        assert PERMISSIONS[name].target == "root"
        assert PERMISSIONS[name].visible_via is None
    assert roles_with("pat:read-all") == {"global_admin"}
    assert roles_with("pat:revoke-any") == {"global_admin"}
    assert PERMISSIONS["pat:read-all"].interactive_only is False
    assert PERMISSIONS["pat:revoke-any"].interactive_only is True


# --- D-69: service accounts are owned like groups and managed by Org and Global admins -----------


def test_service_account_permissions_are_declared_with_their_visibility() -> None:
    assert PERMISSIONS["service-account:list"].target == "principal"
    assert PERMISSIONS["service-account:create"].target == "folder"
    for name in ("service-account:read", "service-account:manage", "service-account:manage-keys"):
        assert PERMISSIONS[name].target == "folder"
        assert PERMISSIONS[name].visible_via == "service-account:read", name
    assert not roles_with("service-account:list")  # principal-level: never held through a role


def test_only_org_and_global_admins_hold_the_service_account_permissions() -> None:
    for name in (
        "service-account:create",
        "service-account:read",
        "service-account:manage",
        "service-account:manage-keys",
    ):
        assert roles_with(name) == {"global_admin", "org_admin"}, name


def test_service_account_deactivate_is_an_interactive_folder_write_held_by_the_org_admin() -> None:
    spec = PERMISSIONS["service-account:deactivate"]
    assert spec.target == "folder" and spec.access == "write"
    assert spec.visible_via == "service-account:read" and spec.interactive_only is True
    assert "service-account:deactivate" in ROLE_PERMISSIONS[Role.ORG_ADMIN]
    assert "service-account:deactivate" in ROLE_PERMISSIONS[Role.GLOBAL_ADMIN]
    holders = {r for r, perms in ROLE_PERMISSIONS.items() if "service-account:deactivate" in perms}
    assert holders == {Role.GLOBAL_ADMIN, Role.ORG_ADMIN}


# --- D-82, D-79: subscriber cards --------------------------------------------------


def test_card_permissions_are_declared_with_their_holders() -> None:
    assert roles_with("card:create") == {
        "consumer",
        "team_admin",
        "project_admin",
        "org_admin",
        "global_admin",
    }
    assert roles_with("card:manage") == {"team_admin", "project_admin", "org_admin", "global_admin"}
    assert roles_with("card:manage-secret") == frozenset()
    for name in ("card:create", "card:manage"):
        spec = PERMISSIONS[name]
        assert (spec.target, spec.access, spec.visible_via) == ("folder", "write", "folder:read")
        assert spec.interactive_only is False, name  # A4: a PAT may still detect admin status
    for name, access in (("card:read", "read"), ("card:write", "write")):
        spec = PERMISSIONS[name]
        assert (spec.target, spec.access, spec.visible_via) == ("principal", access, None)
        assert not roles_with(name)
    secret = PERMISSIONS["card:manage-secret"]
    assert (secret.target, secret.access, secret.visible_via) == ("principal", "write", None)
    assert secret.interactive_only is True
    for role in (Role.TEAM_EDITOR, Role.POLICY_MANAGER):
        assert not {p for p in permissions_of([role]) if p.startswith("card:")}


# --- D-105: the notifier role and mail:send ------------------------------------------------------


def test_mail_send_is_held_by_the_notifier_and_the_global_admin_only() -> None:
    assert roles_with("mail:send") == {"global_admin", "notifier"}
    # Quick 261120-s0p: plus the one-time seed of the webhook allow-list.
    assert ROLE_PERMISSIONS[Role.NOTIFIER] == {"mail:send", "settings:seed-webhook-hosts"}
    assert permissions_of([Role.NOTIFIER]) == {"mail:send", "settings:seed-webhook-hosts"}
    assert roles_with("settings:seed-webhook-hosts") == {"global_admin", "notifier"}


def test_mail_send_is_a_root_write_that_is_neither_interactive_only_nor_a_read() -> None:
    spec = PERMISSIONS["mail:send"]
    assert (spec.target, spec.access, spec.visible_via) == ("root", "write", None)
    assert spec.interactive_only is False
    assert "mail:send" not in READ_GOLDEN
    assert "mail:send" not in INTERACTIVE_ONLY_GOLDEN


def test_the_notifier_is_bound_to_root_only_and_only_a_global_admin_grants_it() -> None:
    assert ROLE_LEVELS[Role.NOTIFIER] == {"root"}
    assert is_bound_to("notifier", "root")
    for kind in ("organization", "project", "team"):
        assert not is_bound_to("notifier", kind)
    assert {role for role in Role if Role.NOTIFIER in GRANTABLE[role]} == {Role.GLOBAL_ADMIN}
    assert GRANTABLE[Role.NOTIFIER] == frozenset()
    assert can_grant(["global_admin"], "notifier")
    for role in Role:
        if role is not Role.GLOBAL_ADMIN:
            assert not can_grant([role], "notifier")


# --- D-159, D-160: rule impact (dry-run and compliance drift) ------------------------------------


def test_the_rule_impact_permissions_are_folder_reads_with_their_visibility() -> None:
    expected = {
        "ruleset:dry-run": ("folder", "read", "ruleset:read"),
        "governance:drift-read": ("folder", "read", "folder:read"),
    }
    for name, (target, access, visible_via) in expected.items():
        spec = PERMISSIONS[name]
        assert (spec.target, spec.access, spec.visible_via) == (target, access, visible_via), name
        assert spec.interactive_only is False, name
        assert spec.evaluated_at == "target", name


def test_only_the_policy_manager_and_the_global_admin_dry_run_a_rule_set() -> None:
    assert roles_with("ruleset:dry-run") == {"policy_manager", "global_admin"}


def test_policy_managers_org_admins_and_global_admins_read_compliance_drift() -> None:
    assert roles_with("governance:drift-read") == {"policy_manager", "org_admin", "global_admin"}
    assert "ruleset:dry-run" not in ROLE_PERMISSIONS[Role.ORG_ADMIN]


@pytest.mark.parametrize(
    "role",
    [
        Role.CONSUMER,
        Role.TEAM_READER,
        Role.TEAM_EDITOR,
        Role.TEAM_ADMIN,
        Role.PROJECT_EDITOR,
        Role.PROJECT_ADMIN,
        Role.ORG_READER,
        Role.GLOBAL_READER,
        Role.NOTIFIER,
    ],
)
def test_consumers_readers_and_editors_hold_no_rule_impact_permission(role: Role) -> None:
    held = ROLE_PERMISSIONS[role]
    assert "ruleset:dry-run" not in held
    assert "governance:drift-read" not in held


def test_rule_impact_gives_the_policy_manager_no_contract_content() -> None:
    held = permissions_of([Role.POLICY_MANAGER])
    assert {"ruleset:dry-run", "governance:drift-read"} <= held
    assert "contract:read" not in held


# --- D-166, D-167: the public audit query -------------------------------------------------------


def test_audit_list_is_a_principal_level_read_held_through_no_role() -> None:
    spec = PERMISSIONS["audit:list"]
    assert (spec.target, spec.access, spec.visible_via) == ("principal", "read", None)
    assert spec.interactive_only is False  # a PAT or service-account key may query (API-04)
    assert not roles_with("audit:list")


def test_audit_query_is_a_folder_read_held_by_exactly_the_d167_roles() -> None:
    spec = PERMISSIONS["audit:query"]
    assert (spec.target, spec.access, spec.visible_via) == ("folder", "read", None)
    assert spec.interactive_only is False
    assert spec.evaluated_at == "target"
    assert roles_with("audit:query") == {
        "global_admin",
        "global_reader",
        "org_admin",
        "project_admin",
        "team_admin",
    }
    for role in (Role.ORG_READER, Role.POLICY_MANAGER, Role.CONSUMER, Role.TEAM_READER):
        assert "audit:query" not in ROLE_PERMISSIONS[role]


def test_the_root_audit_read_stays_with_the_global_admin_only() -> None:
    assert PERMISSIONS["audit:read"].target == "root"
    assert roles_with("audit:read") == {"global_admin"}


def test_only_the_global_admin_holds_the_root_settings_permissions() -> None:
    """Quick 261120-0yc (D-03): Global reader and every other role never hold settings:*."""
    for permission in ("settings:read", "settings:write"):
        holders = {role for role, perms in ROLE_PERMISSIONS.items() if permission in perms}
        assert holders == {Role.GLOBAL_ADMIN}, permission
