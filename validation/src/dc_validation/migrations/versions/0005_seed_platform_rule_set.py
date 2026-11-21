"""seed the platform rule set odcs-structural, mandatory and enforced from version 1

D-117, D-121, D-130 (VAL-01, RULE-05): the structural rules beyond the official ODCS JSON Schema
are a platform rule set (``scope = 'platform'``, no folder), created here by the system actor
``system:validation-seed`` with its ``ruleset.version_published`` outbox event. It is mandatory,
and version 1 carries ``enforce_at = now()``, so it is enforced from its first moment. Platform
sets resolve at depth 0 for every folder and no API route can reach them (the by-id routes answer
the uniform 404), so later platform versions ship as new migrations (research Pitfall 5).

Every insert is ``ON CONFLICT ... DO NOTHING`` on a fixed id, so running the statements twice
leaves one rule set, one version and one outbox event. ``seed_statements()`` is the single source
of those statements (the integration test runs them a second time).

The rules and the content hash come from ``dc_validation.seed.ODCS_STRUCTURAL_V1`` and are
serialised exactly as the API serialises a published version (by alias, JSON mode).

Revision ID: 0005_seed_platform_rule_set
Revises: 0004_validation_runs
Create Date: 2026-11-13
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from dc_rules.model import content_sha256
from dc_validation import seed
from sqlalchemy.dialects import postgresql

revision: str = "0005_seed_platform_rule_set"
down_revision: str | None = "0004_validation_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR_ID = seed.SEED_ACTOR.id


def _jsonb(name: str) -> sa.BindParameter[Any]:
    return sa.bindparam(name, type_=postgresql.JSONB)


def _uuid(name: str) -> sa.BindParameter[Any]:
    return sa.bindparam(name, type_=postgresql.UUID(as_uuid=False))


def seed_statements() -> list[sa.TextClause]:
    """The idempotent inserts: the rule set, its version 1 and the outbox event."""
    content = seed.ODCS_STRUCTURAL_V1
    rules = content.model_dump(mode="json", by_alias=True)["rules"]
    sha = content_sha256(content)
    rule_keys = [rule.key for rule in content.rules]
    set_id = str(seed.PLATFORM_ODCS_STRUCTURAL_ID)
    version_id = str(seed.PLATFORM_ODCS_STRUCTURAL_V1_ID)
    # Same shape as ``UnitOfWork.snapshot`` of a published version (keys and hash, never rules).
    after = {
        "id": set_id,
        "scope": "platform",
        "folder_id": None,
        "folder_kind": None,
        "organization_id": None,
        "name": seed.ODCS_STRUCTURAL_NAME,
        "description": seed.ODCS_STRUCTURAL_DESCRIPTION,
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
        name=seed.ODCS_STRUCTURAL_NAME,
        description=seed.ODCS_STRUCTURAL_DESCRIPTION,
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
            now(), :actor, :actor
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
        event_id=str(seed.PLATFORM_SEED_EVENT_ID),
        aggregate_id=set_id,
        actor=seed.SEED_ACTOR.model_dump(),
        after=after,
    )
    return [insert_set, insert_version, insert_event]


def upgrade() -> None:
    for statement in seed_statements():
        op.execute(statement)


def downgrade() -> None:
    # Published versions cannot be deleted; the guard is lifted for this one downgrade only.
    op.execute("ALTER TABLE rule_set_version DISABLE TRIGGER trg_rule_set_version_no_delete")
    op.execute(
        sa.text("DELETE FROM rule_set_version WHERE rule_set_id = :id").bindparams(
            _uuid("id"), id=str(seed.PLATFORM_ODCS_STRUCTURAL_ID)
        )
    )
    op.execute("ALTER TABLE rule_set_version ENABLE TRIGGER trg_rule_set_version_no_delete")
    op.execute(
        sa.text("DELETE FROM rule_set WHERE id = :id").bindparams(
            _uuid("id"), id=str(seed.PLATFORM_ODCS_STRUCTURAL_ID)
        )
    )
    op.execute(
        sa.text("DELETE FROM outbox WHERE event_id = :id").bindparams(
            _uuid("id"), id=str(seed.PLATFORM_SEED_EVENT_ID)
        )
    )
