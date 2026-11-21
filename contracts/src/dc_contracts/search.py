"""Contract list/search query builder and the keyset cursor codec (CONT-05, D-12).

One statement per request, every value a bound parameter (T-01-49). Ordering is by ``contract.id``
(a uuidv7: unique and time ordered), so equal names never tie and a page sequence stays stable
while new contracts are inserted: new rows have a larger id and only ever appear after the cursor.

There is deliberately no total or count anywhere in this module: a count leaks the existence of
contracts the caller cannot see (research, Search section).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass, field
from typing import Any

from dc_auth.authorizer import FolderScope
from dc_core.problems import ProblemError
from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    any_,
    false,
    func,
    literal,
    not_,
    or_,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import QueryableAttribute

from dc_contracts.models import Contract, ContractVersion

MAX_Q_LENGTH = 200
MAX_LIMIT = 100
DEFAULT_LIMIT = 50


def _invalid(pointer: str, detail: str) -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Unprocessable Content",
        "The request failed validation.",
        errors=[{"pointer": pointer, "detail": detail}],
    )


def like_escape(q: str) -> str:
    """Escape ``\\``, ``%`` and ``_`` so ``q`` matches only itself inside an ILIKE pattern.

    The backslash goes first: escaping it after the other two would double the escapes just added.
    """
    return q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def encode_cursor(contract_id: uuid.UUID) -> str:
    """Opaque cursor: base64url of the uuid string, without padding."""
    return base64.urlsafe_b64encode(str(contract_id).encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> uuid.UUID:
    """Inverse of :func:`encode_cursor`. Anything else is a 422 (T-01-51).

    Only the canonical spelling is accepted, so one position has exactly one cursor.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        text = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode(
            "ascii"
        )
        value = uuid.UUID(text)
        if encode_cursor(value) != raw:  # also rejects stray characters and non-canonical forms
            raise ValueError("not the canonical cursor")
    except (ValueError, binascii.Error, UnicodeError):
        raise _invalid("/query/cursor", "cursor is not valid") from None
    return value


def reject_nul(**values: str | list[str] | None) -> None:
    """422 for a NUL character in any text filter: PostgreSQL cannot bind it in a text value."""
    for name, value in values.items():
        items = [value] if isinstance(value, str) else (value or [])
        if any("\x00" in item for item in items):
            raise _invalid(f"/query/{name}", "NUL (U+0000) characters are not allowed")


@dataclass(frozen=True)
class ContractSearchFilters:
    folder_id: uuid.UUID | None = None
    include_subtree: bool = False
    odcs_id: str | None = None
    name: str | None = None
    domain: str | None = None
    status: str | None = None
    tags: list[str] = field(default_factory=list)
    q: str | None = None
    limit: int = DEFAULT_LIMIT
    cursor: uuid.UUID | None = None


def _uuid_array(ids: frozenset[uuid.UUID]) -> ColumnElement[list[uuid.UUID]]:
    return literal(sorted(ids, key=str), ARRAY(PG_UUID(as_uuid=True)))


def scope_clause(
    scope: FolderScope,
    *,
    folder_col: QueryableAttribute[Any],
    ancestors_col: QueryableAttribute[Any],
) -> ColumnElement[bool] | None:
    """WHERE clause for a compact scope, or ``None`` when the scope is empty (issue no query).

    Mirrors ``FolderScope.covers``: ``(all AND no excluded id in ancestors) OR folder in ids``.
    ``ancestors_col`` is a root-first uuid[] that includes the row's own folder, so an excluded
    folder hides itself and everything below it.
    """
    if scope.is_empty:
        return None
    visible: list[ColumnElement[bool]] = []
    if scope.all:
        if scope.excluded_subtrees:
            excluded = _uuid_array(scope.excluded_subtrees)
            visible.append(not_(ancestors_col.overlap(excluded)))
        else:
            visible.append(true())
    if scope.folder_ids:
        visible.append(folder_col == any_(_uuid_array(scope.folder_ids)))
    return or_(*visible)


