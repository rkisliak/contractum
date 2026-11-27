"""D-07: EXPLAIN the Folders tree's per-folder ``sort=name`` statement on a seeded dataset.

10,000 contracts over 1,000 folders (10 per folder) are inserted straight into a database built
from the migrations. The statement is the exact one the list route issues for one expanded folder
(``folder_id`` set, ``include_subtree`` off, ``sort=name``, ``limit=100``) for the Global admin
scope and for a single-team reader. The plan must reach ``contract`` through
``ix_contract_folder_id`` and never scan the whole table once per expanded folder (T-16-09).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from dc_auth.authorizer import FolderScope
from dc_contracts.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_contracts.search import ContractSearchFilters, build_search_query
from dc_core.migrations import run_alembic
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

CONTRACTS = 10_000
FOLDERS = 1_000
INDEX = "ix_contract_folder_id"
INDEX_SCANS = {"Index Scan", "Index Only Scan", "Bitmap Index Scan"}

SEED_CONTRACTS = """
INSERT INTO contract (folder_id, ancestor_ids, organization_id, odcs_id, created_by, updated_by)
SELECT f.folder_id,
       ARRAY[f.root_id, f.org_id, f.folder_id],
       f.org_id,
       'urn:seed:' || g.i,
       'system:test',
       'system:test'
FROM generate_series(1, %(contracts)s) AS g(i)
JOIN seed_folder f ON f.n = mod(g.i - 1, %(folders)s) + 1
"""

SEED_VERSIONS = """
INSERT INTO contract_version (contract_id, odcs_api_version, raw_document, media_type,
                              content_sha256, size_bytes, document, name, created_by, updated_by)
SELECT c.id, 'v3.2.0', '\\x00'::bytea, 'application/yaml', md5(c.odcs_id), 1, '{}'::jsonb,
       'name-' || md5(c.odcs_id), 'system:test', 'system:test'
FROM contract c
"""


@pytest.fixture(scope="module")
def seeded(pg_admin_url: str) -> Iterator[tuple[Engine, uuid.UUID]]:
    """An engine on a migrated database holding the dataset, and the folder id to list."""
    name = f"t_{uuid.uuid4().hex}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_url(pg_admin_url).set(drivername="postgresql+psycopg", database=name)
    db_url = url.render_as_string(hide_password=False)
    engine = create_engine(db_url)
    try:
        run_alembic(
            script_location=SCRIPT_LOCATION,
            version_table=VERSION_TABLE,
            url=db_url,
            command="upgrade",
            revision="head",
        )
        root = uuid.uuid4()
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE seed_folder AS SELECT n, gen_random_uuid() AS folder_id, "
                "%(root)s::uuid AS root_id, gen_random_uuid() AS org_id "
                "FROM generate_series(1, %(folders)s) AS n",
                {"root": root, "folders": FOLDERS},
            )
            conn.exec_driver_sql(SEED_CONTRACTS, {"contracts": CONTRACTS, "folders": FOLDERS})
            conn.exec_driver_sql(SEED_VERSIONS)
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("ANALYZE contract, contract_version")
        with engine.connect() as conn:
            counts = conn.exec_driver_sql(
                "SELECT count(*), count(DISTINCT folder_id) FROM contract"
            ).one()
            assert counts == (CONTRACTS, FOLDERS)
            folder = conn.exec_driver_sql(
                "SELECT folder_id FROM seed_folder ORDER BY n OFFSET 417 LIMIT 1"
            ).scalar_one()
        yield engine, folder
    finally:
        engine.dispose()
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def explain(engine: Engine, filters: ContractSearchFilters, scope: FolderScope) -> dict[str, Any]:
    """``EXPLAIN (ANALYZE, FORMAT JSON)`` of the list statement for ``filters`` and ``scope``."""
    stmt = build_search_query(filters, scope, scope, published_scope=scope)
    assert stmt is not None
    compiled = stmt.compile(dialect=engine.dialect)
    with engine.connect() as conn:
        raw = conn.exec_driver_sql(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + str(compiled), compiled.params
        ).scalar_one()
    plan: dict[str, Any] = raw[0] if isinstance(raw, list) else raw
    return plan


def nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Every node of the plan tree, depth first."""
    found = [node]
    for child in node.get("Plans", []):
        found.extend(nodes(child))
    return found


def contract_scans(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The plan nodes that read the ``contract`` table."""
    return [node for node in nodes(plan["Plan"]) if node.get("Relation Name") == "contract"]


def indexes_used(plan: dict[str, Any]) -> set[str]:
    return {str(node["Index Name"]) for node in nodes(plan["Plan"]) if "Index Name" in node}


def describe(plan: dict[str, Any]) -> str:
    scans = ", ".join(scan["Node Type"] for scan in contract_scans(plan))
    used = ", ".join(sorted(indexes_used(plan))) or "no index"
    return f"{scans} via {used}; {plan['Execution Time']:.2f} ms"


def tree_filters(folder: uuid.UUID) -> ContractSearchFilters:
    return ContractSearchFilters(folder_id=folder, include_subtree=False, sort="name", limit=100)


def test_the_dataset_is_the_agreed_size(seeded: tuple[Engine, uuid.UUID]) -> None:
    engine, _ = seeded
    with engine.connect() as conn:
        contracts, folders = conn.exec_driver_sql(
            "SELECT count(*), count(DISTINCT folder_id) FROM contract"
        ).one()
    assert contracts >= 10_000
    assert folders >= 1_000


@pytest.mark.parametrize("label", ["global admin", "single team"])
def test_the_per_folder_statement_reaches_contract_through_the_folder_index(
    seeded: tuple[Engine, uuid.UUID], label: str
) -> None:
    engine, folder = seeded
    scope = (
        FolderScope(all=True)
        if label == "global admin"
        else FolderScope(all=False, folder_ids=frozenset({folder}))
    )

    plan = explain(engine, tree_filters(folder), scope)
    print(f"per-folder plan, {label}: {describe(plan)}")

    scans = contract_scans(plan)
    assert scans, "the statement must read contract"
    assert not [scan for scan in scans if scan["Node Type"] == "Seq Scan"], describe(plan)
    assert {scan["Node Type"] for scan in scans} <= INDEX_SCANS | {"Bitmap Heap Scan"}
    assert INDEX in indexes_used(plan), describe(plan)


def test_the_global_name_sorted_list_plan_is_recorded(seeded: tuple[Engine, uuid.UUID]) -> None:
    """The unscoped ``sort=name`` list stays allowed (RESEARCH A5); only its plan is printed."""
    engine, _ = seeded
    plan = explain(engine, ContractSearchFilters(sort="name", limit=100), FolderScope(all=True))
    print(f"global sort=name plan: {describe(plan)}")
