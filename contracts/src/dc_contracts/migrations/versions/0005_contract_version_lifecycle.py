"""contract version lifecycle: semver columns, the Latest pointer and its indexes (D-39, D-40)

Published rows are immutable in the database (D-40): CHECK constraints, a state-aware guard
trigger that is fail-closed for columns added later (it compares whole rows minus an allow-list of
mutable keys), a delete guard and a deferred consistency trigger that keeps the Latest pointer and
the ``is_latest`` flag in agreement at commit.

A draft row is published in place (same id, same bytes): it gains its typed semver, publish
stamps and, for the first publish of a contract, the Latest flag and pointer. ``version_no``
becomes an internal per-contract row number fed by ``contract.version_counter``.

Revision ID: 0005_contract_version_lifecycle
Revises: 0004_draft_revision
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_contract_version_lifecycle"
down_revision: str | None = "0004_draft_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# plpgsql trigger bodies. Keys listed here are the only ones allowed to change; everything else
# (including a column a later migration adds) is immutable once a row is published.
_GUARD_FUNCTION = """
CREATE FUNCTION contract_version_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    publish_mutable text[] := ARRAY[
        'state', 'semver', 'semver_major', 'semver_minor', 'semver_patch', 'semver_prerelease',
        'is_latest', 'published_at', 'published_by', 'updated_by', 'updated_at'
    ];
    lifecycle_mutable text[] := ARRAY[
        'state', 'is_latest', 'unpublished_at', 'unpublished_by', 'deprecated_at',
        'deprecated_by', 'sunset_date', 'updated_by', 'updated_at'
    ];
BEGIN
    IF OLD.state = 'draft' THEN
        IF NEW.state = 'draft' THEN
            RETURN NEW;
        END IF;
        IF NEW.state <> 'published' THEN
            RAISE EXCEPTION 'a draft can only be published' USING ERRCODE = 'raise_exception';
        END IF;
        IF (to_jsonb(NEW) - publish_mutable) IS DISTINCT FROM (to_jsonb(OLD) - publish_mutable)
        THEN
            RAISE EXCEPTION 'publishing must not change the content'
                USING ERRCODE = 'raise_exception';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state NOT IN ('published', 'unpublished')
       OR (to_jsonb(NEW) - lifecycle_mutable) IS DISTINCT FROM (to_jsonb(OLD) - lifecycle_mutable)
    THEN
        RAISE EXCEPTION 'published contract versions are immutable'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
END;
$$
"""

_DELETE_FUNCTION = """
CREATE FUNCTION contract_version_reject_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state <> 'draft' THEN
        RAISE EXCEPTION 'published contract versions cannot be deleted'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN OLD;
END;
$$
"""

_CONSISTENT_FUNCTION = """
CREATE FUNCTION contract_latest_consistent() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    cid uuid;
    ptr uuid;
    flagged integer;
    matching integer;
BEGIN
    IF TG_TABLE_NAME = 'contract' THEN
        cid := NEW.id;
    ELSE
        cid := NEW.contract_id;
    END IF;
    SELECT latest_version_id INTO ptr FROM contract WHERE id = cid;
    SELECT count(*), count(*) FILTER (WHERE id = ptr)
        INTO flagged, matching
        FROM contract_version WHERE contract_id = cid AND is_latest;
    IF (ptr IS NULL AND flagged = 0) OR (ptr IS NOT NULL AND flagged = 1 AND matching = 1) THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'latest pointer and flag disagree' USING ERRCODE = 'raise_exception';