def head_version_id() -> ColumnElement[uuid.UUID]:
    """The version row that stands for a contract everywhere a single row is shown (the head).

    The Latest when the pointer is set; before the first publish the newest row by ``version_no``
    (a draft). One head per contract for every caller keeps list filters and by-id reads on the
    same values (research Pattern 10, pitfall 10). The scalar subquery is only evaluated when the
    Latest pointer is null.
    """
    newest = (
        select(ContractVersion.id)
        .where(ContractVersion.contract_id == Contract.id)
        .order_by(ContractVersion.version_no.desc())
        .limit(1)
        .correlate(Contract)
        .scalar_subquery()
    )
    return func.coalesce(Contract.latest_version_id, newest)


def build_search_query(
    filters: ContractSearchFilters,
    metadata_scope: FolderScope,
    content_scope: FolderScope,
    *,
    published_scope: FolderScope | None = None,
) -> Select[Contract, ContractVersion, bool] | None:
    """The list/search statement, or ``None`` when the caller may see nothing (no query at all).

    ``metadata_scope`` (``contract:read-metadata``) and ``published_scope``
    (``contract:read-published``, only contracts that have a Latest) decide which rows appear;
    ``content_scope`` (``contract:read``) and the published scope decide which of those rows the
    caller may read in full (D-35, D-36). Each row is the contract's head version (Latest when
    one exists) and carries a ``content_visible`` flag. The content-derived predicates
    (``domain``, ``tags`` and the description part of ``q``) are AND-ed with that flag, so a
    metadata-only caller can never use a filter as an oracle for content they may not read
    (research pitfall 4), and a published-only caller only ever matches Latest values.
    """
    metadata = scope_clause(
        metadata_scope, folder_col=Contract.folder_id, ancestors_col=Contract.ancestor_ids
    )
    content = scope_clause(
        content_scope, folder_col=Contract.folder_id, ancestors_col=Contract.ancestor_ids
    )
    published_folders = (
        scope_clause(
            published_scope, folder_col=Contract.folder_id, ancestors_col=Contract.ancestor_ids
        )
        if published_scope is not None
        else None
    )
    published = (
        and_(published_folders, Contract.latest_version_id.is_not(None))
        if published_folders is not None
        else None
    )
    clauses = [clause for clause in (metadata, content, published) if clause is not None]
    if not clauses:
        return None
    readable = [clause for clause in (content, published) if clause is not None]
    content_clause: ColumnElement[bool] = or_(*readable) if readable else false()
    visible = or_(*clauses)
    stmt = select(Contract, ContractVersion, content_clause.label("content_visible")).join(
        ContractVersion,
        (ContractVersion.contract_id == Contract.id) & (ContractVersion.id == head_version_id()),
    )
    # A soft-deleted contract is on no list for anyone (D-55).
    stmt = stmt.where(visible, Contract.deleted_at.is_(None))
    if filters.folder_id is not None:
        if filters.include_subtree:
            stmt = stmt.where(Contract.ancestor_ids.contains([filters.folder_id]))
        else:
            stmt = stmt.where(Contract.folder_id == filters.folder_id)
    if filters.odcs_id is not None:
        stmt = stmt.where(Contract.odcs_id == filters.odcs_id)
    if filters.name is not None:
        stmt = stmt.where(func.lower(ContractVersion.name) == func.lower(filters.name))
    if filters.domain is not None:
        stmt = stmt.where(
            and_(content_clause, func.lower(ContractVersion.domain) == func.lower(filters.domain))
        )
    if filters.status is not None:
        stmt = stmt.where(ContractVersion.status == filters.status)
    if filters.tags:
        stmt = stmt.where(and_(content_clause, ContractVersion.tags.contains(filters.tags)))
    if filters.q:
        pattern = f"%{like_escape(filters.q)}%"
        stmt = stmt.where(
            or_(
                ContractVersion.name.ilike(pattern, escape="\\"),
                Contract.odcs_id.ilike(pattern, escape="\\"),
                and_(content_clause, ContractVersion.description_text.ilike(pattern, escape="\\")),
            )
        )
    if filters.cursor is not None:
        stmt = stmt.where(Contract.id > filters.cursor)
    # limit + 1: the extra row only says "there is a next page"; it is never returned.
    return stmt.order_by(Contract.id).limit(filters.limit + 1)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_Q_LENGTH",
    "ContractSearchFilters",
    "build_search_query",
    "decode_cursor",
    "encode_cursor",
    "head_version_id",
    "like_escape",
    "reject_nul",
    "scope_clause",
]
