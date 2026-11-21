"""contract version pins: the validation run and rule-set versions a version was published under

A published version records what governance validated it against (RULE-07, D-132):
``validation_run_id`` (validation's stored run), ``pinned_rule_sets`` (JSONB array of the rule-set
versions evaluated, every warn-mode version included), ``engine_version`` and ``schema_sha``. All
four are null on a draft and on every version published before this revision; no backfill, because
nothing recorded what those were validated against.

The immutability guard is replaced (``CREATE OR REPLACE``) with the 0005 body verbatim except that
``publish_mutable`` also names the four columns, so the publish transaction can set them and every
later UPDATE of them is refused (any column outside an allow-list is immutable once published).
``lifecycle_mutable`` is unchanged.

Revision ID: 0014_contract_version_pins
Revises: 0013_card_webhook_secret
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_contract_version_pins"
down_revision: str | None = "0013_card_webhook_secret"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD_TEMPLATE = """
CREATE OR REPLACE FUNCTION contract_version_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    publish_mutable text[] := ARRAY[
        'state', 'semver', 'semver_major', 'semver_minor', 'semver_patch', 'semver_prerelease',
        'is_latest', 'published_at', 'published_by', 'updated_by', 'updated_at'{extra}
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

# The four names appended to publish_mutable only (D-132).
_PIN_COLUMNS = ",\n        'validation_run_id', 'pinned_rule_sets', 'engine_version', 'schema_sha'"
_GUARD_WITH_PINS = _GUARD_TEMPLATE.format(extra=_PIN_COLUMNS)
# The 0005 body, re-applied by the downgrade.
_GUARD_0005 = _GUARD_TEMPLATE.format(extra="")


def upgrade() -> None:
    op.add_column("contract_version", sa.Column("validation_run_id", postgresql.UUID(as_uuid=True)))
    op.add_column("contract_version", sa.Column("pinned_rule_sets", postgresql.JSONB))
    op.add_column("contract_version", sa.Column("engine_version", sa.Text))
    op.add_column("contract_version", sa.Column("schema_sha", sa.Text))
    op.execute(_GUARD_WITH_PINS)


def downgrade() -> None:
    op.execute(_GUARD_0005)
    op.drop_column("contract_version", "schema_sha")
    op.drop_column("contract_version", "engine_version")
    op.drop_column("contract_version", "pinned_rule_sets")
    op.drop_column("contract_version", "validation_run_id")
