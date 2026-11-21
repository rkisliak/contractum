"""Contract persistence: insert a contract with its first version, and read it back."""

from __future__ import annotations

import uuid

from dc_auth.authorizer import FolderFacts, FolderScope
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from dc_odcs import ParsedContract, SearchFields
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.models import UNIQUE_ODCS_ID_CONSTRAINT, Contract, ContractVersion
from dc_contracts.search import ContractSearchFilters, build_search_query, head_version_id


def duplicate_contract_id() -> ProblemError:
    return ProblemError(
        409,
        "duplicate-contract-id",
        "Conflict",
        "A contract with this id already exists in this organization "
        "(or, for a contract directly at ROOT, among the contracts at ROOT).",
    )


def odcs_scope_id(facts: FolderFacts) -> uuid.UUID:
    """The ODCS-id scope key of a contract created in the folder ``facts``.

    Contracts at an Organization, its Projects and its Teams share the Organization's scope;
    contracts directly at ROOT share one ROOT scope keyed by the ROOT folder id. The existing NOT
    NULL ``contract.organization_id`` column and ``uq_contract_organization_id_odcs_id`` then
    implement the rule exactly, and ``references.py`` / ``link_facts.py`` (SQL equality on
    ``organization_id``) keep resolving ODCS-id links inside the same scope: no migration.
    """
    if facts.organization_id is not None:
        return facts.organization_id
    return facts.ancestor_ids[0]


async def insert_contract(
    uow: UnitOfWork,
    facts: FolderFacts,
    parsed: ParsedContract,
    fields: SearchFields,
    *,
    raw: bytes,
    template_id: uuid.UUID | None = None,
    template_version: int | None = None,
    origin_contract_id: uuid.UUID | None = None,
    origin_version_id: uuid.UUID | None = None,
) -> tuple[Contract, ContractVersion]:
    """Insert ``contract`` and ``contract_version`` 1 (state ``draft``) in the open unit of work.

    ``folder_id``, ``ancestor_ids`` and ``organization_id`` come only from identity's folder
    facts. ``raw`` is stored verbatim; nothing about the submitted document is normalised.
    ``template_id`` and ``template_version`` record the template origin of a contract created from
    a template (CONT-02); both or neither. ``origin_contract_id`` and ``origin_version_id`` record
    the contract version or draft a contract was copied from (D-44b); both or neither.
    """
    session = uow.session
    contract = Contract(
        folder_id=facts.id,
        ancestor_ids=list(facts.ancestor_ids),
        organization_id=odcs_scope_id(facts),
        odcs_id=parsed.odcs_id,
        template_id=template_id,
        template_version=template_version,
        origin_contract_id=origin_contract_id,
        origin_version_id=origin_version_id,
    )
    session.add(contract)
    # The 409 is keyed on the constraint name uq_contract_organization_id_odcs_id (D-09), never
    # on message text. No SAVEPOINT: releasing a nested transaction fires the audit tripwire's
    # before_commit hook before the outbox row exists (see identity's create_folder). The mapped
    # 409 aborts the unit of work, which rolls back, so there is nothing to preserve.
    try:
        await session.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == UNIQUE_ODCS_ID_CONSTRAINT:
            raise duplicate_contract_id() from exc
        raise
    version = ContractVersion(
        contract_id=contract.id,
        version_no=1,
        state="draft",
        odcs_api_version=parsed.api_version,
        raw_document=raw,
        media_type=parsed.media_type,
        content_sha256=parsed.content_sha256,
        size_bytes=parsed.size_bytes,
        document=parsed.document,
        name=fields.name,
        domain=fields.domain,
        status=fields.status,
        tags=list(fields.tags),
        description_text=fields.description_text,
        odcs_version=fields.odcs_version,
    )
    session.add(version)
    await session.flush()
    return contract, version


async def get_contract_with_version(
    session: AsyncSession, contract_id: uuid.UUID
) -> tuple[Contract, ContractVersion] | None:
    """The contract and its head row, or None when unknown.

    The head row is the Latest when the contract has one, else its newest row by ``version_no``
    (a draft); the same row the list shows for every caller (``search.head_version_id``).
    """
    stmt = (
        select(Contract, ContractVersion)
        .join(
            ContractVersion,
            (ContractVersion.contract_id == Contract.id)
            & (ContractVersion.id == head_version_id()),
        )
        .where(Contract.id == contract_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return row[0], row[1]


async def search_contracts(
    session: AsyncSession,
    filters: ContractSearchFilters,
    *,
    metadata_scope: FolderScope,
    content_scope: FolderScope,
    published_scope: FolderScope | None = None,
) -> tuple[list[tuple[Contract, ContractVersion, bool]], bool]:
    """One page of contracts in id order, and whether a further page exists.

    Each row is ``(contract, version, content_visible)``: ``content_visible`` is true when the
    caller's ``content_scope`` or ``published_scope`` covers the row (D-36). All-empty scopes issue
    no query at all. At most ``filters.limit`` rows are returned; the extra row the statement
    fetches only answers "is there a next page".
    """
    stmt = build_search_query(
        filters, metadata_scope, content_scope, published_scope=published_scope
    )
    if stmt is None:
        return [], False
    rows = [(row[0], row[1], bool(row[2])) for row in (await session.execute(stmt)).all()]
    return rows[: filters.limit], len(rows) > filters.limit
