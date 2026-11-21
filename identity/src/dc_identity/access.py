"""Effective roles of a principal at a folder (D-28, D-29, D-31).

One statement answers "which roles does this principal hold here": the grants made on the folder
or any ancestor, to the principal itself, to a local group it belongs to, or to an Entra group the
caller supplies (always empty in Phase 2; Phase 4 plugs in here). A grant made at G reaches folder
D unless a block-inheritance flag sits strictly below G on the path down to D; ``global_admin`` is
never cut. Authority comes only from these rows, never from token claims.

``dc_auth.oracle`` is the literal-reading reference; ``tests/integration/test_authz_oracle.py``
proves this SQL equal to it. Every statement here binds values as parameters; nothing is formatted
into SQL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dc_auth.authorizer import FolderScope
from dc_auth.roles import Role
from sqlalchemy import ARRAY, Text, bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from dc_identity.gate import resolution_for
from dc_identity.principal_ids import grantee_type, parse_principal_id

if TYPE_CHECKING:
    from dc_auth.principal import Principal
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.selectable import Subquery


@dataclass(frozen=True)
class Grantees:
    """Who is asking: the principal's own identity plus externally resolved group names."""

    principal_type: str
    principal_id: str
    entra_groups: tuple[str, ...] = ()


# Principals whose grantee type, Entra groups or narrowing exist only in the gate's answer.
_GATED_VIA = frozenset({"pat", "api_key"})


def grantees_for(principal: Principal) -> Grantees:
    """Map a verified principal to its grantee identity, as the PrincipalGate resolved it.

    ``PrincipalGate.resolve`` runs first in every request (``LocalAuthorizer`` calls it before any
    decision) and leaves its answer for this request; this returns that answer. Entra, ``sa:`` and
    PAT or API-key principals have nothing but the gate's answer, so asking without one is a
    programming error (RuntimeError) rather than a silently group-less identity. Local and
    generic-OIDC principals fall back to their own type and id, which no grant or membership row
    can carry for an unknown type (fail closed).
    """
    resolved = resolution_for(principal)
    if resolved is not None:
        return Grantees(resolved.grantee_type, resolved.grantee_id, resolved.entra_groups)
    ref = parse_principal_id(principal.id)
    if (ref is not None and ref.kind in ("entra", "service_account")) or (
        str(principal.via) in _GATED_VIA
    ):
        raise RuntimeError("PrincipalGate.resolve must run before grantees_for")
    return Grantees(principal_type=grantee_type(principal), principal_id=principal.id)


_EFFECTIVE_ROLES = text(
    """
    WITH chain AS (
      SELECT a.id, a.block_inheritance, nlevel(a.path) AS depth
      FROM folder d JOIN folder a ON a.path @> d.path WHERE d.id = :folder
    ), cut AS (
      SELECT COALESCE(max(depth), 0) AS depth FROM chain WHERE block_inheritance
    ), me AS (
      SELECT CAST(:ptype AS text) AS ptype, CAST(:pid AS text) AS pid
      UNION ALL
      SELECT 'local_group', CAST(gm.group_id AS text) FROM group_members gm
        WHERE gm.member_type = :ptype AND gm.member_id = :pid
      UNION ALL
      SELECT 'entra_group', g FROM unnest(CAST(:entra AS text[])) AS g
    )
    SELECT DISTINCT ra.role
    FROM chain
    JOIN role_assignments ra ON ra.folder_id = chain.id
    JOIN me ON me.ptype = ra.principal_type AND me.pid = ra.principal_id
    WHERE chain.depth >= (SELECT depth FROM cut) OR ra.role = :global_admin
    """
).bindparams(bindparam("entra", type_=ARRAY(Text)))


async def effective_roles(
    session: AsyncSession, grantees: Grantees, folder_id: uuid.UUID
) -> frozenset[str]:
    """Role values the grantees hold at ``folder_id`` after inheritance and the block cut."""
    rows = await session.execute(
        _EFFECTIVE_ROLES,
        {
            "folder": folder_id,
            "ptype": grantees.principal_type,
            "pid": grantees.principal_id,
            "entra": list(grantees.entra_groups),
            "global_admin": Role.GLOBAL_ADMIN.value,
        },
    )
    return frozenset(str(role) for role in rows.scalars())


_LOCAL_GROUPS = text(
    """
    SELECT CAST(group_id AS text) FROM group_members
    WHERE member_type = :ptype AND member_id = :pid
    """
)


async def grantee_set(session: AsyncSession, grantees: Grantees) -> frozenset[tuple[str, str]]:
    """Every ``(principal_type, principal_id)`` the grantees answer to: self, groups, Entra."""
    found: set[tuple[str, str]] = {(grantees.principal_type, grantees.principal_id)}
    rows = await session.execute(
        _LOCAL_GROUPS, {"ptype": grantees.principal_type, "pid": grantees.principal_id}
    )
    found.update(("local_group", str(group_id)) for group_id in rows.scalars())
    found.update(("entra_group", name) for name in grantees.entra_groups)
    return frozenset(found)