END;
$$
"""


def upgrade() -> None:
    op.add_column("contract_version", sa.Column("semver", sa.Text))
    op.add_column("contract_version", sa.Column("semver_major", sa.Integer))
    op.add_column("contract_version", sa.Column("semver_minor", sa.Integer))
    op.add_column("contract_version", sa.Column("semver_patch", sa.Integer))
    op.add_column("contract_version", sa.Column("semver_prerelease", sa.Text))
    op.add_column(
        "contract_version",
        sa.Column("is_latest", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.add_column("contract_version", sa.Column("base_version_id", postgresql.UUID(as_uuid=True)))
    op.add_column("contract_version", sa.Column("published_at", sa.DateTime(timezone=True)))
    op.add_column("contract_version", sa.Column("published_by", sa.Text))
    op.add_column("contract_version", sa.Column("unpublished_at", sa.DateTime(timezone=True)))
    op.add_column("contract_version", sa.Column("unpublished_by", sa.Text))
    op.add_column("contract_version", sa.Column("deprecated_at", sa.DateTime(timezone=True)))
    op.add_column("contract_version", sa.Column("deprecated_by", sa.Text))
    op.add_column("contract_version", sa.Column("sunset_date", sa.Date))

    op.add_column("contract", sa.Column("latest_version_id", postgresql.UUID(as_uuid=True)))
    op.add_column(
        "contract",
        sa.Column("version_counter", sa.Integer, nullable=False, server_default=sa.text("1")),
    )
    op.execute(
        "UPDATE contract SET version_counter = coalesce("
        "(SELECT max(version_no) FROM contract_version v WHERE v.contract_id = contract.id), 1)"
    )

    op.create_unique_constraint(
        "uq_contract_version_contract_id_id", "contract_version", ["contract_id", "id"]
    )
    op.create_index(
        "uq_contract_version_one_latest",
        "contract_version",
        ["contract_id"],
        unique=True,
        postgresql_where=sa.text("is_latest"),
    )
    op.create_index(
        "uq_contract_version_semver",
        "contract_version",
        ["contract_id", "semver_major", "semver_minor", "semver_patch", "semver_prerelease"],
        unique=True,
        postgresql_where=sa.text("state <> 'draft'"),
    )
    # MATCH SIMPLE: a null pointer skips the check, so a contract with no Latest is valid.
    op.create_foreign_key(
        "fk_contract_latest_version",
        "contract",
        "contract_version",
        ["id", "latest_version_id"],
        ["contract_id", "id"],
    )

    # The naming convention prefixes the table, so the short names are passed.
    op.create_check_constraint(
        "state_known", "contract_version", "state IN ('draft', 'published', 'unpublished')"
    )
    op.create_check_constraint(
        "published_has_semver",
        "contract_version",
        "state = 'draft' OR (semver IS NOT NULL AND semver_major IS NOT NULL "
        "AND semver_minor IS NOT NULL AND semver_patch IS NOT NULL "
        "AND semver_prerelease IS NOT NULL AND published_at IS NOT NULL "
        "AND published_by IS NOT NULL)",
    )
    op.create_check_constraint(
        "latest_is_published", "contract_version", "NOT is_latest OR state = 'published'"
    )
    op.create_check_constraint(
        "deprecation_pair", "contract_version", "(deprecated_at IS NULL) = (sunset_date IS NULL)"
    )
    op.create_check_constraint(
        "deprecation_not_draft",
        "contract_version",
        "deprecated_at IS NULL OR state <> 'draft'",
    )
    op.create_check_constraint(
        "unpublished_pair",
        "contract_version",
        "(state = 'unpublished') = (unpublished_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "semver_parts_non_negative",
        "contract_version",
        "coalesce(semver_major, 0) >= 0 AND coalesce(semver_minor, 0) >= 0 "
        "AND coalesce(semver_patch, 0) >= 0",
    )
    op.create_check_constraint("version_counter_positive", "contract", "version_counter >= 1")

    op.execute(_GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_contract_version_guard BEFORE UPDATE ON contract_version "
        "FOR EACH ROW EXECUTE FUNCTION contract_version_guard()"
    )
    op.execute(_DELETE_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_contract_version_no_delete BEFORE DELETE ON contract_version "
        "FOR EACH ROW EXECUTE FUNCTION contract_version_reject_delete()"
    )
    op.execute(_CONSISTENT_FUNCTION)
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_contract_latest_consistent "
        "AFTER INSERT OR UPDATE ON contract "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION contract_latest_consistent()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_contract_version_latest_consistent "
        "AFTER INSERT OR UPDATE ON contract_version "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION contract_latest_consistent()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_contract_version_latest_consistent ON contract_version")
    op.execute("DROP TRIGGER IF EXISTS trg_contract_latest_consistent ON contract")
    op.execute("DROP FUNCTION IF EXISTS contract_latest_consistent()")
    op.execute("DROP TRIGGER IF EXISTS trg_contract_version_no_delete ON contract_version")
    op.execute("DROP FUNCTION IF EXISTS contract_version_reject_delete()")
    op.execute("DROP TRIGGER IF EXISTS trg_contract_version_guard ON contract_version")
    op.execute("DROP FUNCTION IF EXISTS contract_version_guard()")
    op.drop_constraint("version_counter_positive", "contract", type_="check")
    for name in (
        "semver_parts_non_negative",
        "unpublished_pair",
        "deprecation_not_draft",
        "deprecation_pair",
        "latest_is_published",
        "published_has_semver",
        "state_known",
    ):
        op.drop_constraint(name, "contract_version", type_="check")
    op.drop_constraint("fk_contract_latest_version", "contract", type_="foreignkey")
    op.drop_index("uq_contract_version_semver", table_name="contract_version")
    op.drop_index("uq_contract_version_one_latest", table_name="contract_version")
    op.drop_constraint("uq_contract_version_contract_id_id", "contract_version", type_="unique")
    op.drop_column("contract", "version_counter")
    op.drop_column("contract", "latest_version_id")
    for column in (
        "sunset_date",
        "deprecated_by",
        "deprecated_at",
        "unpublished_by",
        "unpublished_at",
        "published_by",
        "published_at",
        "base_version_id",
        "is_latest",
        "semver_prerelease",
        "semver_patch",
        "semver_minor",
        "semver_major",
        "semver",
    ):
        op.drop_column("contract_version", column)
