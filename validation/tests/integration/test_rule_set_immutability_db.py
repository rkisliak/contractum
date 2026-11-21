"""Published rule-set versions are immutable in the database, and scope is consistent (RULE-04).

Direct SQL on a migrated database, bypassing the service entirely: the triggers and check
constraints are the last line of defence (T-5-14).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import errors

from .conftest import drop_platform_seed

pytestmark = pytest.mark.integration

HASH = "a" * 64


@pytest.fixture
def conn(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> Iterator[psycopg.Connection]:
    migrate_validation()
    drop_platform_seed(fresh_db_url)
    url = fresh_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as connection:
        yield connection


def insert_rule_set(
    conn: psycopg.Connection,
    *,
    scope: str = "folder",
    folder_id: uuid.UUID | None | str = "new",
    folder_kind: str | None = "team",
    name: str | None = None,
) -> uuid.UUID:
    if folder_id == "new":
        folder_id = uuid.uuid4() if scope == "folder" else None
    rule_set_id = uuid.uuid4()
    label = name if name is not None else f"set-{rule_set_id}"
    conn.execute(
        "INSERT INTO rule_set (id, scope, folder_id, folder_kind, name, draft_content_sha256,"
        " created_by, updated_by) VALUES (%s, %s, %s, %s, %s, %s, 'test', 'test')",
        (
            rule_set_id,
            scope,
            folder_id,
            folder_kind,
            label,
            HASH,
        ),
    )
    return rule_set_id


def insert_version(conn: psycopg.Connection, rule_set_id: uuid.UUID, version_no: int = 1) -> None:
    conn.execute(
        "INSERT INTO rule_set_version (rule_set_id, version_no, dsl_version, rules,"
        " content_sha256, rule_keys, created_by, updated_by)"
        " VALUES (%s, %s, 'cel-1', %s, %s, %s, 'test', 'test')",
        (rule_set_id, version_no, json.dumps([{"key": "a"}]), HASH, ["a"]),
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "rules = '[]'::jsonb",
        "version_no = 2",
        "dsl_version = 'cel-2'",
        "content_sha256 = 'b'",
        "rule_keys = ARRAY['b']",
        "rule_set_id = gen_random_uuid()",
    ],
)
def test_a_content_column_of_a_published_version_cannot_be_updated(
    conn: psycopg.Connection, assignment: str
) -> None:
    rule_set_id = insert_rule_set(conn)
    insert_version(conn, rule_set_id)
    with pytest.raises(errors.RaiseException, match="immutable"):
        conn.execute(f"UPDATE rule_set_version SET {assignment}")  # noqa: S608 - fixed literals
    row = conn.execute("SELECT version_no, rules FROM rule_set_version").fetchone()
    assert row == (1, [{"key": "a"}])


def test_a_published_version_cannot_be_deleted(conn: psycopg.Connection) -> None:
    rule_set_id = insert_rule_set(conn)
    insert_version(conn, rule_set_id)
    with pytest.raises(errors.RaiseException, match="cannot be deleted"):
        conn.execute("DELETE FROM rule_set_version")
    assert conn.execute("SELECT count(*) FROM rule_set_version").fetchone() == (1,)


def test_deprecation_and_the_audit_stamps_stay_writable(conn: psycopg.Connection) -> None:
    rule_set_id = insert_rule_set(conn)
    insert_version(conn, rule_set_id)
    conn.execute("UPDATE rule_set_version SET deprecated_at = now(), deprecated_by = 'pm'")
    conn.execute("UPDATE rule_set_version SET updated_by = 'other', updated_at = now()")
    row = conn.execute("SELECT deprecated_by, deprecated_at IS NOT NULL FROM rule_set_version")
    assert row.fetchone() == ("pm", True)


def test_a_version_number_is_unique_within_its_rule_set(conn: psycopg.Connection) -> None:
    rule_set_id = insert_rule_set(conn)
    insert_version(conn, rule_set_id, 1)
    with pytest.raises(errors.UniqueViolation):
        insert_version(conn, rule_set_id, 1)
    other = insert_rule_set(conn)
    insert_version(conn, other, 1)


def test_a_platform_row_with_a_folder_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="scope_folder_consistent"):
        insert_rule_set(conn, scope="platform", folder_id=uuid.uuid4(), folder_kind="team")


def test_a_folder_row_without_a_folder_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="scope_folder_consistent"):
        insert_rule_set(conn, scope="folder", folder_id=None, folder_kind=None)


def test_a_folder_id_without_a_kind_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="scope_folder_consistent"):
        insert_rule_set(conn, scope="folder", folder_id=uuid.uuid4(), folder_kind=None)


def test_an_unknown_scope_is_refused(conn: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="scope_known"):
        insert_rule_set(conn, scope="global", folder_id=None, folder_kind=None)


def test_a_platform_rule_set_has_no_folder_and_its_name_is_unique_among_platform_sets(
    conn: psycopg.Connection,
) -> None:
    insert_rule_set(conn, scope="platform", folder_id=None, folder_kind=None, name="Structural")
    with pytest.raises(errors.UniqueViolation, match="uq_rule_set_platform_name"):
        insert_rule_set(conn, scope="platform", folder_id=None, folder_kind=None, name="structural")
    row = conn.execute("SELECT folder_id, ancestor_ids FROM rule_set").fetchone()
    assert row == (None, [])


def test_names_are_unique_per_folder_ignoring_case_but_not_across_folders(
    conn: psycopg.Connection,
) -> None:
    folder = uuid.uuid4()
    insert_rule_set(conn, folder_id=folder, name="Naming")
    with pytest.raises(errors.UniqueViolation, match="uq_rule_set_folder_name"):
        insert_rule_set(conn, folder_id=folder, name="NAMING")
    insert_rule_set(conn, folder_id=uuid.uuid4(), name="Naming")


def test_the_name_and_description_length_checks_hold(conn: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="name_length"):
        insert_rule_set(conn, name="")
    with pytest.raises(errors.CheckViolation, match="name_length"):
        insert_rule_set(conn, name="x" * 201)
    with pytest.raises(errors.CheckViolation, match="description_length"):
        conn.execute(
            "INSERT INTO rule_set (scope, folder_id, folder_kind, name, description,"
            " draft_content_sha256, created_by, updated_by)"
            " VALUES ('folder', gen_random_uuid(), 'team', 'n', %s, %s, 't', 't')",
            ("d" * 2001, HASH),
        )
