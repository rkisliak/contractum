"""The Phase 2 authorization model as frozen data (D-30, D-31, D-32).

Roles, the folder kinds they bind to, the permission registry, the role -> permission map and the
grant ceiling all live here as immutable mappings. No code branches on a role name: the policy
decision point, the grant service and the oracle all read these tables, so a change to the model is
one reviewable diff. Pure data and pure functions; no I/O and no imports from any service.

Role names are stored in ``role_assignments.role`` and appear in the public grant API, so they are
one-way once shipped (D-30).

``interactive_only`` (D-79) marks a permission a personal access token or service-account key may
never exercise, whatever its owner holds. Every permission that creates or revokes credentials,
changes grants, group memberships or registrations, creates users, or deactivates principals MUST
set it; ``tests/unit/test_roles_matrix.py`` pins the golden set so a new one cannot be forgotten.

Phase 5 adds rule sets and validation (D-113, D-115, D-127). A Policy Manager holds every
``ruleset:*`` permission and ``contract:validate`` at its folder and below, and still never reads
or writes contract content. Every role that holds ``folder:read`` also holds
``ruleset:read-published``; every contract reader holds ``contract:validate``.

Phase 6 adds rule impact (D-159, D-160): ``ruleset:dry-run`` (Policy Manager, Global admin) and
``governance:drift-read`` (Policy Manager, Org admin, Global admin), both read class.

Phase 7 adds the public audit query (D-166, D-167): ``audit:list`` (principal-level route gate)
and ``audit:query`` (folder-evaluated scope: Global admin, Global reader, Org admin, Project admin,
Team admin). The root ``audit:read`` stays on the internal read only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

FolderKind = Literal["root", "organization", "project", "team"]
ALL_KINDS: frozenset[str] = frozenset({"root", "organization", "project", "team"})


class Role(StrEnum):
    GLOBAL_ADMIN = "global_admin"
    GLOBAL_READER = "global_reader"
    ORG_ADMIN = "org_admin"
    ORG_READER = "org_reader"
    PROJECT_ADMIN = "project_admin"
    PROJECT_EDITOR = "project_editor"
    TEAM_ADMIN = "team_admin"
    TEAM_EDITOR = "team_editor"
    TEAM_READER = "team_reader"
    POLICY_MANAGER = "policy_manager"
    CONSUMER = "consumer"
    # D-105: the contracts delivery worker's platform identity. It holds mail:send and nothing else.
    NOTIFIER = "notifier"


# D-30: the folder kinds a role may be granted at.
ROLE_LEVELS: Mapping[Role, frozenset[str]] = MappingProxyType(
    {
        Role.GLOBAL_ADMIN: frozenset({"root"}),
        Role.GLOBAL_READER: frozenset({"root"}),
        Role.ORG_ADMIN: frozenset({"organization"}),
        Role.ORG_READER: frozenset({"organization"}),
        Role.PROJECT_ADMIN: frozenset({"project"}),
        Role.PROJECT_EDITOR: frozenset({"project"}),
        Role.TEAM_ADMIN: frozenset({"team"}),
        Role.TEAM_EDITOR: frozenset({"team"}),
        Role.TEAM_READER: frozenset({"team"}),
        Role.POLICY_MANAGER: ALL_KINDS,
        Role.CONSUMER: ALL_KINDS,
        Role.NOTIFIER: frozenset({"root"}),
    }
)

PermissionTarget = Literal["folder", "root", "principal", "special"]
"""How a permission is evaluated.

