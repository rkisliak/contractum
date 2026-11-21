"""LocalAuthorizer: the policy decision point behind the ``Authorizer`` interface (D-31).

D-05, D-08, D-31, D-35 (which replaces the interim D-22). Authority comes only from identity's
``role_assignments`` and ``group_members`` tables. Token claims (roles, groups, scope strings
minted by any issuer) are never consulted, except that a restricted bootstrap session holds no
authority at all.

* ``password:rotate``: a local user signed in with a JWT only (a PAT carries its owner's id but is
  not a sign-in, D-79). ``authz:check``: every non-restricted principal.
* Any other string must be a key of ``dc_auth.roles.PERMISSIONS``; an unknown one is FORBIDDEN,
  even for the global admin (fail closed).
* ``folder`` permissions: the roles a principal holds at the target (own grants, local groups and
  the Entra seam, inherited down the tree and cut by block flags, see ``dc_identity.access``) are
  mapped through ``ROLE_PERMISSIONS``. Held means ALLOW with the folder facts and the permission
  set held at the target. Not held is FORBIDDEN when the permission's ``visible_via`` is held at
  the target, otherwise NOT_FOUND, so nothing leaks about folders the caller cannot see (D-35).
  ``folder:set-block`` is evaluated at the parent folder.
* ``root`` permissions (no target) are evaluated at ROOT; a denial is FORBIDDEN.
* ``principal`` (list) permissions are ALLOW for every non-restricted principal: the list handler
  trims its rows with ``accessible_folders`` / ``visible_folders_subquery`` and a caller with no
  access gets an empty page, never a 403 (D-35).

* A read-only personal access token (D-66) holds only ``READ_PERMISSIONS`` (derived from
  ``PermissionSpec.access``): every held set below is intersected with it, a principal-level write
  is FORBIDDEN and ``accessible_folders`` of a write permission is empty. The narrowing happens
  inside the decision, before the interactive-only refusal, so a write is never ALLOW.

* ``interactive_only`` permissions (D-79) are refused for a principal that is not a person signed in
  with a JWT (``is_interactive``: a PAT, a key and an Entra service principal's app token), but
  only on a decision that would otherwise be ALLOW: NOT_FOUND and FORBIDDEN pass through unchanged,
  so the visibility rule always runs first and a refusal never reveals an invisible folder.

Every check is live: there is no decision cache (a revoked grant must stop working at once).

* Every outcome that is not ALLOW is recorded by ``dc_identity.denials.DenialRecorder`` as one
  ``authz`` / ``check.denied`` outbox row that commits before the decision is returned or the
  refusal raised (D-172): ``not_found``, ``forbidden``, ``gate_refused`` (the gate's 401 and 403)
  and ``interactive_required`` (D-79). A gate 503 is unavailability and is not recorded. A row
  that cannot be written turns the check into 503 (fail closed). The recorder opens its session
  only after ``_decide``'s session has closed, so a check never holds two pooled connections.
  ``accessible_folders`` records nothing: trimming a scope is not a denial.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from dc_auth.authorizer import Decision, FolderFacts, FolderScope, Outcome
from dc_auth.authz_wire import CheckContext
from dc_auth.principal import Principal
from dc_auth.roles import PERMISSIONS, READ_PERMISSIONS, Role, permissions_of, roles_with
from dc_core.problems import ProblemError
from sqlalchemy import Text, func, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.orm import aliased

from dc_identity.access import accessible_scope, effective_roles, grantees_for, root_id
from dc_identity.denials import DEFAULT_CONTEXT, DenialOutcome, DenialRecorder
from dc_identity.directory import UnconfiguredDirectory
from dc_identity.gate import PrincipalGate, ResolvedPrincipal
from dc_identity.models import Folder
from dc_identity.principal_ids import parse_principal_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GLOBAL_ADMIN = Role.GLOBAL_ADMIN.value
LOCAL_PREFIX = "local:"


async def load_folder_facts(session: AsyncSession, folder_id: uuid.UUID) -> FolderFacts | None:
    """Ancestors-or-self of ``folder_id`` in one query, root first; None when unknown."""
    target = aliased(Folder)
    ancestor = aliased(Folder)
    stmt = (
        select(
            ancestor.id,
            ancestor.kind,
            ancestor.slug,
            sa_cast(ancestor.path, Text),
            ancestor.archived_at,
        )
        .join(target, ancestor.path.op("@>")(target.path))
        .where(target.id == folder_id)
        .order_by(func.nlevel(ancestor.path))
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return None
    own = rows[-1]
    ancestor_ids = tuple(row[0] for row in rows)
    return FolderFacts(
        id=own[0],
        kind=cast("Literal['root', 'organization', 'project', 'team']", own[1]),
        slug=own[2],
        path=own[3],
        ancestor_ids=ancestor_ids,
        organization_id=ancestor_ids[1] if len(ancestor_ids) >= 2 else None,
        archived=any(row[4] is not None for row in rows),
    )


@dataclass(frozen=True)
class _Decided:
    """``_decide``'s answer plus the target scope it established (None: never looked).

    ``scope_path`` is the target folder's ancestors when the folder was loaded, ``(ROOT,)`` for a
    root permission and ``()`` for an unknown folder or a folder permission without a folder.
    """

    decision: Decision
    scope_path: tuple[uuid.UUID, ...] | None = None


_DENIAL_OUTCOMES: dict[Outcome, DenialOutcome] = {
    Outcome.NOT_FOUND: "not_found",
    Outcome.FORBIDDEN: "forbidden",
}


def narrow_read_only(held: frozenset[str], resolved: ResolvedPrincipal) -> frozenset[str]:
    """D-66: a read-only principal holds only the read permissions its owner holds.

    The one narrowing both the PDP (``LocalAuthorizer``) and the capability reads behind
    ``GET /me`` apply, so what identity tells the UI can never be wider than what it decides.
    """
    return held & READ_PERMISSIONS if resolved.read_only else held


def is_interactive(principal: Principal) -> bool:
    """D-79 (WR-01): a person signed in with a JWT, local or Entra.

    An Entra service principal's client-credentials token is also a JWT, but it is a machine
    credential: it may not manage grants, credentials, memberships or registrations either.
    """
    return principal.via == "jwt" and principal.type == "user"


def _interactive_filter(held: frozenset[str], principal: Principal) -> frozenset[str]:
    """D-79: drop the interactive-only permissions of a principal that is not interactive.

    The PDP refuses such a permission with its own 403 (``LocalAuthorizer.check``); the capability
    reads simply do not list what would be refused, so the listed set equals what would be ALLOW.
    """
    if is_interactive(principal):
        return held
    return frozenset(
        name for name in held if not ((spec := PERMISSIONS.get(name)) and spec.interactive_only)
    )


def capabilities_for(
    principal: Principal, resolved: ResolvedPrincipal, held: frozenset[str]
) -> frozenset[str]:
    """What ``principal`` may actually do out of ``held`` (role-derived permissions)."""
    return _interactive_filter(narrow_read_only(held, resolved), principal)


async def global_permissions_for(
    session: AsyncSession, principal: Principal, resolved: ResolvedPrincipal
) -> frozenset[str]:
    """The principal-level permissions plus the ROOT-evaluated set the caller holds (D-UI-08-19).

    Principal-level permissions are ALLOW for any non-restricted principal in the PDP, except that
    a read-only principal keeps only the read ones. The ROOT set is the role-derived permissions at
    ROOT, narrowed like every other held set. Interactive-only permissions are dropped for a
    principal that is not a JWT sign-in.
    """
    principal_level = frozenset(
        name
        for name, spec in PERMISSIONS.items()
        if spec.target == "principal" and not (resolved.read_only and spec.access != "read")
    )
    root = await root_id(session)
    at_root = permissions_of(await effective_roles(session, grantees_for(principal), root))
    return _interactive_filter(principal_level, principal) | capabilities_for(
        principal, resolved, at_root
    )


async def folder_capabilities(
    session: AsyncSession,
    principal: Principal,
    resolved: ResolvedPrincipal,
    facts: FolderFacts,
) -> frozenset[str]:
    """The folder-evaluated permissions ``principal`` holds at ``facts`` (D-UI-08-20).

    Mirrors ``LocalAuthorizer._decide``: the held set at the folder, narrowed; a permission with
    ``evaluated_at == "parent"`` is judged on the parent folder's narrowed set (the folder itself
    for ROOT). A permission is listed exactly when the PDP would answer ALLOW for it.
    """
    grantees = grantees_for(principal)
    at_target = narrow_read_only(
        permissions_of(await effective_roles(session, grantees, facts.id)), resolved
    )
    at_parent = at_target
    if len(facts.ancestor_ids) >= 2:
        at_parent = narrow_read_only(
            permissions_of(await effective_roles(session, grantees, facts.ancestor_ids[-2])),
            resolved,
        )
    listed = frozenset(
        name
        for name, spec in PERMISSIONS.items()
        if spec.target == "folder"
        and name in (at_parent if spec.evaluated_at == "parent" else at_target)
    )
    return _interactive_filter(listed, principal)


class LocalAuthorizer:
    """The PDP. ``gate`` runs before every decision; the default builds a fail-closed gate."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gate: PrincipalGate | None = None,
        *,
        context: CheckContext | None = None,
        recorder: DenialRecorder | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gate = gate or PrincipalGate(session_factory, UnconfiguredDirectory())
        self._context = context or DEFAULT_CONTEXT
        self._recorder = recorder or DenialRecorder(session_factory)

    async def _record(
        self,
        principal: Principal,
        permission: str,
        outcome: DenialOutcome,
        folder_id: uuid.UUID | None,
        scope_path: tuple[uuid.UUID, ...] | None,
    ) -> None:
        await self._recorder.record(
            principal,
            permission,
            outcome,
            folder_id=folder_id,
            scope_path=scope_path,
            context=self._context,
        )

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: uuid.UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        try:
            decided = await self._decide(principal, permission, folder_id)
        except ProblemError as exc:
            # A gate refusal (401 inactive or stale, 403 principal-not-registered) is a denial;
            # a 503 (directory or Graph outage) is unavailability and is not recorded (D-172).
            if exc.status in (401, 403):
                await self._record(principal, permission, "gate_refused", folder_id, None)
            raise
        decision = decided.decision
        denial = _DENIAL_OUTCOMES.get(decision.outcome)
        if denial is not None:
            # _decide's session is closed here, so the recorder takes the only connection.
            await self._record(principal, permission, denial, folder_id, decided.scope_path)
            return decision
        spec = PERMISSIONS.get(permission)
        # D-79: refuse only after the visibility rules above produced ALLOW, so the refusal can
        # never tell a caller that a folder they cannot see exists.
        if (
            decision.outcome is Outcome.ALLOW
            and spec is not None
            and spec.interactive_only
            and not is_interactive(principal)
        ):
            await self._record(
                principal, permission, "interactive_required", folder_id, decided.scope_path
            )
            raise ProblemError(
                403,
                "interactive-session-required",
                "Forbidden",
                "This action needs an interactive sign-in; access tokens, keys and "
                "service principals cannot perform it.",
            )
        return decision

    async def _decide(
        self,
        principal: Principal,
        permission: str,
        folder_id: uuid.UUID | None,
    ) -> _Decided:
        # D-57, D-72: the gate is the first statement, so no branch below (password:rotate,
        # restricted, authz:check) can answer for a principal it refuses.
        resolved = await self._gate.resolve(principal)
        if permission == "password:rotate":
            ref = parse_principal_id(principal.id)
            allowed = principal.via == "jwt" and ref is not None and ref.kind == "local_user"
            return _Decided(Decision(outcome=Outcome.ALLOW if allowed else Outcome.FORBIDDEN))
        if principal.restricted:
            # A bootstrap session holds no authority beyond rotating its own password.
            return _Decided(Decision(outcome=Outcome.FORBIDDEN))
        if permission == "authz:check":
            return _Decided(Decision(outcome=Outcome.ALLOW))

        spec = PERMISSIONS.get(permission)
        if spec is None or spec.target == "special":
            return _Decided(Decision(outcome=Outcome.FORBIDDEN))

        grantees = grantees_for(principal)
        async with self._session_factory() as session:
            if spec.target == "principal":
                if resolved.read_only and spec.access != "read":
                    return _Decided(Decision(outcome=Outcome.FORBIDDEN))
                return _Decided(Decision(outcome=Outcome.ALLOW))
            if spec.target == "root":
                root = await root_id(session)
                held = narrow_read_only(
                    permissions_of(await effective_roles(session, grantees, root)), resolved
                )
                if permission in held:
                    return _Decided(Decision(outcome=Outcome.ALLOW, permissions=held), (root,))
                return _Decided(Decision(outcome=Outcome.FORBIDDEN), (root,))

            if folder_id is None:
                return _Decided(Decision(outcome=Outcome.FORBIDDEN), ())
            facts = await load_folder_facts(session, folder_id)
            if facts is None:
                return _Decided(Decision(outcome=Outcome.NOT_FOUND), ())
            at_target = narrow_read_only(
                permissions_of(await effective_roles(session, grantees, folder_id)), resolved
            )
            evaluated = at_target
            if spec.evaluated_at == "parent" and len(facts.ancestor_ids) >= 2:
                evaluated = narrow_read_only(
                    permissions_of(
                        await effective_roles(session, grantees, facts.ancestor_ids[-2])
                    ),
                    resolved,
                )
        scope = facts.ancestor_ids
        if permission in evaluated:
            return _Decided(
                Decision(outcome=Outcome.ALLOW, folder=facts, permissions=at_target), scope
            )
        if spec.visible_via is not None and spec.visible_via in at_target:
            return _Decided(Decision(outcome=Outcome.FORBIDDEN), scope)
        return _Decided(Decision(outcome=Outcome.NOT_FOUND), scope)

    async def accessible_folders(self, principal: Principal, permission: str) -> FolderScope:
        resolved = await self._gate.resolve(principal)
        spec = PERMISSIONS.get(permission)
        if principal.restricted or spec is None or spec.target != "folder":
            return FolderScope(all=False, folder_ids=frozenset())
        if resolved.read_only and spec.access != "read":
            return FolderScope(all=False, folder_ids=frozenset())  # D-66: a read-only token
        if spec.interactive_only and not is_interactive(principal):
            return FolderScope(all=False, folder_ids=frozenset())  # D-79: nothing to scope
        async with self._session_factory() as session:
            return await accessible_scope(session, grantees_for(principal), roles_with(permission))