_CUT = text(
    """
    SELECT a.id FROM folder d JOIN folder a ON a.path @> d.path
    WHERE d.id = :folder AND a.block_inheritance
    ORDER BY nlevel(a.path) DESC LIMIT 1
    """
)


async def inheritance_cut(session: AsyncSession, folder_id: uuid.UUID) -> uuid.UUID | None:
    """Id of the deepest folder on the path to ``folder_id`` (itself included) that blocks."""
    row = (await session.execute(_CUT, {"folder": folder_id})).scalar_one_or_none()
    return None if row is None else uuid.UUID(str(row))


async def root_id(session: AsyncSession) -> uuid.UUID:
    row = (await session.execute(text("SELECT id FROM folder WHERE kind = 'root'"))).scalar_one()
    return uuid.UUID(str(row))


# Folders where one of ``:roles`` applies (research Pattern 2, query 2). The blocks CTE is
# MATERIALIZED: the correlated-GiST variant is about nine times slower on a large tree. ``{root}``
# and ``{covered}`` are fixed fragments chosen in code; every value is a bound parameter.
_EXPANSION = """
    WITH me AS (
      SELECT CAST(:ptype AS text) AS ptype, CAST(:pid AS text) AS pid
      UNION ALL
      SELECT 'local_group', CAST(gm.group_id AS text) FROM group_members gm
        WHERE gm.member_type = :ptype AND gm.member_id = :pid
      UNION ALL
      SELECT 'entra_group', g FROM unnest(CAST(:entra AS text[])) AS g
    ), blocks AS MATERIALIZED (
      SELECT path, nlevel(path) AS depth FROM folder WHERE block_inheritance
    ), g AS (
      SELECT ra.role, f.path, nlevel(f.path) AS depth
      FROM role_assignments ra
      JOIN me ON me.ptype = ra.principal_type AND me.pid = ra.principal_id
      JOIN folder f ON f.id = ra.folder_id
      WHERE ra.role = ANY(:roles){root}
    )
    SELECT DISTINCT d.id FROM g JOIN folder d ON d.path <@ g.path
    WHERE (g.role = :global_admin
       OR NOT EXISTS (SELECT 1 FROM blocks b WHERE b.depth > g.depth AND b.path @> d.path)){covered}
"""
_NOT_ROOT = " AND f.kind <> 'root'"
# Folders a ROOT grant (all minus the blocked subtrees) already shows need no explicit id.
_INSIDE_A_BLOCK = " AND EXISTS (SELECT 1 FROM blocks bl WHERE bl.path @> d.path)"


def _expansion_params(grantees: Grantees, roles: frozenset[str]) -> dict[str, object]:
    return {
        "ptype": grantees.principal_type,
        "pid": grantees.principal_id,
        "entra": list(grantees.entra_groups),
        "roles": sorted(roles),
        "global_admin": Role.GLOBAL_ADMIN.value,
    }


def _expansion_statement(*, root: bool, covered: bool) -> str:
    return _EXPANSION.format(
        root="" if root else _NOT_ROOT, covered=_INSIDE_A_BLOCK if covered else ""
    )


def visible_folders_subquery(grantees: Grantees, roles: frozenset[str]) -> Subquery:
    """Every folder where one of ``roles`` applies, as a subquery with the column ``id``.

    Identity's own folder list joins this in SQL instead of shipping an id list (T-02-15).
    """
    statement = (
        text(_expansion_statement(root=True, covered=False))
        .bindparams(
            bindparam("entra", type_=ARRAY(Text)),
            bindparam("roles", type_=ARRAY(Text)),
            **_expansion_params(grantees, roles),
        )
        .columns(id=PG_UUID(as_uuid=True))
    )
    return statement.subquery("visible_folders")


_BLOCKED = text("SELECT id FROM folder WHERE block_inheritance")


async def accessible_scope(
    session: AsyncSession, grantees: Grantees, roles: frozenset[str]
) -> FolderScope:
    """Compact scope of the folders where one of ``roles`` applies for ``grantees``.

    ``global_admin`` held at ROOT is everything. Any other ROOT-level grant reaches every folder
    except the blocked subtrees (``excluded_subtrees``). Every other grant is expanded exactly into
    ``folder_ids``, including grants made at or inside a blocked folder, which keep applying there
    (D-29). ``FolderScope.covers`` on the result equals ``dc_auth.oracle.folders_with``.
    """
    held_at_root = (await effective_roles(session, grantees, await root_id(session))) & roles
    if Role.GLOBAL_ADMIN.value in held_at_root:
        return FolderScope(all=True)
    everything = bool(held_at_root)
    excluded: frozenset[uuid.UUID] = frozenset()
    if everything:
        blocked = await session.execute(_BLOCKED)
        excluded = frozenset(uuid.UUID(str(row)) for row in blocked.scalars())
    statement = text(_expansion_statement(root=False, covered=everything)).bindparams(
        bindparam("entra", type_=ARRAY(Text)), bindparam("roles", type_=ARRAY(Text))
    )
    rows = await session.execute(statement, _expansion_params(grantees, roles))
    folder_ids = frozenset(uuid.UUID(str(row)) for row in rows.scalars())
    return FolderScope(all=everything, folder_ids=folder_ids, excluded_subtrees=excluded)