* ``folder``: held at a target folder (inherited down the tree, subject to block flags).
* ``root``: no route target; evaluated at the ROOT folder, a denial is FORBIDDEN.
* ``principal``: answered ALLOW for any non-restricted principal; results are trimmed by scope.
* ``special``: answered by dedicated branches of the decision point, not by the matrix.
"""


@dataclass(frozen=True)
class PermissionSpec:
    """One registered permission string.

    ``evaluated_at="parent"`` evaluates the permission at the target's parent folder (the target
    itself for ROOT). ``visible_via`` is the permission that, when held at the target folder
    itself, turns a denial into 403 instead of 404; ``None`` means a denial is always 403.
    ``access`` classifies the permission as ``read`` or ``write`` with no default, so a new
    permission cannot be registered unclassified; ``READ_PERMISSIONS`` is derived from it and a
    read-only personal access token holds only those (D-66).
    ``interactive_only`` permissions are refused for any principal that did not sign in with a JWT
    (via ``pat`` or ``api_key``), but only once the decision would otherwise be ALLOW (D-79).
    """

    name: str
    target: PermissionTarget
    access: Literal["read", "write"]
    evaluated_at: Literal["target", "parent"] = "target"
    visible_via: str | None = "folder:read"
    interactive_only: bool = False


def _spec(
    name: str,
    target: PermissionTarget,
    access: Literal["read", "write"],
    *,
    evaluated_at: Literal["target", "parent"] = "target",
    visible_via: str | None = "folder:read",
    interactive_only: bool = False,
) -> tuple[str, PermissionSpec]:
    return name, PermissionSpec(name, target, access, evaluated_at, visible_via, interactive_only)


_SPECS: tuple[tuple[str, PermissionSpec], ...] = (
    # special: password:rotate and authz:check keep their dedicated branches in the PDP.
    _spec("password:rotate", "special", "write", visible_via=None),
    _spec("authz:check", "special", "read", visible_via=None),
    # principal-level lists: ALLOW for any non-restricted principal, trimmed by scope.
    _spec("folder:list", "principal", "read", visible_via=None),
    _spec("contract:list", "principal", "read", visible_via=None),
    _spec("template:list", "principal", "read", visible_via=None),
    _spec("group:list", "principal", "read", visible_via=None),
    # D-68: a principal lists and manages its own personal access tokens.
    _spec("pat:list", "principal", "read", visible_via=None),
    _spec("pat:manage", "principal", "write", visible_via=None, interactive_only=True),
    # D-UI-08-19: any non-restricted principal asks who it is and what it may do (identity's
    # /me routes). Principal-level and read class, so a read-only PAT and a key hold it too.
    _spec("me:read", "principal", "read", visible_via=None),
    # Quick 261119-16f (UD-1): a local principal changes its own display name. Principal-level and
    # a write, so a read-only PAT is refused; interactive sign-in only (D-79), so a PAT or key gets
    # interactive-session-required. Entra and service-account principals are refused by the route.
    _spec("me:update", "principal", "write", visible_via=None, interactive_only=True),
    # Any non-restricted principal turns principal ids it already holds (created_by, published_by)
    # into display names: identity's POST /principals/names answers the display name only.
    _spec("principal:read-names", "principal", "read", visible_via=None),
    # D-68, D-79: the Global admin oversees every PAT (metadata only) and may revoke any, by JWT.
    _spec("pat:read-all", "root", "read", visible_via=None),
    _spec("pat:revoke-any", "root", "write", visible_via=None, interactive_only=True),
    # D-59: Entra registrations are listed trimmed by owner; read and manage are evaluated at the
    # owner folder (ROOT or an Organization), and a caller who cannot read there gets a 404.
    _spec("entra:list", "principal", "read", visible_via=None),
    # D-69: service accounts are owned like local groups. Listing is principal-level and trimmed
    # by owner; read, manage and manage-keys are evaluated at the owner folder (ROOT or an
    # Organization). Creating or renaming one and minting or revoking its keys manage a principal
    # and a credential, so they need an interactive sign-in (D-79).
    _spec("service-account:list", "principal", "read", visible_via=None),
    # D-82, D-104: subscriber cards. Reading and writing a card are principal-level gates: the
    # handler resolves the caller's capability on the card (folder reader, creator or admin, later
    # a member) and answers one uniform 404 to everyone else. A read-only PAT is refused on writes.
    _spec("card:read", "principal", "read", visible_via=None),
    _spec("card:write", "principal", "write", visible_via=None),
    # D-79: issuing or rotating a card's webhook signing secret is credential management, so it
    # needs an interactive JWT. No role holds it: it is evaluated like pat:manage (first used by
    # the webhook plan; declared here because this change owns the role-matrix edit).
    _spec("card:manage-secret", "principal", "write", visible_via=None, interactive_only=True),
    # D-166, D-167: the public audit query. audit:list is the route gate (ALLOW for any
    # non-restricted principal); the rows are trimmed to accessible_folders(audit:query).
    _spec("audit:list", "principal", "read", visible_via=None),
    # root-evaluated: no route target.
    _spec("user:create", "root", "write", visible_via=None, interactive_only=True),
    _spec("user:read", "root", "read", visible_via=None),
    # Quick 261119-16f (UD-2): only a Global admin changes a local user's display name, by
    # interactive sign-in (D-79).
    _spec("user:update", "root", "write", visible_via=None, interactive_only=True),
    # D-75, D-76: only a Global admin deactivates or reactivates a local user, by interactive
    # sign-in (D-79). Deactivation permanently revokes the user's refresh tokens and PATs.
    _spec("user:deactivate", "root", "write", visible_via=None, interactive_only=True),
    _spec("audit:read", "root", "read", visible_via=None),
    # D-105: sending mail through the mail service is a root write held by the notifier role and
    # the Global admin only. Not interactive-only: the notifier is a service-account key.
    _spec("mail:send", "root", "write", visible_via=None),
    # Quick 261120-s0p (item 6): the contracts notifier seeds the webhook allow-list once, from the
    # hosts of the channels that already exist. Not interactive-only: the notifier is a key.
    _spec("settings:seed-webhook-hosts", "root", "write", visible_via=None),
    # Quick 261120-0yc (D-03): platform settings. Reading and writing them is root-evaluated, so
    # only the Global admin holds them (Global reader and every other role must not); writing is
    # interactive sign-in only (D-79). settings:read-public (the banner and the Entra pre-fill
    # values) and settings:read-runtime (the non-secret runtime sections other services read from
    # identity's internal route) are principal-level reads for any non-restricted principal.
    _spec("settings:read", "root", "read", visible_via=None),
    _spec("settings:write", "root", "write", visible_via=None, interactive_only=True),
    _spec("settings:read-public", "principal", "read", visible_via=None),
    _spec("settings:read-runtime", "principal", "read", visible_via=None),
    # folder-evaluated.
    _spec("folder:read", "folder", "read"),
    _spec("folder:create", "folder", "write"),
    _spec("folder:update", "folder", "write"),
    _spec("folder:archive", "folder", "write"),
    # Quick 261119-0v1: deleting an empty folder; held by exactly the roles that hold archive.
    _spec("folder:delete", "folder", "write"),
    _spec("folder:unarchive", "folder", "write"),
    _spec("folder:set-block", "folder", "write", evaluated_at="parent"),
    _spec("grant:read", "folder", "read"),
    _spec("grant:manage", "folder", "write", interactive_only=True),
    _spec("group:create", "folder", "write"),
    _spec("group:read", "folder", "read", visible_via="group:read"),
    _spec("group:manage", "folder", "write", visible_via="group:read", interactive_only=True),
    _spec("entra:read", "folder", "read", visible_via="entra:read"),
    _spec("entra:manage", "folder", "write", visible_via="entra:read", interactive_only=True),
    _spec("service-account:create", "folder", "write", interactive_only=True),
    _spec("service-account:read", "folder", "read", visible_via="service-account:read"),
    _spec(
        "service-account:manage",
        "folder",
        "write",
        visible_via="service-account:read",
        interactive_only=True,
    ),
    _spec(
        "service-account:manage-keys",
        "folder",
        "write",
        visible_via="service-account:read",
        interactive_only=True,
    ),
    # D-75, D-76: the owner (Global admin, or the Org admin of an Organization-owned account)
    # deactivates or reactivates an account; deactivation permanently revokes its keys.
    _spec(
        "service-account:deactivate",
        "folder",
        "write",
        visible_via="service-account:read",
        interactive_only=True,
    ),
    _spec("contract:create", "folder", "write"),
    # D-82: a Consumer (or an admin above) creates a card in a team or project folder. card:manage
    # marks "is a folder admin" in Decision.permissions; it is deliberately NOT interactive-only
    # (A4), so a folder admin's PAT can still be recognised as a manager and edit a card.
    _spec("card:create", "folder", "write"),
    _spec("card:manage", "folder", "write"),
    # A denied write is 404 unless the caller can read the contract content: with the default
    # folder:read visibility a Consumer or Policy Manager would get 403 on a draft-only contract
    # and 404 on an unknown id, an existence oracle (D-08, RBAC-09).
    _spec("contract:update", "folder", "write", visible_via="contract:read"),
    _spec("contract:publish", "folder", "write", visible_via="contract:read"),
    # Soft delete (CONT-04, D-55): same visibility as the other contract writes, so a Consumer or
    # Policy Manager deleting gets the unknown-contract 404 and a Team reader a 403.
    _spec("contract:delete", "folder", "write", visible_via="contract:read"),
    # D-56: only Organization and Global admins restore, and the permission is its own visibility,
    # so for every other caller a deleted contract stays unknown (404).
    _spec("contract:restore", "folder", "write", visible_via="contract:restore"),
    # A denied read is always 404 (D-35, D-36): the read permission is its own visibility.
    _spec("contract:read", "folder", "read", visible_via="contract:read"),
    _spec("contract:read-metadata", "folder", "read", visible_via="contract:read-metadata"),
    _spec("contract:read-published", "folder", "read", visible_via="contract:read-published"),
    # Metadata of one contract by id for every role that can see contracts at all. A denied view is
    # always 404 (D-08): the handler narrows what each holder gets (Latest only for a published-only
    # caller) and answers the unknown-contract 404 for the rest (RBAC-07, RBAC-09).
    _spec("contract:view", "folder", "read", visible_via="contract:view"),
    _spec("template:read", "folder", "read", visible_via="template:read"),
    # Published template metadata and published version documents only, never drafts (A4).
    _spec("template:read-published", "folder", "read", visible_via="template:read-published"),
    _spec("template:create", "folder", "write"),
    _spec("template:update", "folder", "write", visible_via="template:read"),
    _spec("template:publish", "folder", "write", visible_via="template:read"),
    _spec("template:deprecate", "folder", "write", visible_via="template:read"),
    # Phase 5 (D-113, D-115, D-127): rule sets. Same shape as templates. Published rules are
    # readable by every role that reads the folder, so an editor can read the rule that blocks
    # them (research Pitfall 4); drafts need ruleset:read. contract:validate is a read-class dry
    # run, so a read-only PAT may validate. ruleset:mandate is deliberately not interactive-only
    # (research A5): a Policy Manager's pipeline may switch enforcement.
    _spec("ruleset:read", "folder", "read", visible_via="ruleset:read"),
    _spec("ruleset:read-published", "folder", "read", visible_via="ruleset:read-published"),
    _spec("ruleset:create", "folder", "write"),
    _spec("ruleset:update", "folder", "write", visible_via="ruleset:read"),
    _spec("ruleset:publish", "folder", "write", visible_via="ruleset:read"),
    _spec("ruleset:deprecate", "folder", "write", visible_via="ruleset:read"),
    _spec("ruleset:mandate", "folder", "write", visible_via="ruleset:read"),
    _spec("contract:validate", "folder", "read", visible_via="contract:validate"),
    # Phase 6 (D-159, D-160): rule impact. Both are read class (they change nothing and store
    # nothing), evaluated at a folder and never interactive-only, so a pipeline may run them.
    # A dry-run evaluates a rule-set version against contracts under the rule set's folder; it is
    # visible to whoever reads the rule set's drafts. The drift report is visible to folder readers.
    # Neither ever returns contract content (D-36).
    _spec("ruleset:dry-run", "folder", "read", visible_via="ruleset:read"),
    _spec("governance:drift-read", "folder", "read"),
    # Phase 7 (D-167): the scope of the public audit query. Never a route gate (a folder-target
    # check without a folder is FORBIDDEN): audit asks accessible_folders for it and an event is
    # visible when its leaf folder is in that scope. Events with an empty or [root] scope_path
    # are visible only to holders at ROOT. Org reader and Policy Manager do not hold it.
    _spec("audit:query", "folder", "read", visible_via=None),
)

PERMISSIONS: Mapping[str, PermissionSpec] = MappingProxyType(dict(_SPECS))

# D-66: what a read-only personal access token may hold. Derived from ``PermissionSpec.access``,
# never a hand-kept list.
READ_PERMISSIONS: frozenset[str] = frozenset(
    name for name, spec in PERMISSIONS.items() if spec.access == "read"
)

_CONTRACT_WRITE = frozenset(
    {"contract:create", "contract:update", "contract:publish", "contract:delete"}
)
_CONTRACT_READ = frozenset(
    {
        "contract:read",
        "contract:read-metadata",
        "contract:read-published",
        "contract:view",
        "contract:validate",
    }
)
_CONTRACT_ALL = _CONTRACT_WRITE | _CONTRACT_READ
# D-115: every role that reads a folder also reads the published rules there. One constant, used
# wherever "folder:read" is listed, so a role cannot get one without the other.
_FOLDER_READ = frozenset({"folder:read", "ruleset:read-published"})
_READER = _FOLDER_READ | _CONTRACT_READ
_RULESET_MANAGE = frozenset(
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

ROLE_PERMISSIONS: Mapping[Role, frozenset[str]] = MappingProxyType(
    {
        Role.GLOBAL_ADMIN: frozenset(
            name for name, spec in PERMISSIONS.items() if spec.target in ("folder", "root")
        ),
        # A4: Global reader sees published templates, not drafts.
        # D-167: Global reader also reads the audit trail of every folder it can see.
        Role.GLOBAL_READER: _READER | {"template:read-published", "audit:query"},
        Role.ORG_ADMIN: frozenset(
            {
                "folder:create",
                "folder:update",
                "folder:set-block",
                "grant:read",
                "grant:manage",
                "group:create",
                "group:read",
                "group:manage",
                "entra:read",
                "entra:manage",
                "service-account:create",
                "service-account:read",
                "service-account:manage",
                "service-account:manage-keys",
                "service-account:deactivate",
                "contract:restore",
                "card:create",
                "card:manage",
                # D-160: an Org admin reads the compliance drift of its Organization.
                "governance:drift-read",
                # D-167: admins read the audit trail of their subtree.
                "audit:query",
            }
        )
        | _FOLDER_READ
        | _CONTRACT_ALL,
        Role.ORG_READER: _READER,
        # OQ4: org and project admins may rename inside their subtree; slugs stay immutable.
        Role.PROJECT_ADMIN: frozenset(
            {
                "folder:create",
                "folder:update",
                "folder:set-block",
                "grant:read",
                "grant:manage",
                "card:create",
                "card:manage",
                "audit:query",
            }
        )
        | _FOLDER_READ
        | _CONTRACT_ALL,
        Role.PROJECT_EDITOR: _FOLDER_READ | _CONTRACT_ALL,
        Role.TEAM_ADMIN: frozenset(
            {
                "folder:update",
                "grant:read",
                "grant:manage",
                "card:create",
                "card:manage",
                "audit:query",
            }
        )
        | _FOLDER_READ
        | _CONTRACT_ALL,
        Role.TEAM_EDITOR: _FOLDER_READ | _CONTRACT_ALL,
        Role.TEAM_READER: _READER,
        # D-36, D-37, D-127: metadata, templates, rule sets and dry-run validation, never contract
        # content (no contract:read, update or publish).
        Role.POLICY_MANAGER: frozenset(
            {
                "contract:read-metadata",
                "contract:view",
                "contract:validate",
                "template:read",
                "template:read-published",
                "template:create",
                "template:update",
                "template:publish",
                "template:deprecate",
                # D-159, D-160: rule impact, before (dry-run) and after (drift) enforcement.
                "ruleset:dry-run",
                "governance:drift-read",
            }
        )
        | _FOLDER_READ
        | _RULESET_MANAGE,
        # RBAC-09: a consumer reads published versions only.
        # D-82: and a Consumer records what it depends on in subscriber cards.
        Role.CONSUMER: _FOLDER_READ | {"contract:read-published", "contract:view", "card:create"},
        # D-105: exactly mail:send, bound to ROOT, grantable by a Global admin only.
        # Quick 261120-s0p: and the one-time seed of the webhook allow-list.
        Role.NOTIFIER: frozenset({"mail:send", "settings:seed-webhook-hosts"}),
    }
)

# D-32 (also governs revoke and group membership changes): granter role -> roles it may grant.
# Nobody but a Global admin grants a Global role, and no admin grants its own role or above;
# A1: a Global admin may grant global_admin.
GRANTABLE: Mapping[Role, frozenset[Role]] = MappingProxyType(
    {
        Role.GLOBAL_ADMIN: frozenset(Role),
        Role.ORG_ADMIN: frozenset(
            {
                Role.ORG_READER,
                Role.PROJECT_ADMIN,
                Role.PROJECT_EDITOR,
                Role.TEAM_ADMIN,
                Role.TEAM_EDITOR,
                Role.TEAM_READER,
                Role.POLICY_MANAGER,
                Role.CONSUMER,
            }
        ),
        Role.PROJECT_ADMIN: frozenset(
            {
                Role.PROJECT_EDITOR,
                Role.TEAM_ADMIN,
                Role.TEAM_EDITOR,
                Role.TEAM_READER,
                Role.POLICY_MANAGER,
                Role.CONSUMER,
            }
        ),
        Role.TEAM_ADMIN: frozenset(
            {Role.TEAM_EDITOR, Role.TEAM_READER, Role.POLICY_MANAGER, Role.CONSUMER}
        ),
        Role.GLOBAL_READER: frozenset(),
        Role.ORG_READER: frozenset(),
        Role.PROJECT_EDITOR: frozenset(),
        Role.TEAM_EDITOR: frozenset(),
        Role.TEAM_READER: frozenset(),
        Role.POLICY_MANAGER: frozenset(),
        Role.CONSUMER: frozenset(),
        Role.NOTIFIER: frozenset(),
    }
)


def _known(roles: Iterable[str]) -> list[Role]:
    known: list[Role] = []
    for name in roles:
        try:
            known.append(Role(name))
        except ValueError:
            continue
    return known


def roles_with(permission: str) -> frozenset[str]:
    """Role values whose permission set contains ``permission``."""
    return frozenset(str(role) for role, held in ROLE_PERMISSIONS.items() if permission in held)


def permissions_of(roles: Iterable[str]) -> frozenset[str]:
    """Union of the permissions of the given roles; unknown role strings contribute nothing."""
    held: set[str] = set()
    for role in _known(roles):
        held |= ROLE_PERMISSIONS[role]
    return frozenset(held)


def can_grant(actor_roles: Iterable[str], role: str) -> bool:
    """True when any of the actor's known roles has ``role`` under its D-32 ceiling."""
    try:
        target = Role(role)
    except ValueError:
        return False
    return any(target in GRANTABLE[actor] for actor in _known(actor_roles))


def is_bound_to(role: str, kind: str) -> bool:
    """True when ``role`` may be granted at a folder of ``kind`` (D-30)."""
    try:
        return kind in ROLE_LEVELS[Role(role)]
    except ValueError:
        return False
