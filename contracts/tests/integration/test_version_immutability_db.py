"""The database itself refuses changes to published versions and a second Latest (VER-02, D-40).

Every statement here is raw SQL through the ``sql`` fixture (psycopg, autocommit), so none of it
goes through the application: the triggers, CHECKs and indexes are the only defence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]

PUBLISH_SECOND = (
    "UPDATE contract_version SET state = 'published', semver = '2.0.0', semver_major = 2, "
    "semver_minor = 0, semver_patch = 0, semver_prerelease = '', published_at = now(), "
    "published_by = 'system:test' WHERE id = %s"
)


def doc(odcs_id: str) -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
        "status: active\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


async def published(stack: Any, team: str) -> tuple[str, str]:
    contract, draft, etag = await stack.create_contract(
        team, doc(f"urn:dc:immutable:{uuid.uuid4().hex}")
    )
    response = await stack.publish(contract, draft, etag)
    assert response.status_code == 201, response.text
    return contract, draft


def second_row(stack: Any, sql: Sql, first: str) -> str:
    """A second draft row of the same contract, inserted by SQL (the API arrives in Plan 03-04)."""
    row = sql(
        stack.contracts_db_url,
        "INSERT INTO contract_version (contract_id, version_no, state, odcs_api_version, "
        "raw_document, media_type, content_sha256, size_bytes, document, created_by, updated_by) "
        "SELECT contract_id, 2, 'draft', odcs_api_version, raw_document, media_type, "
        "content_sha256, size_bytes, document, 'system:test', 'system:test' "
        "FROM contract_version WHERE id = %s RETURNING id",
        first,
    )[0]
    return str(row["id"])


@pytest.mark.parametrize(
    ("assignment", "value"),
    [
        ("raw_document = %s", b"tampered"),
        ("document = %s::jsonb", "{}"),
        ("content_sha256 = %s", "0" * 64),
        ("semver = %s", "9.9.9"),
        ("semver_major = %s", 9),
        ("published_at = %s", "2001-01-01T00:00:00Z"),
        ("published_by = %s", "someone:else"),
        ("version_no = %s", 9),
        ("contract_id = %s", uuid.UUID("019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa")),
        ("revision = %s", 99),
        ("name = %s", "renamed"),
    ],
)
async def test_a_content_or_identity_column_of_a_published_row_cannot_change(
    make_stack: MakeStack,
    sql: Sql,
    invariant: Invariant,
    assignment: str,
    value: object,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, draft = await published(stack, chain.team)
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            sql(
                stack.contracts_db_url,
                f"UPDATE contract_version SET {assignment} WHERE id = %s",  # noqa: S608 - fixed list
                value,
                draft,
            )
        assert invariant(stack.contracts_db_url) == []


PIN_COLUMNS = ("pinned_rule_sets", "validation_run_id", "engine_version", "schema_sha")


def pin_values(sql: Sql, stack: Any, version_id: str) -> dict[str, Any]:
    row = sql(
        stack.contracts_db_url,
        "SELECT pinned_rule_sets, validation_run_id, engine_version, schema_sha "
        "FROM contract_version WHERE id = %s",
        version_id,
    )[0]
    return dict(row)


@pytest.mark.parametrize(
    "assignment",
    [
        "pinned_rule_sets = '[]'::jsonb",
        "pinned_rule_sets = NULL",
        "validation_run_id = NULL",
        "validation_run_id = '019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa'::uuid",
        "engine_version = '0.0.0'",
        "engine_version = NULL",
        "schema_sha = 'tampered'",
        "schema_sha = NULL",
    ],
)
async def test_a_pin_column_of_a_version_published_through_the_gate_cannot_change(
    make_stack: MakeStack, sql: Sql, invariant: Invariant, assignment: str
) -> None:
    """RULE-07, D-132: what a version was validated against is frozen by the guard function."""
    # The seeded platform set is mandatory, so the pins are a non-empty array: an UPDATE to '[]' is
    # a real change and not a no-op the trigger never sees.
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        _contract, draft = await published(stack, chain.team)
        before = pin_values(sql, stack, draft)
        # The real gate recorded all four; the test would prove nothing against NULLs.
        assert all(before[column] is not None for column in PIN_COLUMNS), before
        assert before["pinned_rule_sets"], before
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            sql(
                stack.contracts_db_url,
                f"UPDATE contract_version SET {assignment} WHERE id = %s",  # noqa: S608 - fixed list
                draft,
            )
        assert pin_values(sql, stack, draft) == before
        assert invariant(stack.contracts_db_url) == []


async def test_a_lifecycle_change_still_works_and_leaves_the_pins_as_published(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, ids = await stack.publish_versions(chain.team, ["1.0.0", "2.0.0"])
        preview = ids["2.0.0"]
        latest = ids["1.0.0"]
        before = {version: pin_values(sql, stack, version) for version in (preview, latest)}
        assert all(
            values[column] is not None for values in before.values() for column in PIN_COLUMNS
        )
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET state = 'unpublished', unpublished_at = now(), "
            "unpublished_by = 'system:test' WHERE id = %s",
            preview,
        )
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET deprecated_at = now(), sunset_date = current_date + 30 "
            "WHERE id = %s",
            latest,
        )
        states = sql(
            stack.contracts_db_url,
            "SELECT id, state FROM contract_version WHERE id = ANY(%s)",
            [preview, latest],
        )
        assert {str(r["id"]): r["state"] for r in states}[preview] == "unpublished"
        for version, values in before.items():
            assert pin_values(sql, stack, version) == values
        assert invariant(stack.contracts_db_url) == []


async def test_a_published_row_cannot_go_back_to_draft_or_be_deleted(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, draft = await published(stack, chain.team)
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET state = 'draft' WHERE id = %s",
                draft,
            )
        with pytest.raises(psycopg.errors.RaiseException, match="cannot be deleted"):
            sql(stack.contracts_db_url, "DELETE FROM contract_version WHERE id = %s", draft)
        assert sql(stack.contracts_db_url, "SELECT 1 FROM contract_version WHERE id = %s", draft)
        assert invariant(stack.contracts_db_url) == []


async def test_deprecation_may_change_but_only_as_a_pair(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, draft = await published(stack, chain.team)
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET deprecated_at = now() WHERE id = %s",
                draft,
            )
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET deprecated_at = now(), sunset_date = current_date "
            "WHERE id = %s",
            draft,
        )
        row = sql(
            stack.contracts_db_url,
            "SELECT deprecated_at, sunset_date FROM contract_version WHERE id = %s",
            draft,
        )[0]
        assert row["deprecated_at"] is not None
        assert row["sunset_date"] is not None
        assert invariant(stack.contracts_db_url) == []


async def test_a_second_latest_row_violates_the_one_latest_index(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, first = await published(stack, chain.team)
        second = second_row(stack, sql, first)
        sql(stack.contracts_db_url, PUBLISH_SECOND, second)
        with pytest.raises(psycopg.errors.UniqueViolation, match="uq_contract_version_one_latest"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET is_latest = true WHERE id = %s",
                second,
            )
        assert invariant(stack.contracts_db_url) == []


async def test_the_flag_needs_a_published_row_and_the_latest_cannot_be_unpublished(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, first = await published(stack, chain.team)
        second = second_row(stack, sql, first)
        with pytest.raises(psycopg.errors.CheckViolation, match="latest_is_published"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET is_latest = true WHERE id = %s",
                second,
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="latest_is_published"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET state = 'unpublished', unpublished_at = now() "
                "WHERE id = %s",
                first,
            )
        assert invariant(stack.contracts_db_url) == []


async def test_unpublishing_needs_its_timestamp_and_a_published_row_needs_its_semver(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, first = await published(stack, chain.team)
        second = second_row(stack, sql, first)
        with pytest.raises(psycopg.errors.CheckViolation, match="published_has_semver"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET state = 'published' WHERE id = %s",
                second,
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="semver_parts_non_negative"):
            sql(
                stack.contracts_db_url,
                PUBLISH_SECOND.replace("semver_major = 2", "semver_major = -1"),
                second,
            )
        sql(stack.contracts_db_url, PUBLISH_SECOND, second)
        with pytest.raises(psycopg.errors.CheckViolation, match="unpublished_pair"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET state = 'unpublished' WHERE id = %s",
                second,
            )
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET state = 'unpublished', unpublished_at = now(), "
            "unpublished_by = 'system:test' WHERE id = %s",
            second,
        )
        assert invariant(stack.contracts_db_url) == []


async def test_an_unknown_state_is_refused(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, draft, _etag = await stack.create_contract(
            chain.team, doc(f"urn:dc:immutable:{uuid.uuid4().hex}")
        )
        with pytest.raises(psycopg.errors.RaiseException, match="a draft can only be published"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET state = 'bogus' WHERE id = %s",
                draft,
            )
        with pytest.raises(
            # PostgreSQL reports the first failing CHECK alphabetically; a bogus state with no
            # semver trips more than one, so only the exception class is pinned here.
            psycopg.errors.CheckViolation,
            match="ck_contract_version_",
        ):
            sql(
                stack.contracts_db_url,
                "INSERT INTO contract_version (contract_id, version_no, state, odcs_api_version, "
                "raw_document, media_type, content_sha256, size_bytes, document, created_by, "
                "updated_by) SELECT contract_id, 7, 'bogus', odcs_api_version, raw_document, "
                "media_type, content_sha256, size_bytes, document, 'system:test', 'system:test' "
                "FROM contract_version WHERE id = %s",
                draft,
            )


async def test_a_draft_row_stays_editable_and_deletable(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, draft, _etag = await stack.create_contract(
            chain.team, doc(f"urn:dc:immutable:{uuid.uuid4().hex}")
        )
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET raw_document = %s WHERE id = %s",
            b"edited",
            draft,
        )
        stored = sql(
            stack.contracts_db_url, "SELECT raw_document FROM contract_version WHERE id = %s", draft
        )[0]
        assert bytes(stored["raw_document"]) == b"edited"
        sql(stack.contracts_db_url, "DELETE FROM contract_version WHERE id = %s", draft)
        assert (
            sql(stack.contracts_db_url, "SELECT 1 FROM contract_version WHERE id = %s", draft) == []
        )
        assert invariant(stack.contracts_db_url) == []


async def test_publishing_cannot_change_the_content_in_the_same_statement(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        _contract, first = await published(stack, chain.team)
        second = second_row(stack, sql, first)
        with pytest.raises(psycopg.errors.RaiseException, match="publishing must not change"):
            sql(
                stack.contracts_db_url,
                PUBLISH_SECOND.replace(
                    "state = 'published',", "state = 'published', raw_document = '\\x78'::bytea,"
                ),
                second,
            )
        assert invariant(stack.contracts_db_url) == []


async def test_a_pointer_and_flag_that_disagree_fail_at_commit(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, first = await published(stack, chain.team)
        with pytest.raises(psycopg.errors.RaiseException, match="latest pointer and flag disagree"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract SET latest_version_id = NULL WHERE id = %s",
                contract,
            )
        with pytest.raises(psycopg.errors.RaiseException, match="latest pointer and flag disagree"):
            sql(
                stack.contracts_db_url,
                "UPDATE contract_version SET is_latest = false WHERE id = %s",
                first,
            )
        assert invariant(stack.contracts_db_url) == []
