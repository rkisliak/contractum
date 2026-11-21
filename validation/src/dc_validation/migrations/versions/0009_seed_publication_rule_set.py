"""seed the platform rule set odcs-publication, mandatory, in warn grace

Quick 261119-rpi (UD-1): the publish-only checks ship as a platform rule set
(``scope = 'platform'``, no folder, ``dsl_version = 'cel-2'``) created by the system actor
``system:validation-seed`` with its ``ruleset.version_published`` outbox event. Its one rule,
``published-status-active``, warns when a version is published while the ODCS ``status`` is not
``active``. Like odcs-consistency and odcs-references (0006) it is mandatory and NOT enforced from
the start: ``enforce_at = now() + mandatory_warn_days`` from ``ValidationSettings``. The rule is
warning-severity, so it never blocks, even after ``enforce_at`` (D-124): enforcing only turns the
finding's mode from ``warn`` to ``enforce``. A Global admin can still pause it or move its date
through the ROOT platform routes.

Every insert is ``ON CONFLICT ... DO NOTHING`` on a fixed id, so running the statements twice
leaves one rule set, one version and one outbox event. This migration is a self-contained copy of
the 0006 shape; 0005 and 0006 and their constants are never edited. The rules and hash come from
``dc_validation.seed.ODCS_PUBLICATION_V1``.

Revision ID: 0009_seed_publication_rule_set
Revises: 0008_folder_purge_guard
Create Date: 2026-11-19
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from dc_rules.model import RuleSetContent, content_sha256
from dc_validation import seed
from dc_validation.settings import ValidationSettings
from sqlalchemy.dialects import postgresql

revision: str = "0009_seed_publication_rule_set"
down_revision: str | None = "0008_folder_purge_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR_ID = seed.SEED_ACTOR.id


@dataclass(frozen=True)
class _Seeded:
    set_id: UUID
    version_id: UUID
    event_id: UUID
    name: str
    description: str
    content: RuleSetContent


SEEDED: tuple[_Seeded, ...] = (
    _Seeded(
        seed.PLATFORM_ODCS_PUBLICATION_ID,
        seed.PLATFORM_ODCS_PUBLICATION_V1_ID,
        seed.PLATFORM_PUBLICATION_SEED_EVENT_ID,
        seed.ODCS_PUBLICATION_NAME,
        seed.ODCS_PUBLICATION_DESCRIPTION,
        seed.ODCS_PUBLICATION_V1,
    ),
)


def _jsonb(name: str) -> sa.BindParameter[Any]:
    return sa.bindparam(name, type_=postgresql.JSONB)


def _uuid(name: str) -> sa.BindParameter[Any]:
    return sa.bindparam(name, type_=postgresql.UUID(as_uuid=False))


def _statements(item: _Seeded, warn_days: int) -> list[sa.TextClause]:
    content = item.content
    rules = content.model_dump(mode="json", by_alias=True)["rules"]
    sha = content_sha256(content)
    rule_keys = [rule.key for rule in content.rules]
    set_id = str(item.set_id)
    version_id = str(item.version_id)
    # Same shape as ``UnitOfWork.snapshot`` of a published version (keys and hash, never rules).
    after = {
        "id": set_id,
        "scope": "platform",
        "folder_id": None,
        "folder_kind": None,
        "organization_id": None,
        "name": item.name,
        "description": item.description,
        "publish_counter": 1,
        "mandatory": True,
        "version": {
            "id": version_id,
            "rule_set_id": set_id,
            "version_no": 1,
            "dsl_version": content.dsl_version,
            "content_sha256": sha,
            "rule_keys": rule_keys,
        },
    }
    insert_set = sa.text(
        """
        INSERT INTO rule_set (
            id, scope, folder_id, folder_kind, ancestor_ids, organization_id, name, description,
            publish_counter, draft_rules, draft_revision, draft_content_sha256,
            mandatory, mandatory_set_at, mandatory_set_by,
            created_by, updated_by
        ) VALUES (
            :id, 'platform', NULL, NULL, '{}'::uuid[], NULL, :name, :description,
            1, :draft_rules, 0, :sha,
            true, now(), :actor,
            :actor, :actor
        )
        ON CONFLICT (id) DO NOTHING
        """
    ).bindparams(
        _uuid("id"),
        _jsonb("draft_rules"),
        id=set_id,
        name=item.name,
        description=item.description,
        draft_rules=rules,
        sha=sha,
        actor=ACTOR_ID,
    )
    insert_version = sa.text(
        """
        INSERT INTO rule_set_version (
            id, rule_set_id, version_no, dsl_version, rules, content_sha256, rule_keys,
            enforce_at, created_by, updated_by
        ) VALUES (
            :id, :rule_set_id, 1, :dsl_version, :rules, :sha, :rule_keys,
            now() + make_interval(days => :warn_days), :actor, :actor
        )
        ON CONFLICT (id) DO NOTHING
        """
    ).bindparams(
        _uuid("id"),
        _uuid("rule_set_id"),
        sa.bindparam("rule_keys", type_=postgresql.ARRAY(sa.Text)),
        _jsonb("rules"),
        id=version_id,
        rule_set_id=set_id,
        dsl_version=content.dsl_version,
        rules=rules,
        sha=sha,
        rule_keys=rule_keys,
        warn_days=warn_days,
        actor=ACTOR_ID,
    )
    insert_event = sa.text(
        """
        INSERT INTO outbox (
            event_id, aggregate_type, aggregate_id, aggregate_version, action, scope_path,
            actor, before, after
        ) VALUES (
            :event_id, 'rule_set', :aggregate_id, 1, 'ruleset.version_published', '{}'::uuid[],
            :actor, NULL, :after
        )
        ON CONFLICT (event_id) DO NOTHING
        """
    ).bindparams(
        _uuid("event_id"),
        _jsonb("actor"),
        _jsonb("after"),
        event_id=str(item.event_id),
        aggregate_id=set_id,
        actor=seed.SEED_ACTOR.model_dump(),
        after=after,
    )
    return [insert_set, insert_version, insert_event]


def seed_statements(warn_days: int | None = None) -> list[sa.TextClause]:
    """The idempotent inserts for the set; ``warn_days`` defaults to the service setting."""
    days = ValidationSettings().mandatory_warn_days if warn_days is None else warn_days
    return [statement for item in SEEDED for statement in _statements(item, days)]


def upgrade() -> None:
    for statement in seed_statements():
        op.execute(statement)


def downgrade() -> None:
    # Published versions cannot be deleted; the guard is lifted for this one downgrade only.
    op.execute("ALTER TABLE rule_set_version DISABLE TRIGGER trg_rule_set_version_no_delete")
    for item in SEEDED:
        op.execute(
            sa.text("DELETE FROM rule_set_version WHERE rule_set_id = :id").bindparams(
                _uuid("id"), id=str(item.set_id)
            )
        )
    op.execute("ALTER TABLE rule_set_version ENABLE TRIGGER trg_rule_set_version_no_delete")
    for item in SEEDED:
        op.execute(
            sa.text("DELETE FROM rule_set WHERE id = :id").bindparams(
                _uuid("id"), id=str(item.set_id)
            )
        )
        op.execute(
            sa.text("DELETE FROM outbox WHERE event_id = :id").bindparams(
                _uuid("id"), id=str(item.event_id)
            )
        )
