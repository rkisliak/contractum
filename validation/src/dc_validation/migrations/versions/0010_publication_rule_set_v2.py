"""publish version 2 of the platform rule set odcs-publication, in warn grace

Quick 261128-rsa: a released contract version (a SemVer without a prerelease part) must declare
ODCS ``status: active``. Version 2 of ``odcs-publication`` adds the **error** rule
``released-status-active`` and keeps the ``published-status-active`` warning for Preview publishes
(prerelease versions). Version 1 and its constant are never edited (0009 stored its hash).

The rollout is the one every new version of a mandatory set gets (``rulesets.publish_rule_set``,
D-120): ``publish_counter`` becomes 2, the draft becomes the version 2 content, version 2 is
inserted with ``enforce_at = now() + mandatory_warn_days`` (read from ``ValidationSettings`` at
migration time, like 0006 and 0009, so set ``DC_MANDATORY_WARN_DAYS`` on ``validation-migrate`` too)
and one ``ruleset.version_published`` outbox event records it. Until ``enforce_at`` version 2 is
reported as a non-blocking ``warn`` entry next to version 1; from then on it is enforced, version 1
drops out and a released version without ``status: active`` is refused. A Global admin can pause the
set or move the date through the ROOT platform routes, as for every platform set. The set's
description is updated in the same step: the old one said "never blocking".

Idempotent: the update applies only while ``publish_counter`` is 1, the version and the event are
``ON CONFLICT DO NOTHING`` on fixed ids, and nothing is written when the set does not exist. The
rules and hash come from ``dc_validation.seed.ODCS_PUBLICATION_V2``.

Revision ID: 0010_publication_rule_set_v2
Revises: 0009_seed_publication_rule_set
Create Date: 2026-11-28
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from dc_rules.model import RuleSetContent, content_sha256
from dc_validation import seed
from dc_validation.settings import ValidationSettings
from sqlalchemy.dialects import postgresql

revision: str = "0010_publication_rule_set_v2"
down_revision: str | None = "0009_seed_publication_rule_set"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR_ID = seed.SEED_ACTOR.id
SET_ID = str(seed.PLATFORM_ODCS_PUBLICATION_ID)
VERSION_ID = str(seed.PLATFORM_ODCS_PUBLICATION_V2_ID)
EVENT_ID = str(seed.PLATFORM_PUBLICATION_V2_EVENT_ID)


def _jsonb(name: str) -> sa.BindParameter[Any]:
    return sa.bindparam(name, type_=postgresql.JSONB)


def _uuid(name: str) -> sa.BindParameter[Any]:
    return sa.bindparam(name, type_=postgresql.UUID(as_uuid=False))


def _rules(content: RuleSetContent) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = content.model_dump(mode="json", by_alias=True)["rules"]
    return rules


def _snapshot(
    description: str, counter: int, version_no: int, version_id: str, content: RuleSetContent
) -> dict[str, Any]:
    """The 0009 snapshot shape (keys and hash, never rules) for the set at ``version_no``."""
    return {
        "id": SET_ID,
        "scope": "platform",
        "folder_id": None,
        "folder_kind": None,
        "organization_id": None,
        "name": seed.ODCS_PUBLICATION_NAME,
        "description": description,
        "publish_counter": counter,
        "mandatory": True,
        "version": {
            "id": version_id,
            "rule_set_id": SET_ID,
            "version_no": version_no,
            "dsl_version": content.dsl_version,
            "content_sha256": content_sha256(content),
            "rule_keys": [rule.key for rule in content.rules],
        },
    }


def seed_statements(warn_days: int | None = None) -> list[sa.TextClause]:
    """The idempotent statements; ``warn_days`` defaults to the service setting."""
    days = ValidationSettings().mandatory_warn_days if warn_days is None else warn_days
    content = seed.ODCS_PUBLICATION_V2
    rules = _rules(content)
    sha = content_sha256(content)
    rule_keys = [rule.key for rule in content.rules]
    update_set = sa.text(
        """
        UPDATE rule_set
           SET publish_counter = 2,
               draft_rules = :draft_rules,
               draft_content_sha256 = :sha,
               draft_revision = draft_revision + 1,
               description = :description,
               updated_by = :actor,
               updated_at = now()
         WHERE id = :id AND publish_counter = 1
        """
    ).bindparams(
        _uuid("id"),
        _jsonb("draft_rules"),
        id=SET_ID,
        draft_rules=rules,
        sha=sha,
        description=seed.ODCS_PUBLICATION_V2_DESCRIPTION,
        actor=ACTOR_ID,
    )
    insert_version = sa.text(
        """
        INSERT INTO rule_set_version (
            id, rule_set_id, version_no, dsl_version, rules, content_sha256, rule_keys,
            enforce_at, created_by, updated_by
        )
        SELECT :id, rs.id, 2, :dsl_version, :rules, :sha, :rule_keys,
               now() + make_interval(days => :warn_days), :actor, :actor
          FROM rule_set rs
         WHERE rs.id = :rule_set_id AND rs.publish_counter = 2
        ON CONFLICT DO NOTHING
        """
    ).bindparams(
        _uuid("id"),
        _uuid("rule_set_id"),
        sa.bindparam("rule_keys", type_=postgresql.ARRAY(sa.Text)),
        _jsonb("rules"),
        id=VERSION_ID,
        rule_set_id=SET_ID,
        dsl_version=content.dsl_version,
        rules=rules,
        sha=sha,
        rule_keys=rule_keys,
        warn_days=days,
        actor=ACTOR_ID,
    )
    insert_event = sa.text(
        """
        INSERT INTO outbox (
            event_id, aggregate_type, aggregate_id, aggregate_version, action, scope_path,
            actor, before, after
        )
        SELECT :event_id, 'rule_set', :aggregate_id, 2, 'ruleset.version_published',
               '{}'::uuid[], :actor, :before, :after
         WHERE EXISTS (SELECT 1 FROM rule_set_version WHERE id = :version_id)
        ON CONFLICT (event_id) DO NOTHING
        """
    ).bindparams(
        _uuid("event_id"),
        _uuid("version_id"),
        _jsonb("actor"),
        _jsonb("before"),
        _jsonb("after"),
        event_id=EVENT_ID,
        version_id=VERSION_ID,
        aggregate_id=SET_ID,
        actor=seed.SEED_ACTOR.model_dump(),
        before=_snapshot(
            seed.ODCS_PUBLICATION_DESCRIPTION,
            1,
            1,
            str(seed.PLATFORM_ODCS_PUBLICATION_V1_ID),
            seed.ODCS_PUBLICATION_V1,
        ),
        after=_snapshot(seed.ODCS_PUBLICATION_V2_DESCRIPTION, 2, 2, VERSION_ID, content),
    )
    return [update_set, insert_version, insert_event]


def upgrade() -> None:
    for statement in seed_statements():
        op.execute(statement)


def downgrade() -> None:
    # Published versions cannot be deleted; the guard is lifted for this one downgrade only.
    op.execute("ALTER TABLE rule_set_version DISABLE TRIGGER trg_rule_set_version_no_delete")
    op.execute(
        sa.text("DELETE FROM rule_set_version WHERE id = :id").bindparams(
            _uuid("id"), id=VERSION_ID
        )
    )
    op.execute("ALTER TABLE rule_set_version ENABLE TRIGGER trg_rule_set_version_no_delete")
    v1 = seed.ODCS_PUBLICATION_V1
    op.execute(
        sa.text(
            """
            UPDATE rule_set
               SET publish_counter = 1,
                   draft_rules = :draft_rules,
                   draft_content_sha256 = :sha,
                   draft_revision = draft_revision + 1,
                   description = :description,
                   updated_by = :actor,
                   updated_at = now()
             WHERE id = :id AND publish_counter = 2
            """
        ).bindparams(
            _uuid("id"),
            _jsonb("draft_rules"),
            id=SET_ID,
            draft_rules=_rules(v1),
            sha=content_sha256(v1),
            description=seed.ODCS_PUBLICATION_DESCRIPTION,
            actor=ACTOR_ID,
        )
    )
    op.execute(
        sa.text("DELETE FROM outbox WHERE event_id = :id").bindparams(_uuid("id"), id=EVENT_ID)
    )
